"""
阶段三：自然语言 → YAML 生成器 — TestCaseGenerator

接收中文/英文自然语言描述，调用 LLM API 生成符合 Maestro 语法的 YAML 测试用例。
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from openai import OpenAI

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
# 系统 Prompt 设计
# ═══════════════════════════════════════════

MAESTRO_SYSTEM_PROMPT = """You are a Maestro mobile automation testing expert. Your task is to convert natural language test descriptions into valid Maestro YAML format.

## CRITICAL RULES:
# 【规则1】纯YAML输出：禁止输出markdown代码块标记(```)、禁止任何解释性文字或对话内容。
# 目的：确保LLM输出的内容可以直接被YAML解析器读取，避免后处理时出现格式错误。
1. Output ONLY the raw YAML content — no markdown fences, no code blocks, no explanations.

# 【规则2】必须包含appId：每个YAML文件必须以 appId: 开头，紧随其后的是 - launchApp 步骤。
# 目的：Maestro框架要求每个测试脚本明确指定被测应用ID并首先启动应用。
2. Every YAML must start with `appId:` followed by `- launchApp`.

# 【规则3】命令白名单：限制LLM只能使用Maestro框架原生支持的9种命令。
# 目的：防止LLM生成Maestro不识别的自定义命令或幻觉命令，保证YAML可被正确执行。
3. Use ONLY these Maestro commands:
   - launchApp
   - tapOn: <element>
   - inputText: "<text>"
   - assertVisible: <element>
   - assertNotVisible: <element>
   - swipe: (direction)
   - scroll
   - waitForAnimationToEnd
   - extendedWaitUntil/visible
   - pressKey: back

# 【规则4】元素定位器规范：定义了5种Maestro支持的元素查找方式。
# - text: 精确文本匹配或正则匹配(用.*包裹表示正则)
# - id: Android resource-id定位
# - index: 按元素索引定位
# - 相对位置: 基于其他元素的空间关系定位(above/below/leftOf/rightOf)
# 目的：引导LLM使用Maestro原生的多种定位策略，避免使用不支持的XPath等复杂表达式。
4. Element locators support:
   - text: "Login"                      (exact text match)
   - text: ".*Login.*"                  (regex text match)
   - id: "com.example:id/button"        (resource-id)
   - index: 0                           (element index)
   - Relative position: above/below/leftOf/rightOf other elements

# 【规则5】重试机制：对点击、断言等不稳定操作，要求包裹在 retry 块中。
# 目的：移动端UI渲染往往存在延迟，通过重试机制提高测试脚本的鲁棒性和抗抖动能力。
5. Include `retry` blocks for potentially flaky steps (tapOn, assertVisible).

# 【规则6】与maestro studio录制兼容：要求使用简洁、人类可读的元素选择器。
# 目的：确保生成的YAML与maestro studio录制格式一致，便于团队人员阅读和调试。
6. The generated YAML must be compatible with `maestro studio` recording.
   - Use simple, human-readable element selectors.
   - Avoid overly complex XPath expressions.

# 【规则7】等待策略：在交互动态加载元素前，先等待该元素可见。
# 目的：防止因网络延迟或渲染延迟导致的"No element found"错误，确保交互时机正确。
7. Add `- extendedWaitUntil/visible` before interacting with elements that may not appear immediately.

## OUTPUT FORMAT:
appId: com.example.app
---
- launchApp
- extendedWaitUntil:
    visible:
      text: "Welcome"
    timeout: 10000
- tapOn: "Login"
- inputText: "username"
- ...

Do NOT include triple backticks or ```yaml markers. Start directly with `appId:`."""


# ═══════════════════════════════════════════
# 实体抽取
# ═══════════════════════════════════════════

class EntityExtractor:
    """
    从自然语言中提取核心动作实体。

    设计思路：
    1. 先通过 CJK Unicode 范围（\\u4e00-\\u9fff）检测输入语言是中/英文。
    2. 用中/英文标点（。，,.;!！?\\n）将文本拆分为独立句子。
    3. 对每个句子遍历对应语言的正则模式，匹配动作 + 目标 + 可选值。
    4. 将匹配结果组装为标准化的 step 字典，供后续 prompt 构建使用。

    正则设计逻辑：
    - 每个模式通过捕获组 (group) 提取关键信息：
      * click/swipe/wait: 只需 target（被操作的元素）
      * input: 需要 target（输入框）和 value（输入内容）两个捕获组
      * assert: 需要 target（被检查的元素）和 value（预期的状态/内容）
      * launch: 固定 target 为 "launchApp"
    - 正则末尾的 (?:$|[,，。]) 确保匹配在句子边界或列表分隔处停止，
      避免跨子句的过度贪婪匹配。
    """

    # ╔══════════════════════════════════════════════════════════════╗
    # ║  中文动作关键词映射                                          ║
    # ║  每个模式包含1-2个捕获组(group): target 和可选的 value        ║
    # ╚══════════════════════════════════════════════════════════════╝
    CN_ACTION_PATTERNS = {
        # 匹配: "点击登录按钮" / "单击设置" / "选择商品"
        # group(1) = 目标元素（如"登录按钮"）
        "click": re.compile(r"(点击|单击|点|按下|选择|进入)(.+?)(?:$|[,，。])"),
        # 匹配: "输入用户名 admin" / "填入密码 123456"
        # group(1) = 输入框名称, group(2) = 输入的值
        "input": re.compile(r"(输入|填入|键入|填写)(.+?)(?:为|的值)?(.+?)(?:$|[,，。])"),
        # 匹配: "滑动到页面底部" / "上滑" / "左滑到侧边栏"
        # group(1) = 滑动方向或目标
        "swipe": re.compile(r"(滑动|上滑|下滑|左滑|右滑)(?:到)?(.+?)(?:$|[,，。])"),
        # 匹配: "验证首页显示欢迎语" / "检查登录按钮存在" / "确认支付成功"
        # group(1) = 被验证元素, group(2) = 预期状态
        "assert": re.compile(r"(验证|检查|断言|确认|确保)(.+?)(?:是否|是|存在|显示|包含)(.+?)(?:$|[,，。])"),
        # 匹配: "打开App" / "启动应用" / "进入程序"
        # 无捕获组，匹配后固定 target="launchApp"
        "launch": re.compile(r"(打开|启动|进入)(?:App|应用|程序)"),
        # 匹配: "等待页面加载" / "稍等2秒"
        # group(1) = 等待的对象或时长
        "wait": re.compile(r"(等待|稍等)(.+?)(?:$|[,，。])"),
    }

    # ╔══════════════════════════════════════════════════════════════╗
    # ║  英文动作关键词映射（re.I = 忽略大小写）                       ║
    # ╚══════════════════════════════════════════════════════════════╝
    EN_ACTION_PATTERNS = {
        # 匹配: "click the login button" / "tap on Submit" / "press Enter"
        "click": re.compile(r"(?:click|tap|press|select)\s+(.+?)(?:$|[,.;])", re.I),
        # 匹配: "input username admin" / "enter password 123456" / "type hello"
        # group(1) = 输入框, group(2) = 输入内容
        "input": re.compile(r"(?:input|enter|type|fill)\s+(.+?)(?:with|as)?\s*(.+?)(?:$|[,.;])", re.I),
        # 匹配: "swipe to bottom" / "scroll down"
        # group(1) = 方向或目标
        "swipe": re.compile(r"(?:swipe|scroll)\s+(?:to\s+)?(.+?)(?:$|[,.;])", re.I),
        # 匹配: "assert that home page shows welcome" / "verify login button exists"
        # group(1) = 被验证元素, group(2) = 预期状态
        "assert": re.compile(r"(?:assert|verify|check|confirm)\s+(?:that\s+)?(.+?)(?:is|exists|shows|displays|contains)\s+(.+?)(?:$|[,.;])", re.I),
        # 匹配: "open app" / "launch the application"
        "launch": re.compile(r"(?:open|launch|start)\s+(?:the\s+)?(?:app|application)", re.I),
        # 匹配: "wait for page load" / "pause 3 seconds"
        "wait": re.compile(r"(?:wait|pause)\s+(.+?)(?:$|[,.;])", re.I),
    }

    @classmethod
    def extract(cls, text: str) -> list[dict[str, str]]:
        """
        从自然语言文本中提取测试步骤。

        Returns:
            [{"action": "click", "target": "登录按钮", "value": ""}, ...]
        """
        steps: list[dict[str, str]] = []

        # ── 语言检测 ──
        # 利用 CJK 统一汉字 Unicode 范围 \\u4e00-\\u9fff 判断是否为中文文本。
        # 此范围覆盖最常用的中文字符，如果文本中包含至少一个 CJK 汉字则判定为中文，
        # 从而选择中文正则模式进行匹配；否则使用英文模式。
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
        patterns = cls.CN_ACTION_PATTERNS if has_cjk else cls.EN_ACTION_PATTERNS

        # ── 句子分割 ──
        # 使用中文和英文常见的标点符号作为分隔符将文本拆分为独立句子：
        #   。（中文句号）、，（中文逗号）、,（英文逗号）、.（英文句号）、
        #   ;（分号）、!（感叹号）、？（中文问号）、\\n（换行符）
        # 每个句子独立匹配动作模式，避免长文本中跨句子的过度贪婪匹配。
        sentences = re.split(r"[。，,.;!！?\n]", text)
        step_index = 0

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            # ── 遍历所有动作模式进行匹配 ──
            # 对每个句子尝试匹配所有已定义的动作模式（click/input/swipe/assert/launch/wait）。
            # 一旦某个模式匹配成功（非贪婪的 search），立即 break 进入下一个句子，
            # 避免同一句子被多个模式重复匹配。
            for action_name, pattern in patterns.items():
                match = pattern.search(sentence)
                if match:
                    step_index += 1
                    step = {
                        "index": str(step_index),
                        "action": action_name,
                        "target": "",
                        "value": "",
                        "raw": sentence,
                    }

                    # 根据动作类型从正则捕获组中提取 target 和 value：
                    # - input:  需要两个捕获组——group(1)=输入框名称(target), group(2)=输入内容(value)
                    # - click/swipe/wait: 只需一个捕获组——group(1)=被操作元素(target)
                    # - assert: 需要两个捕获组——group(1)=被检查元素(target), group(2)=预期状态(value)
                    # - launch: 固定 target 为 "launchApp"，无 value
                    if action_name == "input":
                        step["target"] = match.group(1).strip() if len(match.groups()) >= 1 else ""
                        step["value"] = match.group(2).strip() if len(match.groups()) >= 2 else ""
                    elif action_name in ("click", "swipe", "wait"):
                        step["target"] = match.group(1).strip()
                    elif action_name == "assert":
                        step["target"] = match.group(1).strip() if len(match.groups()) >= 1 else ""
                        step["value"] = match.group(2).strip() if len(match.groups()) >= 2 else ""
                    elif action_name == "launch":
                        step["target"] = "launchApp"

                    steps.append(step)
                    break

        # ── 首步保障：确保第一个步骤始终是 launchApp ──
        # 如果用户描述中没有显式提到"打开App"等启动操作，自动在最前面插入一个
        # launch 步骤。这保证了生成的YAML始终以 appId + launchApp 开头，
        # 符合 Maestro 测试脚本的强制要求。
        if steps and steps[0]["action"] != "launch":
            steps.insert(0, {
                "index": "0",
                "action": "launch",
                "target": "launchApp",
                "value": "",
                "raw": "打开App",
            })

        logger.info(f"从自然语言中提取了 {len(steps)} 个步骤")
        return steps


# ═══════════════════════════════════════════
# TestCaseGenerator — 主类
# ═══════════════════════════════════════════

class TestCaseGenerator:
    """
    自然语言 → Maestro YAML 生成器。

    使用示例:
        generator = TestCaseGenerator()
        yaml_path = generator.generate(
            "打开 App，点击登录，输入账号 admin 和密码 123456，验证首页显示欢迎语。",
            app_id="com.example.app",
            platform="android"
        )
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ):
        # API Key 优先级：构造参数 > DEEPSEEK_API_KEY 环境变量 > OPENAI_API_KEY 环境变量
        # 这种设计允许调用方显式传入 key，同时也兼容默认环境变量配置。
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
        # Base URL 默认指向 DeepSeek API，可通过 DEEPSEEK_BASE_URL 环境变量覆盖。
        # 兼容 OpenAI 标准接口，因此也可配置为任何 OpenAI 兼容的 API 地址。
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        # 模型名称：构造参数 > AI_MODEL 环境变量 > 默认值 "deepseek-chat"
        self.model = model or os.getenv("AI_MODEL", "deepseek-chat")
        # YAML 输出目录：相对路径 maestro_ai_platform/flows/
        self.flows_dir = Path(__file__).parent.parent.parent / "flows"

        # _client 初始化为 None，遵循懒惰初始化模式（Lazy Initialization Pattern）。
        # 不在 __init__ 中创建 OpenAI 客户端，而是等到首次访问 client 属性时才创建。
        # 好处：
        #   1. 避免在未配置 API Key 时构造阶段就报错，将错误推迟到实际调用时。
        #   2. 允许实例化 generator 对象后动态修改 api_key/base_url 再触发客户端创建。
        #   3. 减少不必要的网络连接——仅在需要调用 LLM 时才初始化客户端。
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        """
        【懒惰客户端属性模式（Lazy Client Property）】

        设计意图：
        - 不在 __init__ 中立即创建 OpenAI 客户端，而是在首次访问此属性时按需创建。
        - 检查 _client 是否为 None：若为 None 表示尚未初始化，则检查 api_key 是否存在，
          不存在则抛出明确的中文错误提示；存在则创建新的 OpenAI 客户端并缓存到 _client。
        - 后续所有调用都复用同一个已缓存的客户端实例（单例模式），避免重复创建。

        ValueError 触发场景：用户未设置 DEEPSEEK_API_KEY 或 OPENAI_API_KEY 环境变量，
        也未在构造函数中传入 api_key 参数。此时会中断流程并给出明确指引。
        """
        if self._client is None:
            if not self.api_key:
                raise ValueError(
                    "未设置 API Key。请设置环境变量 DEEPSEEK_API_KEY 或 OPENAI_API_KEY"
                )
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def generate(
        self,
        description: str,
        app_id: str = "com.example.app",
        platform: str = "android",
        model: str | None = None,
    ) -> Path:
        """
        从自然语言描述生成 Maestro YAML 测试文件。

        完整执行流水线（5步）：
          步骤1 → 实体抽取：将自然语言中的动作关键词提取为结构化步骤列表
          步骤2 → 构建Prompt：将实体、appId、平台信息拼装成LLM友好的用户提示词
          步骤3 → 调用LLM：通过OpenAI兼容接口发送系统Prompt+用户Prompt，获取YAML文本
          步骤4 → 清理输出：用正则去除LLM可能夹带的markdown代码块标记(```yaml)
          步骤5 → 保存YAML：将清理后的内容写入 flows/ 目录，文件名带时间戳

        Args:
            description: 自然语言测试描述（中文/英文）
            app_id: 目标 App 的 packageId / bundleId
            platform: 目标平台 (android / ios)
            model: 使用的 AI 模型（覆盖默认值）

        Returns:
            生成的 YAML 文件路径
        """
        # ── 步骤1：实体抽取 ──
        # 调用 EntityExtractor.extract() 解析自然语言描述，提取出结构化的动作列表。
        # 返回格式：[{"index":"1","action":"click","target":"登录按钮","value":"","raw":"点击登录按钮"}, ...]
        entities = EntityExtractor.extract(description)
        logger.info(f"实体抽取完成: {len(entities)} 个步骤")

        # ── 步骤2：构建用户 Prompt ──
        # 将抽取的实体与 appId、platform、原始描述组合成结构化的用户提示词。
        # Prompt 中明确要求 LLM 使用 Maestro 语法、添加等待和重试机制。
        user_prompt = self._build_user_prompt(description, app_id, platform, entities)

        # ── 步骤3：调用 LLM 生成 YAML ──
        # 通过 OpenAI SDK 的 chat.completions.create 方法发送请求。
        # system 消息使用预定义的 MAESTRO_SYSTEM_PROMPT，user 消息使用步骤2构建的 prompt。
        # temperature=0.2 确保输出YAML的确定性和一致性，max_tokens=4096 限制输出长度。
        yaml_content = self._call_llm(user_prompt, model or self.model)

        # ── 步骤4：清理输出 ──
        # LLM 有时会在输出中包裹 ```yaml ... ``` 的 markdown 代码块标记。
        # 使用正则去除这些标记以及首尾空白，确保输出是纯净的 YAML 文本。
        yaml_content = self._clean_yaml_output(yaml_content)

        # ── 步骤5：保存到文件 ──
        # 以时间戳命名文件（格式：generated_YYYYMMDD_HHMMSS.yaml），
        # 确保多次生成不会相互覆盖，便于追溯和回滚。
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"generated_{timestamp}.yaml"
        self.flows_dir.mkdir(parents=True, exist_ok=True)
        yaml_path = self.flows_dir / filename
        yaml_path.write_text(yaml_content, encoding="utf-8")

        logger.info(f"YAML 测试用例已生成: {yaml_path}")
        return yaml_path

    def _build_user_prompt(
        self,
        description: str,
        app_id: str,
        platform: str,
        entities: list[dict[str, str]],
    ) -> str:
        """
        构建发送给 LLM 的用户 Prompt。

        将 EntityExtractor 提取的实体列表格式化为编号列表：
           1. [click] target='登录按钮', value=''
           2. [input] target='用户名输入框', value='admin'
           ...

        这种格式让 LLM 能够：
        1. 理解每个步骤的动作类型（action）和目标元素（target）
        2. 获取输入操作的具体值（value）
        3. 以这些预提取的动作为"骨架"指导，填充 Maestro 所需的等待/重试/缩进等细节

        Prompt 还包含 Platform 和 App ID 信息，
        以及额外的约束要求（extendedWaitUntil、retry、timeout、assertVisible等）。
        """
        # 构建实体列表的多行字符串，每行格式: "  序号. [动作类型] target='目标', value='值'"
        entity_lines = "\n".join(
            f"  {e['index']}. [{e['action']}] target='{e['target']}', value='{e['value']}'"
            for e in entities
        )

        prompt = f"""Generate a Maestro YAML test case with the following specifications:

**Platform**: {platform}
**App ID**: {app_id}
**Test Description**: {description}

**Pre-extracted Actions** (use as guidance, adjust for Maestro syntax):
{entity_lines}

**Additional Requirements**:
- Use `extendedWaitUntil/visible` before interacting with dynamically loaded elements.
- Wrap potentially flaky steps in `- retry:` blocks with maxRetries: 3.
- Include reasonable timeout values (default 10000ms).
- For assertions, use `assertVisible` and `assertNotVisible`.
- Ensure YAML indentation is exactly 2 spaces per level.

Generate ONLY the YAML now:"""

        return prompt

    def _call_llm(self, user_prompt: str, model: str) -> str:
        """
        调用 AI 模型生成 YAML。

        使用 OpenAI SDK 的 chat.completions.create 方法发送对话请求：
        - system 角色：注入 MAESTRO_SYSTEM_PROMPT，定义 YAML 生成的所有语法规则和约束。
        - user 角色：传入 _build_user_prompt 构建的结构化测试描述。

        关键参数说明：
        - temperature=0.2：
          temperature 控制输出的随机性，范围 [0, 2]。设为 0.2（较低值）可使 LLM 输出更加
          确定性和可重复，减少 YAML 语法的随机变化。这对于代码/YAML 生成场景至关重要——
          我们需要稳定的、语法正确的输出，而非创意性文本。
        - max_tokens=4096：
          限制单次响应的最大 token 数（输入+输出）。4096 对于 Maestro YAML 测试用例足够，
          同时防止因 prompt 过长导致输出被截断。

        异常处理：
        - content 为 None：LLM 返回了空内容，抛出 ValueError。
        - 其他异常：统一包装为 RuntimeError，保留原始异常链。
        """
        try:
            response = self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": MAESTRO_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=4096,
            )
            content = response.choices[0].message.content
            if content is None:
                raise ValueError("LLM 返回空内容")
            return content

        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            raise RuntimeError(f"AI 生成失败: {e}") from e

    @staticmethod
    def _clean_yaml_output(raw: str) -> str:
        """
        清理 LLM 输出，移除 markdown fences 等干扰。

        LLM 经常在输出 YAML 时包上 markdown 代码块标记，即使 prompt 明确要求不要这样做。
        此处使用两条正则来处理：

        1. re.sub(r"^```(?:yaml)?\\s*\\n?", "", raw, flags=re.MULTILINE)
           - 匹配文本开头的 ``` 或 ```yaml 标记（可选语言标识符），后跟可选空白和换行。
           - re.MULTILINE 使 ^ 匹配每行的行首，而非整个字符串的开头。
           - 替换为空字符串，移除开头的代码块起始标记。

        2. re.sub(r"\\n?```\\s*$", "", raw, flags=re.MULTILINE)
           - 匹配文本结尾的 ``` 标记，前可能有换行，后有可选空白。
           - re.MULTILINE 使 $ 匹配每行的行尾。
           - 替换为空字符串，移除末尾的代码块结束标记。

        最后 strip() 去除首尾空白并追加一个换行符，保证文件格式整洁。
        """
        # 移除开头的 ```yaml 或 ``` 标记（支持语言标识符如 yaml/yml）
        raw = re.sub(r"^```(?:yaml)?\s*\n?", "", raw, flags=re.MULTILINE)
        # 移除末尾的 ``` 标记
        raw = re.sub(r"\n?```\s*$", "", raw, flags=re.MULTILINE)
        # 移除开头的空行，确保YAML从第一行即 appId: 开始
        raw = raw.strip() + "\n"
        return raw

    def validate_yaml(self, yaml_path: Path) -> bool:
        """
        验证生成的 YAML 文件语法是否正确。

        验证流程：
        1. 读取 YAML 文件内容（UTF-8编码）。
        2. 使用 yaml_lib.safe_load_all(content) 解析：Maestro YAML 可以包含多个
           由 `---` 分隔的 YAML 文档（每个文档是一个测试步骤块），因此使用
           safe_load_all 而非 safe_load 来正确解析多文档结构。
        3. 将解析结果的生成器转为 list，检查是否有文档被成功解析。
        4. 检查第一个文档是否为 dict 类型且包含 "appId" 键：
           - Maestro 测试脚本的第一个文档必须是 {"appId": "com.example.app"} 格式。
           - 如果第一个文档不是 dict 或缺少 appId 键，说明 YAML 不符合 Maestro 规范。

        Returns:
            True 如果 YAML 包含必要的字段（appId 必须存在于第一个文档中）
        """
        try:
            import yaml as yaml_lib
            content = yaml_path.read_text(encoding="utf-8")
            # safe_load_all 返回一个生成器，逐个产出由 --- 分隔的 YAML 文档。
            # 转为 list 后可检查文档数量和每个文档的内容。
            data = yaml_lib.safe_load_all(content)
            docs = list(data)
            if not docs:
                return False
            # 第一个文档必须是 {"appId": "..."} 格式的字典，
            # 且必须包含 appId 键，这是 Maestro 测试脚本的强制要求。
            first_doc = docs[0]
            return isinstance(first_doc, dict) and "appId" in first_doc
        except Exception as e:
            logger.warning(f"YAML 验证失败: {e}")
            return False
