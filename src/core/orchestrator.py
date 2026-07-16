"""
阶段四：智能重试与执行编排器 — MaestroOrchestrator

封装 Maestro CLI 命令，引入三层失败恢复机制 (L1 → L2 → L3)。
三层恢复机制概述：
  - L1：Maestro 原生重试 — 直接重新执行同一个 YAML，最多 max_retries 次。
  - L2：自愈重试 — 解析错误日志，通过 ElementHealer 模糊匹配 UI 层级树，
         找到替代定位器，原地修改 YAML 后重新执行。
  - L3：AI 自主修复 — 将错误日志 + 原始 YAML 发给大模型（DeepSeek/OpenAI），
         由 LLM 分析失败原因并生成修复后的 YAML，再用新 YAML 重新执行。
如果三层全部失败，则最终返回 FAILED 状态。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.healing import ElementHealer
from src.models.schemas import ExecutionResult, Platform, StepResult, TestStatus

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
# MaestroCommand — 命令包装器
# ═══════════════════════════════════════════

class MaestroCommand:
    """
    封装 Maestro CLI 子进程调用。

    职责：
    将 Maestro CLI 的各种操作（跑测试、启动 Studio、获取 UI 层级）包装为
    Python 方法，内部通过 subprocess 模块启动子进程执行 shell 命令，
    并统一处理超时、命令未找到等异常情况。

    使用方式：
        cmd = MaestroCommand("maestro")
        result = cmd.run_test(yaml_path, device_id="emulator-5554")
        # result 是 subprocess.CompletedProcess，包含 returncode/stdout/stderr
    """

    def __init__(self, maestro_path: str | None = None):
        """
        初始化 MaestroCommand，自动发现 maestro CLI 路径。

        查找顺序:
          1. 环境变量 MAESTRO_CLI_PATH
          2. npm 全局安装目录 (%APPDATA%/npm/maestro.cmd)
          3. 系统 PATH 中的 "maestro"

        Args:
            maestro_path: 手动指定 maestro CLI 路径，为 None 时自动发现。
        """
        self.maestro_path = maestro_path or self._find_maestro()

    @staticmethod
    def _find_maestro() -> str:
        """自动发现 maestro CLI 可执行文件路径。

        查找顺序:
          1. 环境变量 MAESTRO_CLI_PATH
          2. Windows: %APPDATA%/npm/maestro.cmd
          3. npm 全局目录 (where maestro)
          4. 回退到 "maestro"（依赖系统 PATH）
        """
        # 1. 环境变量优先
        env_path = os.environ.get("MAESTRO_CLI_PATH")
        if env_path and Path(env_path).exists():
            return env_path

        # 2. Windows: npm 全局目录
        default_npm = Path.home() / "AppData" / "Roaming" / "npm" / "maestro.cmd"
        if default_npm.exists():
            return str(default_npm)

        # 3. 尝试通过 which/where 查找
        import shutil
        found = shutil.which("maestro")
        if found:
            return found

        return "maestro"

    def run_test(
        self,
        yaml_path: Path,
        device_id: str | None = None,
        platform: str = "android",
        analyze: bool = True,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        """
        执行 `maestro test <yaml_path>` 命令，运行单个 YAML 测试用例。

        内部流程：
        1. 拼接命令行参数：maestro test <yaml> [--analyze] [-e DEVICE_ID=xxx] [--platform ios]
        2. 通过 subprocess.run 启动子进程，捕获 stdout/stderr
        3. 设置 cwd=yaml_path.parent，使 YAML 中的相对路径能正确解析
        4. 处理两种异常：
           - subprocess.TimeoutExpired：命令超时，构建一个 returncode=-1 的 CompletedProcess 返回
           - FileNotFoundError：maestro 命令未安装，抛出 RuntimeError

        Args:
            yaml_path: YAML 测试文件路径（Path 对象）
            device_id: 目标设备 ID（例如 "emulator-5554"），通过 -e 环境变量传递
            platform: 目标平台，"android" 或 "ios"
            analyze: 是否启用 --analyze 模式（Maestro 的分析模式会输出更详细的诊断信息）
            timeout: 命令超时时间（秒），默认 120 秒

        Returns:
            subprocess.CompletedProcess 对象：
            - returncode: 0 表示成功，非 0 表示失败，-1 表示超时
            - stdout: 标准输出（Maestro 的执行日志）
            - stderr: 标准错误（错误详情）
        """
        # ── 拼接 maestro test 命令 ──
        cmd = [self.maestro_path, "test", str(yaml_path)]

        # --analyze 启用 Maestro 分析模式，输出更详细的步骤执行信息
        if analyze:
            cmd.append("--analyze")

        # iOS 平台需要显式指定 --platform ios
        if platform == "ios":
            cmd.extend(["--platform", "ios"])

        logger.info(f"执行 Maestro 命令: {' '.join(cmd)}")

        # 设置子进程环境变量（Maestro 2.x 需要 JAVA_HOME）
        env = os.environ.copy()
        if not env.get("JAVA_HOME"):
            env["JAVA_HOME"] = r"C:\Program Files\Java\jdk-17"
        env.setdefault("MAESTRO_CLI_ANALYSIS_NOTIFICATION_DISABLED", "true")
        env.setdefault("MAESTRO_CLI_NO_APP_UNINSTALL", "true")

        try:
            # subprocess.run 启动子进程，同步等待完成
            # capture_output=True: 捕获 stdout 和 stderr 到内存
            # text=True: 以字符串（而非 bytes）返回输出
            # cwd=yaml_path.parent: 工作目录设为 YAML 所在目录，确保相对路径引用正确
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=yaml_path.parent,
                env=env,
            )
            logger.info(f"Maestro 退出码: {result.returncode}")
            if result.returncode != 0:
                logger.warning(f"Maestro stderr: {result.stderr[-500:]}")
            return result

        except subprocess.TimeoutExpired as e:
            # 超时异常：构造一个返回码为 -1 的 CompletedProcess，
            # stdout 取已捕获的部分输出，stderr 记录超时原因
            logger.error(f"Maestro 命令超时 ({timeout}s): {e}")
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=-1,
                stdout=e.stdout or "",
                stderr=str(e),
            )
        except FileNotFoundError:
            # maestro 命令不在 PATH 中，并且指定路径下也没有
            raise RuntimeError(
                f"未找到 maestro 命令。请确认 Maestro CLI 已安装，或设置 MAESTRO_CLI_PATH。"
            )

    def run_studio(self, output_path: Path | None = None) -> subprocess.Popen[str]:
        """
        启动 Maestro Studio 录制模式。

        Maestro Studio 是一个交互式 GUI 工具，用于录制和回放测试步骤。
        此方法通过 subprocess.Popen 以非阻塞方式启动该进程，
        返回 Popen 对象以便后续管理（如关闭 Studio）。

        Args:
            output_path: 录制输出的 YAML 文件路径（可选）

        Returns:
            subprocess.Popen 对象，调用方可通过 .terminate() 或 .kill() 关闭 Studio
        """
        cmd = [self.maestro_path, "studio"]
        logger.info(f"启动 Maestro Studio: {' '.join(cmd)}")
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def get_ui_hierarchy(
        self, device_id: str | None = None
    ) -> str | None:
        """
        获取当前屏幕的 UI 层级树（XML 格式）。

        实现方式：
        - Android 平台：通过 adb exec-out uiautomator dump 命令直接获取 UI 层级 XML。
          这比通过 maestro query 更直接、更可靠。
        - 如果指定了 device_id，则在 adb 命令中添加 -s 参数指定目标设备。
        - 此方法设计为非关键路径：获取失败时只记录 debug 日志并返回 None，
          不会中断主流程。

        Args:
            device_id: 目标设备 ID（例如 "emulator-5554"），多设备时必须指定

        Returns:
            成功时返回 UI 层级树 XML 字符串，失败时返回 None
        """
        # 使用 adb 直接获取 UI hierarchy（Android）
        # uiautomator dump /dev/tty 将 UI 层级 XML 输出到标准输出
        if device_id:
            cmd = ["adb", "-s", device_id, "exec-out", "uiautomator", "dump", "/dev/tty"]
        else:
            cmd = ["adb", "exec-out", "uiautomator", "dump", "/dev/tty"]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception as e:
            # 非关键路径：获取失败不影响主流程，记录 debug 级别日志即可
            logger.debug(f"获取 UI hierarchy 失败 (非关键): {e}")

        return None


# ═══════════════════════════════════════════
# 错误解析
# ═══════════════════════════════════════════

class ErrorParser:
    """
    解析 Maestro 输出中的错误信息。

    职责：
    从 Maestro 的 stderr 输出中自动识别错误类型，为后续的 L2 自愈和 L3 AI 修复
    提供决策依据。

    支持识别的错误类型：
    1. 元素未找到（ELEMENT_NOT_FOUND）：定位器选择器匹配不到任何 UI 元素
    2. 超时（TIMEOUT）：等待某个条件（如元素出现）超时
    3. 从错误日志中提取失败的定位器文本，供 ElementHealer 使用

    所有匹配均使用大小写不敏感的字符串包含匹配，
    模式列表同时覆盖英文和中文的 Maestro 错误信息。
    """

    # ── 元素未找到的错误模式 ──
    # 这些是 Maestro 在无法找到指定 UI 元素时输出的典型错误关键词。
    # 覆盖了 Maestro 原生英文错误和中文环境下的错误信息。
    ELEMENT_NOT_FOUND_PATTERNS = [
        "not found",              # 通用 "not found" 错误
        "No view matching",       # Maestro 的 "No view matching selector" 错误
        "Element not found",      # 显式的 "Element not found"
        "找不到元素",             # 中文环境下的元素未找到错误
        "unable to find",         # "unable to find element" 变体
        "could not find",         # "could not find element" 变体
    ]

    # ── 超时错误模式 ──
    # Maestro 等待元素出现或操作完成时可能触发的超时错误关键词
    TIMEOUT_PATTERNS = [
        "timeout",               # 通用 timeout
        "timed out",             # "timed out waiting for ..." 
        "TimeoutException",      # Java 异常类名（Maestro 底层是 JVM）
        "超时",                  # 中文环境下的超时错误
    ]

    @classmethod
    def is_element_not_found(cls, stderr: str) -> bool:
        """
        判断错误是否为「元素未找到」类型。

        将 stderr 转为小写后与所有 ELEMENT_NOT_FOUND_PATTERNS 做子串匹配，
        只要匹配到任意一个模式即返回 True。

        Args:
            stderr: Maestro 的标准错误输出字符串

        Returns:
            True 表示错误原因是找不到 UI 元素，False 表示其他原因
        """
        stderr_lower = stderr.lower()
        return any(p.lower() in stderr_lower for p in cls.ELEMENT_NOT_FOUND_PATTERNS)

    @classmethod
    def is_timeout(cls, stderr: str) -> bool:
        """
        判断错误是否为「超时」类型。

        同样使用大小写不敏感的子串匹配。

        Args:
            stderr: Maestro 的标准错误输出字符串

        Returns:
            True 表示错误原因是操作超时，False 表示其他原因
        """
        stderr_lower = stderr.lower()
        return any(p.lower() in stderr_lower for p in cls.TIMEOUT_PATTERNS)

    @classmethod
    def extract_failed_locator(cls, stderr: str) -> str | None:
        """
        从错误信息中提取失败的定位器（选择器文本）。

        解析策略：
        1. 用正则找出 stderr 中所有被引号（单引号或双引号）包裹的字符串。
        2. 过滤掉常见的非定位器关键词（如 "view"、"element"、"id"、"text"）。
        3. 返回第一个长度 > 1 的候选字符串作为失败定位器。

        使用场景：
        L2 自愈阶段，ElementHealer 需要知道是哪个定位器失败了，
        才能在 UI 层级树中搜索替代选择器。

        Args:
            stderr: Maestro 的标准错误输出字符串

        Returns:
            提取到的定位器文本（如 "登录按钮"），未找到时返回 None
        """
        import re
        # 匹配所有被单引号或双引号包裹的字符串
        matches = re.findall(r"['\"]([^'\"]+)['\"]", stderr)
        if matches:
            # 过滤掉常见非定位器字符串（Maestro 内部术语）
            skip = {"view", "element", "id", "text"}
            for m in matches:
                if m.lower() not in skip and len(m) > 1:
                    return m
        return None


# ═══════════════════════════════════════════
# L3 AI 修复 (通过 LLM 重写 YAML)
# ═══════════════════════════════════════════

class AIYamlFixer:
    """
    L3 级别的 AI 自主修复：将错误日志发给大模型重写 YAML 步骤。

    设计思路：
    当 L1（原生重试）和 L2（自愈模糊匹配）都失败后，将完整的错误日志
    和原始 YAML 内容发送给 LLM（默认使用 DeepSeek），由大模型分析失败原因
    并生成修复后的 YAML。

    提示词（Prompt）设计要点：
    1. 明确角色：告诉 LLM 它在调试一个 Maestro 移动端测试失败
    2. 提供完整上下文：错误日志 + 原始 YAML
    3. 给出修复建议方向：
       - 元素选择器错误 → 建议更健壮的替代方案
       - 时序问题 → 添加 extendedWaitUntil/visible 块
       - 元素变化 → 使用更宽泛的选择器（文本正则、索引等）
    4. 输出约束：只输出修正后的完整 YAML，不要解释

    模型配置：
    - 优先使用 DEEPSEEK_API_KEY 环境变量
    - 回退到 OPENAI_API_KEY
    - 默认 base_url 指向 DeepSeek API，兼容 OpenAI SDK
    """

    # ── L3 AI 修复提示词模板 ──
    # {error_log} 会被替换为 Maestro 的错误输出（截断到 3000 字符）
    # {original_yaml} 会被替换为原始 YAML 内容（截断到 5000 字符）
    # temperature=0.3 保证输出相对稳定，max_tokens=4096 足够容纳完整 YAML
    AI_FIX_PROMPT = """You are debugging a Maestro mobile test failure. Below is the error log and the original YAML test case.

## Error Log:
{error_log}

## Original YAML:
```yaml
{original_yaml}
```

## Task:
Analyze the error and rewrite the failing YAML steps. Output ONLY the corrected complete YAML (no explanations).
- If an element selector is wrong, suggest a more robust alternative.
- If timing is an issue, add extendedWaitUntil/visible blocks.
- If the element may have changed, use broader selectors (e.g., text regex, index).

Output the full corrected YAML now:"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        """
        初始化 AI 修复器。

        API 密钥获取优先级：
        1. 构造参数 api_key
        2. 环境变量 DEEPSEEK_API_KEY
        3. 环境变量 OPENAI_API_KEY

        base_url 默认指向 DeepSeek 的 OpenAI 兼容 API 端点。

        Args:
            api_key: LLM API 密钥（可选，不传则从环境变量读取）
            base_url: API 端点地址（可选，默认 DeepSeek）
        """
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        self.model = os.getenv("AI_MODEL", "deepseek-chat")

    def fix(
        self,
        error_log: str,
        yaml_path: Path,
    ) -> Path | None:
        """
        通过 AI 重写失败的 YAML 文件。

        执行流程：
        1. 读取原始 YAML 文件内容
        2. 初始化 OpenAI 客户端（指向 DeepSeek 或 OpenAI API）
        3. 构造提示词：将 error_log（截断到 3000 字符）和 original_yaml（截断到 5000 字符）
           填入 AI_FIX_PROMPT 模板
        4. 调用 LLM chat completion API，temperature=0.3 保证输出稳定
        5. 清理 LLM 输出：去掉可能的 markdown 代码块标记（```yaml 和 ```）
        6. 将修复后的 YAML 保存为 <原名>_ai_fixed.yaml（例如 login_ai_fixed.yaml）
        7. 返回新文件路径

        Args:
            error_log: Maestro 的完整错误输出（stdout + stderr）
            yaml_path: 原始 YAML 测试文件路径

        Returns:
            修复后的 YAML 文件 Path，失败时返回 None（包括 API 错误、网络问题、
            LLM 返回空内容等所有异常情况）
        """
        try:
            from openai import OpenAI
            original_yaml = yaml_path.read_text(encoding="utf-8")

            # 使用 OpenAI 兼容客户端连接 DeepSeek（或其他兼容 API）
            client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": self.AI_FIX_PROMPT.format(
                            error_log=error_log[-3000:],  # 限制长度，避免超出 token 限制
                            original_yaml=original_yaml[-5000:],  # 同样截断
                        ),
                    }
                ],
                temperature=0.3,   # 低温度保证输出更确定、更稳定
                max_tokens=4096,   # 足够生成完整的 YAML 文件
            )

            content = response.choices[0].message.content
            if content is None:
                return None

            # 清理输出：去掉 LLM 可能包裹的 markdown 代码块标记
            # 例如去掉开头的 ```yaml 和结尾的 ```
            import re
            content = re.sub(r"^```(?:yaml)?\s*\n?", "", content, flags=re.MULTILINE)
            content = re.sub(r"\n?```\s*$", "", content, flags=re.MULTILINE)

            # 保存修复版本：在同一目录下生成 <原名>_ai_fixed.yaml
            # with_stem 替换文件名主干（不含后缀），保留 .yaml 后缀
            fix_path = yaml_path.with_stem(yaml_path.stem + "_ai_fixed")
            fix_path.write_text(content.strip() + "\n", encoding="utf-8")
            logger.info(f"AI 修复的 YAML 已保存: {fix_path}")
            return fix_path

        except Exception as e:
            # 捕获所有异常（网络错误、API 限额、JSON 解析错误等），
            # AI 修复是尽力而为的增强手段，失败不应中断主流程
            logger.error(f"AI 修复失败: {e}")
            return None


# ═══════════════════════════════════════════
# MaestroOrchestrator — 主编排器
# ═══════════════════════════════════════════

class MaestroOrchestrator:
    """
    Maestro 执行编排器，实现三层失败恢复机制。

    ┌─────────────────────────────────────────────────────────────┐
    │                     execute() 执行流程                       │
    ├─────────────────────────────────────────────────────────────┤
    │  1. 构建初始 ExecutionResult（status=FAILED）               │
    │                                                             │
    │  2. ── L1 循环：Maestro 原生重试 ──                         │
    │     for attempt in 1..max_retries:                          │
    │         result = maestro.run_test(yaml_path)                │
    │         if result.returncode == 0 → PASSED，直接返回        │
    │     全部失败 → 进入 L2                                       │
    │                                                             │
    │  3. ── L2 循环：自愈重试（仅当错误为「元素未找到」时）────  │
    │     for attempt in 1..l2_max_retries:                       │
    │         ① ErrorParser.extract_failed_locator(stderr)        │
    │            → 提取失败的定位器文本                            │
    │         ② maestro.get_ui_hierarchy(device_id)               │
    │            → 获取当前屏幕 UI 层级树 XML                     │
    │         ③ healer.build_fingerprint_from_error(stderr)       │
    │            → 从错误日志构建元素指纹                          │
    │         ④ healer.heal(fingerprint, xml, yaml, locator)      │
    │            → 在 UI 树中模糊匹配替代定位器，原地更新 YAML    │
    │         ⑤ 如果 heal_result["healed"]:                       │
    │              maestro.run_test(yaml_path)  # 用修复后的 YAML │
    │              成功 → HEALED，返回                            │
    │     全部失败 → 进入 L3                                       │
    │                                                             │
    │  4. ── L3：AI 自主修复（可选，l3_enabled=True 时才执行）─── │
    │         ai_fixer.fix(error_log, yaml_path)                  │
    │         → LLM 分析错误日志 + 原始 YAML                      │
    │         → 生成 <原名>_ai_fixed.yaml                         │
    │         → maestro.run_test(ai_fixed.yaml)                   │
    │         成功 → HEALED，返回                                  │
    │                                                             │
    │  5. 三层全部失败 → status=FAILED，返回                      │
    └─────────────────────────────────────────────────────────────┘

    使用示例:
        orch = MaestroOrchestrator()
        result = orch.execute(
            yaml_path=Path("flows/login.yaml"),
            device_id="emulator-5554",
            heal=True,
        )
    """

    def __init__(
        self,
        maestro_path: str | None = None,
        l1_max_retries: int = 3,
        l2_max_retries: int = 2,
        l3_enabled: bool = True,
    ):
        """
        初始化编排器。

        组件依赖：
        - MaestroCommand：封装 maestro CLI 子进程调用
        - ElementHealer：L2 自愈，在 UI 层级树中模糊匹配替代元素定位器
        - AIYamlFixer：L3 AI 修复，通过 LLM 重写失败的 YAML

        Args:
            maestro_path: maestro CLI 路径，默认从环境变量 MAESTRO_CLI_PATH 读取，
                          回退到 PATH 中的 "maestro"
            l1_max_retries: L1 级别最大重试次数（默认 3 次）
            l2_max_retries: L2 级别自愈最大尝试次数（默认 2 次）
            l3_enabled: 是否启用 L3 AI 修复（默认 True）
        """
        # ── 初始化命令包装器 ──
        self.maestro = MaestroCommand(maestro_path or os.getenv("MAESTRO_CLI_PATH") or None)
        # ── 初始化元素自愈器（L2 使用）──
        self.healer = ElementHealer()
        # ── 初始化 AI 修复器（L3 使用，可通过 l3_enabled 关闭）──
        self.ai_fixer = AIYamlFixer() if l3_enabled else None
        self.l1_max_retries = l1_max_retries
        self.l2_max_retries = l2_max_retries
        self.l3_enabled = l3_enabled

    def execute(
        self,
        yaml_path: Path,
        device_id: str | None = None,
        platform: str = "android",
        heal: bool = True,
        max_retries: int = 3,
    ) -> ExecutionResult:
        """
        执行一个 YAML 测试用例，支持自动重试和自愈。

        完整执行流程参见类文档中的流程图。

        Args:
            yaml_path: YAML 文件路径
            device_id: 设备 ID（Android 模拟器如 "emulator-5554"）
            platform: 目标平台，"android" 或 "ios"
            heal: 是否启用 L2 自愈和 L3 AI 修复
            max_retries: L1 级别的最大重试次数

        Returns:
            ExecutionResult — 完整的执行结果，包含：
            - status: PASSED / HEALED / FAILED
            - duration_ms: 总耗时（毫秒）
            - raw_maestro_log: Maestro 完整输出日志
            - healed_steps: 被自愈修复的步骤数
        """
        # ── 记录测试开始时间，用于计算总耗时 ──
        start_time = datetime.now()

        # ── 构建初始结果对象，默认状态为 FAILED ──
        # 后续各层成功后会将 status 更新为 PASSED 或 HEALED
        result = ExecutionResult(
            test_name=yaml_path.stem,       # 测试名称 = YAML 文件名（不含后缀）
            platform=Platform(platform),
            device_id=device_id,
            status=TestStatus.FAILED,       # 初始假设失败，成功后覆盖
            start_time=start_time,
            yaml_path=yaml_path,
        )

        # ═══════════════════════════════════════════
        # L1: Maestro 原生重试
        # ═══════════════════════════════════════════
        # 策略：直接重新执行同一个 YAML 文件，不做任何修改。
        # 适用于网络波动、设备瞬时繁忙等临时性问题。
        # 每次重试都会捕获完整的 stdout + stderr 存入 result.raw_maestro_log。
        for attempt in range(1, max_retries + 1):
            logger.info(f"[L1 尝试 {attempt}/{max_retries}] 执行: {yaml_path.name}")
            proc = self.maestro.run_test(
                yaml_path=yaml_path,
                device_id=device_id,
                platform=platform,
                analyze=True,  # 启用分析模式以获取详细日志
            )

            # 保存原始日志供后续 L2/L3 分析使用
            result.raw_maestro_log = proc.stdout + "\n" + proc.stderr

            if proc.returncode == 0:
                # 测试通过！更新状态和时间，直接返回
                result.status = TestStatus.PASSED
                result.end_time = datetime.now()
                result.duration_ms = (result.end_time - start_time).total_seconds() * 1000
                logger.info(f"测试通过: {yaml_path.name}")
                return result

            logger.warning(f"[L1 尝试 {attempt}] 失败 (exit={proc.returncode})")

        # ═══════════════════════════════════════════
        # L2: 自愈重试（元素级智能修复）
        # ═══════════════════════════════════════════
        # 触发条件：
        #   1. heal=True（用户启用了自愈功能）
        #   2. ErrorParser 识别出错误类型为「元素未找到」
        #
        # 自愈流程：
        #   ① 从 stderr 中提取失败的定位器文本（如 "登录按钮"）
        #   ② 通过 adb 获取当前屏幕的 UI 层级树 XML
        #   ③ 从错误日志构建元素指纹（包含文本、ID、位置等特征）
        #   ④ 调用 ElementHealer.heal() 在 UI 树中模糊匹配相似元素
        #      - 如果找到相似度足够高的替代元素，原地修改 YAML 中的定位器
        #      - 返回 {"healed": True/False, ...}
        #   ⑤ 如果自愈成功，用修改后的 YAML 重新执行测试
        if heal and ErrorParser.is_element_not_found(result.raw_maestro_log or ""):
            for attempt in range(1, self.l2_max_retries + 1):
                logger.info(f"[L2 自愈尝试 {attempt}/{self.l2_max_retries}]")

                # 步骤①：从错误日志中提取失败的具体定位器
                failed_locator = ErrorParser.extract_failed_locator(
                    result.raw_maestro_log or ""
                )

                if failed_locator:
                    # 步骤②：获取当前屏幕的 UI 层级树
                    xml_content = self.maestro.get_ui_hierarchy(device_id)
                    if xml_content:
                        # 步骤③：构建元素指纹（用于模糊匹配）
                        fingerprint = self.healer.build_fingerprint_from_error(
                            result.raw_maestro_log or ""
                        )
                        # 步骤④：执行自愈 — 在 UI 树中搜索替代元素并更新 YAML
                        heal_result = self.healer.heal(
                            fingerprint=fingerprint,
                            xml_content=xml_content,
                            yaml_path=yaml_path,
                            failed_locator=failed_locator,
                        )

                        if heal_result["healed"]:
                            # 步骤⑤：自愈成功，用原地修改后的 YAML 重新执行
                            logger.info(f"[L2] 自愈成功，重跑测试")
                            proc = self.maestro.run_test(
                                yaml_path=yaml_path,
                                device_id=device_id,
                                platform=platform,
                                analyze=True,
                            )
                            if proc.returncode == 0:
                                # 自愈后测试通过，状态标记为 HEALED
                                result.status = TestStatus.HEALED
                                result.healed_steps = 1
                                result.end_time = datetime.now()
                                result.duration_ms = (
                                    result.end_time - start_time
                                ).total_seconds() * 1000
                                logger.info(f"自愈后测试通过: {yaml_path.name}")
                                return result

        # ═══════════════════════════════════════════
        # L3: AI 自主修复（LLM 重写 YAML）
        # ═══════════════════════════════════════════
        # 触发条件：
        #   1. l3_enabled=True 且 ai_fixer 初始化成功
        #   2. L1 和 L2 均已失败
        #
        # AI 修复流程：
        #   ① 将错误日志（截断到 5000 字符）+ 原始 YAML 发送给 LLM
        #   ② LLM 分析失败原因，生成修复后的完整 YAML
        #   ③ 清理 LLM 输出，保存为 <原名>_ai_fixed.yaml
        #   ④ 用新生成的 YAML 执行测试
        if self.l3_enabled and self.ai_fixer:
            logger.info("[L3] 启动 AI 自主修复")
            fixed_yaml = self.ai_fixer.fix(
                error_log=(result.raw_maestro_log or "")[-5000:],  # 截断日志避免超 token 限制
                yaml_path=yaml_path,
            )

            if fixed_yaml:
                # AI 成功生成了修复版 YAML，使用它重新执行测试
                logger.info(f"[L3] AI 修复完成，执行修复后的用例: {fixed_yaml.name}")
                proc = self.maestro.run_test(
                    yaml_path=fixed_yaml,
                    device_id=device_id,
                    platform=platform,
                    analyze=True,
                )
                if proc.returncode == 0:
                    # AI 修复后测试通过，状态标记为 HEALED
                    result.status = TestStatus.HEALED
                    result.end_time = datetime.now()
                    result.duration_ms = (
                        result.end_time - start_time
                    ).total_seconds() * 1000
                    logger.info(f"AI 修复后测试通过")
                    return result

        # ═══════════════════════════════════════════
        # 三层恢复全部失败 — 最终标记为 FAILED
        # ═══════════════════════════════════════════
        result.status = TestStatus.FAILED
        result.end_time = datetime.now()
        result.duration_ms = (result.end_time - start_time).total_seconds() * 1000
        logger.error(f"测试最终失败: {yaml_path.name} (L1+L2+L3 均未恢复)")
        return result

    def run_multiple(
        self,
        yaml_paths: list[Path],
        device_id: str | None = None,
        platform: str = "android",
        heal: bool = True,
    ) -> list[ExecutionResult]:
        """
        批量执行多个 YAML 测试用例。

        依次调用 execute() 执行每个 YAML 文件，收集所有结果。
        每个用例独立执行，前一个用例的失败不影响后续用例。

        Args:
            yaml_paths: YAML 文件路径列表
            device_id: 设备 ID
            platform: android / ios
            heal: 是否启用自愈

        Returns:
            ExecutionResult 列表，与输入顺序一一对应
        """
        results: list[ExecutionResult] = []
        for yaml_path in yaml_paths:
            result = self.execute(
                yaml_path=yaml_path,
                device_id=device_id,
                platform=platform,
                heal=heal,
            )
            results.append(result)
        return results
