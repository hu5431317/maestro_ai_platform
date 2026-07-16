"""
阶段二：AI 自愈引擎 — ElementHealer

当 Maestro 执行因找不到元素而失败时，自动构建备选定位器并通过
模糊匹配在 UI 层级树中寻找最相似元素，最终动态更新 YAML 定位器。

=============================================================================
模块整体架构概览：
  本模块实现了 Maestro 自动化测试的"自愈"（self-healing）能力。当 UI 元素
  因页面变更导致原始定位器失效时，自愈引擎会自动：
    1. 从失败信息中解析出目标元素的"指纹"（text/resource-id/content-desc等属性）
    2. 基于指纹生成多种备选定位策略（模糊文本、XPath、class匹配等）
    3. 使用 difflib.SequenceMatcher 在当前 UI 层级树（XML）中进行模糊匹配
    4. 找到最佳匹配后，动态修改原始 YAML 测试脚本中的定位器

  核心算法选择：
    - difflib.SequenceMatcher：基于 Gestalt 模式匹配算法，计算两个字符串的
      相似度比率（0.0~1.0）。相比 Levenshtein 编辑距离，SequenceMatcher
      对字符串局部偏移和缺失有更好的容忍度，更适合 UI 元素属性文本的模糊匹配。
    - 多策略定位器生成：不依赖单一 XPath 或 ID，而是生成优先级排序的备选列表，
      确保即使在极端情况下（如 ID 完全变化）仍能通过 class 匹配或索引兜底。
=============================================================================
"""

from __future__ import annotations

import difflib       # 用于模糊字符串匹配的核心库
import logging       # 记录自愈过程的日志
import re            # 正则表达式，用于从 Maestro 错误信息中提取元素属性
import xml.etree.ElementTree as ET  # 解析 Android UI Automator 导出的 XML 层级树
from dataclasses import dataclass, field  # 定义轻量级数据容器
from pathlib import Path   # 跨平台处理 YAML 文件路径
from typing import Any      # 泛型类型标注

import yaml           # 读写 Maestro 的 YAML 格式测试脚本

from src.models.schemas import ElementFingerprint, LocatorStrategy

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
# 元素指纹构建
# ═══════════════════════════════════════════

@dataclass
class ElementFingerprintInternal:
    """
    内部使用的元素指纹数据类（与 Pydantic 模型互补）。

    【类的目的】
      作为自愈引擎内部统一的元素描述数据结构。与 Pydantic 的 ElementFingerprint
      模型形成"内外分离"：外部 API 使用 Pydantic 模型（带验证），内部引擎使用
      此 dataclass（更轻量、无验证开销）。

    【在自愈管道中的角色】
      元素指纹是整个自愈流程的"输入起点"——无论从 Maestro 错误消息解析、
      从字典构造、还是从 API 传入，最终都标准化为此数据类，供后续的
      LocatorStrategyGenerator 和 FuzzyElementMatcher 使用。

    【关键属性说明】
      - text:          元素的显示文本，如 "登录"
      - resource_id:   Android 资源 ID，如 "com.example:id/login_btn"
      - content_desc:  无障碍描述（content-description），用于辅助功能
      - xpath:         元素在 XML 层级树中的完整路径
      - class_name:    Android View 类名，如 "android.widget.Button"
      - index:         元素在兄弟节点中的序号

    【与 Pydantic 模型的关系】
      - from_pydantic()：将外部 Pydantic 模型转换为内部 dataclass
      - to_pydantic()：  将内部 dataclass 转回 Pydantic 模型（用于 API 响应）
      - from_dict()：    从字典（如 JSON 解析结果）构造指纹
      - to_dict()：      序列化为字典，key 名使用 Android 惯用的连字符格式
    """
    text: str | None = None
    resource_id: str | None = None
    content_desc: str | None = None
    xpath: str | None = None
    class_name: str | None = None
    index: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ElementFingerprintInternal":
        """
        从字典构造元素指纹。

        【参数】data：可能包含 'text', 'resource-id'/'resource_id', 'content-desc'/'content_desc',
                     'xpath', 'class', 'index' 键的字典，通常来自 JSON 解析或 API 请求。
        【返回值】ElementFingerprintInternal 实例。
        【逻辑】
          1. 对 resource-id 和 content-desc 同时支持连字符和蛇形命名（兼容 Android 惯例与 Python 惯例）。
          2. 其余字段直接取值，若键不存在则 get() 返回 None。
        """
        return cls(
            text=data.get("text"),
            resource_id=data.get("resource-id") or data.get("resource_id"),
            content_desc=data.get("content-desc") or data.get("content_desc"),
            xpath=data.get("xpath"),
            class_name=data.get("class"),
            index=data.get("index"),
        )

    @classmethod
    def from_pydantic(cls, fp: ElementFingerprint) -> "ElementFingerprintInternal":
        """
        从 Pydantic ElementFingerprint 模型转换为内部 dataclass。

        【参数】fp：经过 Pydantic 校验的 ElementFingerprint 实例。
        【返回值】ElementFingerprintInternal 实例。
        【逻辑】逐个字段平铺复制，消除 Pydantic 模型带来的额外开销。
        """
        return cls(
            text=fp.text,
            resource_id=fp.resource_id,
            content_desc=fp.content_desc,
            xpath=fp.xpath,
            class_name=fp.class_name,
            index=fp.index,
        )

    def to_pydantic(self) -> ElementFingerprint:
        """
        将内部 dataclass 转换回 Pydantic ElementFingerprint 模型。

        【返回值】ElementFingerprint 实例，可用于 API 序列化响应。
        【用途】当自愈结果需要通过 FastAPI 返回给调用方时使用。
        """
        return ElementFingerprint(
            text=self.text,
            resource_id=self.resource_id,
            content_desc=self.content_desc,
            xpath=self.xpath,
            class_name=self.class_name,
            index=self.index,
        )

    def to_dict(self) -> dict[str, Any]:
        """
        序列化为字典，key 名采用 Android 惯用的连字符格式。

        【返回值】包含 'text', 'resource-id', 'content-desc', 'xpath', 'class', 'index' 的字典。
        【用途】用于日志输出、调试打印，以及传递给 _build_text_repr() 构建相似度比较文本。
        """
        return {
            "text": self.text,
            "resource-id": self.resource_id,
            "content-desc": self.content_desc,
            "xpath": self.xpath,
            "class": self.class_name,
            "index": self.index,
        }


# ═══════════════════════════════════════════
# 备选定位器生成
# ═══════════════════════════════════════════

class LocatorStrategyGenerator:
    """
    根据原始元素指纹生成优先级排序的备选定位策略列表。

    【类的目的】
      原始定位器失效后，单一的"重试"毫无意义。本类基于元素的多种属性（text、
      content-desc、class、resource-id）生成 3 种以上的备选定位方式，形成
      策略梯度，确保至少有一种方式能定位到目标元素。

    【算法思想】
      采用"从精确到模糊、从语义到结构"的退化策略：
        优先级 1（最精确）：text 模糊匹配 — 文本是最稳定的 UI 语义标识
        优先级 2：content-desc 或 resource-id 部分匹配 — 辅助标识
        优先级 3：class 类型匹配 — 回退到同类元素
        优先级 4+：fallback_index — 索引兜底，确保至少返回 3 个策略

    【在自愈管道中的角色】
      位于 Step 2：接收标准化指纹 → 生成备选策略列表 → 传递给 FuzzyElementMatcher
      用于在 XML 树中筛选候选元素。
    """

    @staticmethod
    def generate(fingerprint: ElementFingerprintInternal) -> list[LocatorStrategy]:
        """
        根据元素指纹生成优先级排序的备选定位策略列表。

        【参数】fingerprint：标准化的元素指纹，包含 text/resource_id/content_desc/class_name/index。
        【返回值】按 priority 升序排列的 LocatorStrategy 列表，至少包含 3 个策略。

        【生成逻辑 — 五种策略依次判断】
          策略 1 — text_fuzzy（优先级 1）：
            - 条件：fingerprint.text 不为空
            - 定位器：XPath 表达式 contains(@text, "文本内容")
            - 原理：使用 XPath 的 contains() 函数做子串匹配，比精确匹配更能容忍
                    文本的微小变化（如前后多出空格、文案微调）
            - 适用场景：按钮文本、标签文本等语义明确的元素

          策略 2 — xpath_contains / resource_id_partial（优先级 2）：
            - 条件 A：fingerprint.content_desc 不为空
              → 生成 xpath_contains 策略：//*[contains(@content-desc, "...")]
            - 条件 B：fingerprint.resource_id 不为空 且 text 为空（避免与策略1重叠）
              → 生成 resource_id_partial 策略：提取 resource-id 的最后一段
                （如 "com.example:id/login_btn" → "login_btn"）
            - 原理：content-desc 常用于无障碍场景，稳定性较高；
                    resource-id 的部分匹配可在 ID 前缀变化时仍能命中

          策略 3 — class_match（优先级 3）：
            - 条件：fingerprint.class_name 不为空
            - 定位器：class 全限定名的最后一段（如 "android.widget.Button" → "Button"）
            - 原理：当文本和 ID 都不可用时，退回到元素类型匹配

          兜底 — fallback_index（优先级 4+）：
            - 条件：当前策略数不足 3 个
            - 定位器：index:N（N 为 fingerprint.index 或 0）
            - 原理：使用元素在父节点中的序号定位，是最不精确但始终可用的兜底方案

        【排序保证】最终按 priority 升序排列，确保使用时优先尝试高精度的策略。
        """
        strategies: list[LocatorStrategy] = []

        # 策略 1: text 模糊匹配 (contains)
        # 使用 XPath contains() 做子串匹配，比精确匹配更健壮
        if fingerprint.text:
            strategies.append(LocatorStrategy(
                strategy_type="text_fuzzy",
                locator=f'contains(@text, "{fingerprint.text}")',
                priority=1,
                description=f"通过 text 模糊匹配: {fingerprint.text}",
            ))

        # 策略 2: 基于 content-desc 的 contains XPath
        # content-desc 是无障碍描述，在按钮/图标等元素上稳定性较好
        if fingerprint.content_desc:
            strategies.append(LocatorStrategy(
                strategy_type="xpath_contains",
                locator=f'//*[contains(@content-desc, "{fingerprint.content_desc}")]',
                priority=2,
                description=f"通过 content-desc 包含匹配: {fingerprint.content_desc}",
            ))

        # 策略 3: 基于 class 的同类型元素匹配
        # 截取 class 全限定名的最后一段（简单类名）作为定位器
        if fingerprint.class_name:
            class_short = fingerprint.class_name.split(".")[-1]
            strategies.append(LocatorStrategy(
                strategy_type="class_match",
                locator=class_short,
                priority=3,
                description=f"通过 class 类型匹配: {class_short}",
            ))

        # 如果没有文本属性，补充基于 resource-id 部分匹配的策略
        # 提取 resource-id 最后一段（如 "com.example:id/login_btn" → "login_btn"）
        # 条件：resource_id 存在 且 text 为空（避免与 text_fuzzy 策略重复）
        if fingerprint.resource_id and not fingerprint.text:
            rid_parts = fingerprint.resource_id.split("/")
            if len(rid_parts) > 1:
                rid_short = rid_parts[-1]
                strategies.append(LocatorStrategy(
                    strategy_type="resource_id_partial",
                    locator=rid_short,
                    priority=2,
                    description=f"通过 resource-id 部分匹配: {rid_short}",
                ))

        # 确保至少有 3 个策略（不足则补充通用索引策略作为兜底）
        # fallback_index 是利用元素在兄弟节点中的序号进行定位，几乎总是可用但最不精确
        while len(strategies) < 3:
            idx = len(strategies) + 1
            strategies.append(LocatorStrategy(
                strategy_type="fallback_index",
                locator=f"index:{fingerprint.index or 0}",
                priority=idx + 3,
                description="通用索引备选策略",
            ))

        # 按优先级升序排列，确保调用方按 priority 从小到大依次尝试
        return sorted(strategies, key=lambda s: s.priority)


# ═══════════════════════════════════════════
# 模糊匹配器
# ═══════════════════════════════════════════

class FuzzyElementMatcher:
    """
    利用 difflib.SequenceMatcher 在 XML UI 层级树中寻找与目标指纹最相似的元素。

    【类的目的】
      当需要判断"当前页面上哪个元素最像之前那个找不到的元素"时，本类提供
      基于字符串相似度的模糊匹配能力。这是自愈引擎的核心判定模块。

    【算法详解 — difflib.SequenceMatcher 模糊匹配】
      difflib 是 Python 标准库模块，SequenceMatcher 是其中的核心类。它基于
      Ratcliff/Obershelp 算法（又称 Gestalt 模式匹配），核心思想是：

        1. 寻找两个字符串 S1 和 S2 的最长公共子序列（longest contiguous matching
           subsequence，不含"junk"元素）。
        2. 递归地在匹配子序列的左右两侧重复寻找最长公共子序列。
        3. 将所有匹配子序列的总长度 × 2 ÷ (len(S1) + len(S2)) 得到相似度比率 ratio。

      该算法的关键特性：
        - ratio 值范围 [0.0, 1.0]，1.0 表示完全相同
        - 对字符级别的插入、删除、替换有一定容忍度
        - 比简单的编辑距离更适合"两个文本片段有多接近"的语义判断
        - 计算复杂度 O(N*M)，但对于短文本（UI 属性通常 < 200 字符）性能足够

    【在本模块中的匹配流程】
      1. _extract_nodes()：从 XML 层级树中提取所有带属性的节点
      2. _build_text_repr()：为每个节点构建"文本表示"（拼接 text/resource-id/
         content-desc/class 四个属性值）
      3. find_best_match()：遍历所有节点，分别对每个节点和目标指纹计算
         SequenceMatcher 相似度，记录最高分
      4. 若最高分 ≥ SIMILARITY_THRESHOLD（默认 0.85），返回匹配节点；否则返回 None

    【在自愈管道中的角色】
      位于 Step 3：接收备选策略列表和 XML 层级树 → 在树中寻找与指纹最相似的元素 →
      返回匹配节点的属性字典，供 YamlUpdater 提取新定位器。
    """

    # 相似度阈值：只有 ratio ≥ 0.85 的匹配才被认为"足够相似"
    # 该值经实验调优：过低会产生大量误匹配，过高则导致真实匹配被拒绝
    SIMILARITY_THRESHOLD = 0.85

    @staticmethod
    def _extract_nodes(xml_content: str) -> list[dict[str, Any]]:
        """
        从 XML 层级树字符串中提取所有带属性的节点。

        【参数】xml_content：Android UI Automator 导出的 UI 层级树 XML 字符串。
        【返回值】节点字典列表，每个字典包含节点的所有 XML 属性，外加 _index（全局序号）和
                 _tag（标签名）。无属性节点会被跳过。
        【逻辑】
          1. 使用 xml.etree.ElementTree.fromstring() 解析 XML 字符串。
          2. 调用 root.iter() 深度优先遍历整棵树的所有元素。
          3. 对每个元素提取 attrib（属性字典），若为空则跳过（空节点无匹配价值）。
          4. 为每个有效节点附加 _index（遍历序号，用于定位）和 _tag（XML 标签名）。
        【异常处理】
          若 XML 格式错误导致 ParseError，记录警告日志并返回空列表。
        """
        nodes: list[dict[str, Any]] = []
        try:
            # 解析 XML 层级树字符串为 ElementTree 对象
            root = ET.fromstring(xml_content)
            # iter() 深度优先遍历所有子元素
            for i, elem in enumerate(root.iter()):
                attrs = dict(elem.attrib)
                if attrs:  # 跳过没有属性的节点
                    attrs["_index"] = i       # 记录全局遍历序号
                    attrs["_tag"] = elem.tag  # 记录 XML 标签名
                    nodes.append(attrs)
        except ET.ParseError as e:
            logger.warning(f"XML 解析失败: {e}")
        return nodes

    @staticmethod
    def _build_text_repr(node: dict[str, Any]) -> str:
        """
        构建节点的文本表示，用于 difflib 相似度比较。

        【参数】node：节点属性字典（来自 _extract_nodes() 或 fingerprint.to_dict()）。
        【返回值】由 text、resource-id、content-desc、class 四个属性值拼接而成的字符串，
                 各值之间以空格分隔。若所有属性均为空，返回空字符串。

        【设计考量 — 为什么选择这四个属性】
          - text：用户可见文本，是最核心的元素语义标识
          - resource-id：开发者定义的 ID，虽可能变化但唯一性强
          - content-desc：无障碍描述，在图标按钮上尤为关键
          - class：元素类型，提供结构信息
          这四个属性共同构成元素的"语义向量"，将它们拼接为单一字符串后，
          SequenceMatcher 可以自然地捕捉到多属性组合的相似性。

        【与 difflib 的配合】
          SequenceMatcher.ratio() 的两个参数就是两个字符串。本方法将结构化的
          节点属性"扁平化"为字符串，使其可以直接传入 SequenceMatcher。
          例如：节点 {"text":"登录","class":"Button"} → "登录 Button"
               指纹 {"text":"登 录","class":"Button"} → "登 录 Button"
               两者 ratio 约为 0.92，超过阈值 0.85，视为匹配成功。
        """
        parts: list[str] = []
        # 按顺序提取四个关键属性，忽略 None/空值
        for key in ("text", "resource-id", "content-desc", "class"):
            if val := node.get(key):
                parts.append(val)
        # 用空格拼接所有非空属性值
        return " ".join(parts)

    @classmethod
    def find_best_match(
        cls,
        xml_content: str,
        fingerprint: ElementFingerprintInternal,
        threshold: float | None = None,
    ) -> dict[str, Any] | None:
        """
        在 XML UI 层级树中寻找与目标指纹相似度最高的元素。

        【参数】
          - xml_content：当前页面的 UI 层级树 XML 字符串。
          - fingerprint：目标元素的标准化指纹。
          - threshold：相似度阈值，若为 None 则使用类常量 SIMILARITY_THRESHOLD (0.85)。

        【返回值】
          最佳匹配节点的属性字典（包含 text/resource-id/content-desc/class 等），
          若没有节点达到阈值则返回 None。

        【匹配算法 — 逐步详解】
          第 1 步：解析 XML 树
            调用 _extract_nodes(xml_content) 获取所有带属性节点的列表。
            若无节点，返回 None。

          第 2 步：构建目标文本表示
            调用 _build_text_repr(fingerprint.to_dict()) 将元素指纹的四个关键属性
            拼接为字符串。若指纹无任何可用属性（如空指纹），返回 None。

          第 3 步：遍历所有节点，计算相似度
            对每个节点：
              a. 调用 _build_text_repr(node) 构建该节点的文本表示
              b. 若节点文本表示为空，跳过
              c. 创建 difflib.SequenceMatcher(None, target_repr, node_repr)
                 - 第一个参数 None 表示不忽略任何字符（isjunk=None）
                 - ratio() 返回 0.0~1.0 的相似度比率
              d. 若当前 ratio > best_ratio，更新最佳匹配

          第 4 步：阈值判定
            若 best_ratio >= threshold，返回最佳匹配节点
            若 best_ratio < threshold，记录警告日志后返回 None
        """
        if threshold is None:
            threshold = cls.SIMILARITY_THRESHOLD

        # 第 1 步：从 XML 层级树提取所有带属性节点
        nodes = cls._extract_nodes(xml_content)
        if not nodes:
            logger.warning("UI 层级树中无可解析的节点")
            return None

        # 第 2 步：将目标指纹转换为文本表示（用于相似度比较）
        target_repr = cls._build_text_repr(fingerprint.to_dict())
        if not target_repr:
            logger.warning("指纹无可用的匹配属性")
            return None

        # 第 3 步：遍历所有 XML 节点，寻找与目标指纹最相似的元素
        best_match: dict[str, Any] | None = None
        best_ratio: float = 0.0

        for node in nodes:
            # 构建当前节点的文本表示
            node_repr = cls._build_text_repr(node)
            if not node_repr:
                continue

            # 使用 difflib.SequenceMatcher 计算字符串相似度
            # SequenceMatcher(None, a, b)：第一个参数 isjunk=None 表示
            # 不忽略任何字符，所有字符都参与匹配计算
            # .ratio() 返回 2.0 * M / T，其中 M 是匹配的字符总数，
            # T 是两个字符串的字符总数之和
            ratio = difflib.SequenceMatcher(None, target_repr, node_repr).ratio()

            # 记录全局最高相似度及其对应节点
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = node

        # 第 4 步：阈值判定 — 只有相似度达到阈值才认为匹配成功
        if best_match and best_ratio >= threshold:
            logger.info(
                f"模糊匹配成功: ratio={best_ratio:.2%}, "
                f"matched={cls._build_text_repr(best_match)}"
            )
            return best_match

        if best_match:
            logger.warning(
                f"最佳匹配相似度过低: ratio={best_ratio:.2%} < threshold={threshold:.0%}"
            )
        return None


# ═══════════════════════════════════════════
# YAML 动态更新
# ═══════════════════════════════════════════

class YamlUpdater:
    """
    动态修改 Maestro YAML 测试文件中的定位器并保存。

    【类的目的】
      自愈引擎找到新的有效定位器后，需要将变更持久化到原始的 YAML 测试脚本中。
      本类负责安全地完成这一文件修改操作。

    【在自愈管道中的角色】
      位于 Step 5（最后一步）：接收旧/新定位器和 YAML 文件路径 → 备份原文件 →
      文本替换 → 写回文件。

    【备份策略】
      在修改前自动创建 .bak 备份文件，确保自愈操作可回滚。
      备份路径 = 原路径 + ".bak"（例如 test.yaml → test.yaml.bak）。
    """

    @staticmethod
    def update_locator(
        yaml_path: Path,
        old_locator: str,
        new_locator: str,
        backup: bool = True,
    ) -> bool:
        """
        替换 YAML 文件中的定位器字符串。

        【参数】
          - yaml_path：Maestro YAML 测试脚本的路径（Path 对象）。
          - old_locator：旧的（已失效的）定位器字符串，将被替换。
          - new_locator：新的（自愈生成的）定位器字符串，用于替换。
          - backup：是否在修改前创建备份文件（默认 True）。

        【返回值】
          True 表示更新成功，False 表示更新失败（原因包括：文件不存在、
          旧定位器未找到、写入权限不足等）。

        【替换逻辑 — 逐步详解】
          第 1 步：读取文件内容
            以 UTF-8 编码读取整个 YAML 文件的文本内容。
            注意：此处使用的是原始文本读取而非 YAML 解析，原因是
            YAML 解析→修改→序列化 会丢失注释、自定义格式和空行，
            直接文本替换则最大程度保留原始文件格式。

          第 2 步：检查旧定位器是否存在
            使用 Python 字符串的 in 操作符检查 old_locator 是否在文件内容中。
            若不存在，记录警告并返回 False（避免无意义的空替换或误替换）。

          第 3 步：创建备份（可选）
            若 backup=True：
              - 构造备份路径：原路径 + ".bak"（如 test.yaml → test.yaml.bak）
              - 将当前文件内容原封不动写入备份文件
              - 记录备份成功日志
            备份文件与原始文件在同一目录，可在出现问题时手动回滚。

          第 4 步：执行替换并写回
            使用 str.replace() 将所有出现的 old_locator 替换为 new_locator，
            然后将新内容以 UTF-8 编码写回原文件。

        【异常处理】
          任何异常（IOError、PermissionError 等）都会被捕获，记录错误日志后返回 False。
        """
        try:
            # 第 1 步：以 UTF-8 编码读取整个 YAML 文件的原始文本
            content = yaml_path.read_text(encoding="utf-8")

            # 第 2 步：确保旧定位器确实存在于文件中（防止误替换）
            if old_locator not in content:
                logger.warning(f"在 YAML 中未找到旧定位器: {old_locator}")
                return False

            # 第 3 步：创建备份文件（.bak 后缀），确保可回滚
            if backup:
                backup_path = yaml_path.with_suffix(yaml_path.suffix + ".bak")
                backup_path.write_text(content, encoding="utf-8")
                logger.info(f"已备份原 YAML 至: {backup_path}")

            # 第 4 步：执行字符串替换并写回原文件
            # 使用 str.replace() 而非 YAML 序列化，以保留注释和格式
            new_content = content.replace(old_locator, new_locator)
            yaml_path.write_text(new_content, encoding="utf-8")
            logger.info(f"YAML 定位器已更新: '{old_locator}' → '{new_locator}'")
            return True

        except Exception as e:
            logger.error(f"更新 YAML 定位器失败: {e}")
            return False

    @staticmethod
    def parse_locators_from_yaml(yaml_path: Path) -> list[str]:
        """
        从 YAML 文件中提取所有 Maestro 命令使用的定位器字符串。

        【参数】yaml_path：Maestro YAML 测试脚本路径。
        【返回值】定位器字符串列表（去重未做，保留原始顺序）。

        【提取逻辑】
          支持的 Maestro 命令及其定位器格式：
            - tapOn: "登录按钮"              → 直接字符串定位器
            - tapOn: { text: "登录按钮" }    → 嵌套字典，取 "text" 键
            - tapOn: { id: "login_btn" }     → 嵌套字典，取 "id" 键
            - assertVisible                 → 同上格式
            - assertNotVisible              → 同上格式
            - inputText                     → 同上格式

          遍历 YAML 中的每个步骤（step），如果步骤命令属于上述六种之一，
          则根据值的类型提取定位器字符串。

        【异常处理】
          YAML 格式错误或文件不存在时记录错误日志，返回空列表。
        """
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)

            # 确保顶层是列表（Maestro YAML 的根结构是命令列表）
            if not isinstance(data, list):
                return []

            locators: list[str] = []
            for step in data:
                if isinstance(step, dict):
                    for cmd, val in step.items():
                        # 只提取定位相关命令中的定位器
                        if cmd in ("tapOn", "assertVisible", "assertNotVisible", "inputText"):
                            # 格式 1：直接字符串，如 tapOn: "登录"
                            if isinstance(val, str):
                                locators.append(val)
                            # 格式 2：text 嵌套，如 tapOn: { text: "登录" }
                            elif isinstance(val, dict) and "text" in val:
                                locators.append(val["text"])
                            # 格式 3：id 嵌套，如 tapOn: { id: "com.example:id/btn" }
                            elif isinstance(val, dict) and "id" in val:
                                locators.append(val["id"])
            return locators
        except Exception as e:
            logger.error(f"解析 YAML 定位器失败: {e}")
            return []


# ═══════════════════════════════════════════
# ElementHealer — 主类
# ═══════════════════════════════════════════

class ElementHealer:
    """
    AI 自愈引擎主类 —— 整个自愈管道的编排器。

    【类的目的】
      将元素指纹标准化、备选策略生成、模糊匹配、YAML 更新四个模块串联为
      一个完整的自愈工作流。外部只需调用 heal() 方法即可完成全流程。

    【整体工作流（heal() 方法的 4+1 步）】
      Step 1 — 标准化指纹（normalize fingerprint）
               将不同来源的元素描述（dict / Pydantic 模型 / 内部 dataclass）
               统一转换为 ElementFingerprintInternal。
      Step 2 — 生成备选定位策略（generate alternatives）
               调用 LocatorStrategyGenerator.generate() 基于指纹属性生成
               至少 3 种备选策略（text_fuzzy / xpath_contains / class_match /
               resource_id_partial / fallback_index）。
      Step 3 — 模糊匹配（fuzzy match）
               调用 FuzzyElementMatcher.find_best_match() 在当前 UI 层级树
               XML 中寻找与目标指纹相似度 ≥ threshold 的最佳匹配节点。
      Step 4 — 提取新定位器 + 更新 YAML（update YAML）
               从匹配节点提取最可用的定位器（优先 resource-id > text >
               content-desc > 备选策略），然后调用 YamlUpdater.update_locator()
               将 YAML 中的旧定位器替换为新定位器。

    【关键属性】
      - similarity_threshold：模糊匹配的相似度阈值（默认 0.85）
      - max_alternatives：最大备选策略数（默认 3）
      - strategy_gen：LocatorStrategyGenerator 实例
      - matcher：FuzzyElementMatcher 实例
      - updater：YamlUpdater 实例

    【错误处理】
      每个步骤失败时都会设置 result["message"] 并返回 result，不会抛出异常。
      调用方通过 result["healed"] 布尔值判断自愈是否成功。
    """

    def __init__(
        self,
        similarity_threshold: float = 0.85,
        max_alternatives: int = 3,
    ):
        """
        初始化自愈引擎。

        【参数】
          - similarity_threshold：模糊匹配的相似度阈值，范围建议 0.75~0.95。
                                  值越高匹配越严格（可能漏掉真实匹配），
                                  值越低匹配越宽松（可能产生误匹配）。
          - max_alternatives：最大备选策略数量，影响失败后的重试次数。
        """
        self.similarity_threshold = similarity_threshold
        self.max_alternatives = max_alternatives
        # 初始化三个子模块：策略生成器、模糊匹配器、YAML 更新器
        self.strategy_gen = LocatorStrategyGenerator()
        self.matcher = FuzzyElementMatcher()
        self.updater = YamlUpdater()

    def heal(
        self,
        fingerprint: ElementFingerprint | ElementFingerprintInternal | dict[str, Any],
        xml_content: str,
        yaml_path: Path,
        failed_locator: str,
    ) -> dict[str, Any]:
        """
        执行完整的自愈流程（4 步工作流）。

        【参数】
          - fingerprint：失败元素的指纹，支持三种输入类型：
              * dict：从 JSON/API 直接传入的字典
              * ElementFingerprint：Pydantic 模型（API 层使用）
              * ElementFingerprintInternal：内部 dataclass（引擎内部流转）
          - xml_content：当前页面的 UI 层级树 XML 字符串（Android UI Automator 格式）。
          - yaml_path：需要修复的 Maestro YAML 测试脚本文件路径。
          - failed_locator：原始的（已失效的）定位器字符串，如 "登录按钮"。

        【返回值】
          dict 包含以下字段：
            - "healed" (bool)：自愈是否成功
            - "new_locator" (str | None)：自愈生成的新定位器
            - "alternatives" (list)：生成的备选定位策略列表，每项含 type 和 locator
            - "message" (str)：自愈过程的描述信息（成功/失败原因）

        【4 步工作流详解】

        ┌──────────────────────────────────────────────────────────────┐
        │ Step 1 — 标准化指纹（normalize fingerprint）                  │
        │   根据输入类型调用对应的转换方法，统一为                        │
        │   ElementFingerprintInternal 实例。                           │
        │   - dict → ElementFingerprintInternal.from_dict()            │
        │   - ElementFingerprint → from_pydantic()                     │
        │   - ElementFingerprintInternal → 直接使用                     │
        └──────────────────────────────────────────────────────────────┘
                                ↓
        ┌──────────────────────────────────────────────────────────────┐
        │ Step 2 — 生成备选定位策略（generate alternatives）            │
        │   调用 self.strategy_gen.generate(fp) 生成至少 3 种备选策略。  │
        │   策略按 priority 升序排列，记录到 result["alternatives"]。    │
        └──────────────────────────────────────────────────────────────┘
                                ↓
        ┌──────────────────────────────────────────────────────────────┐
        │ Step 3 — 模糊匹配（fuzzy match）                              │
        │   调用 self.matcher.find_best_match() 在 xml_content 中寻找    │
        │   与 fp 相似度 ≥ self.similarity_threshold 的最佳匹配节点。    │
        │   若找不到（best_node is None），设置失败信息并返回。          │
        └──────────────────────────────────────────────────────────────┘
                                ↓
        ┌──────────────────────────────────────────────────────────────┐
        │ Step 4 — 提取新定位器 + 更新 YAML（update YAML）             │
        │   4a. 调用 self._extract_best_locator() 从匹配节点提取         │
        │       定位器（优先级：resource-id > text > content-desc        │
        │       > 备选策略第一项）。                                     │
        │   4b. 调用 self.updater.update_locator() 将 YAML 中的         │
        │       failed_locator 替换为 new_locator。                     │
        │   4c. 设置 result["healed"]=True 和 result["new_locator"]。   │
        └──────────────────────────────────────────────────────────────┘
        """
        # 初始化结果字典，默认状态为"未治愈"
        result: dict[str, Any] = {
            "healed": False,
            "new_locator": None,
            "alternatives": [],
            "message": "",
        }

        # ═══ Step 1: 标准化指纹 — 将不同来源的元素描述统一为内部 dataclass ═══
        # 判断 fingerprint 的实际类型并调用对应的转换方法：
        #   - dict → from_dict()（处理 JSON/API 传入的原始字典）
        #   - ElementFingerprint → from_pydantic()（处理 Pydantic 模型）
        #   - ElementFingerprintInternal → 直接使用（无需转换）
        if isinstance(fingerprint, dict):
            fp = ElementFingerprintInternal.from_dict(fingerprint)
        elif isinstance(fingerprint, ElementFingerprint):
            fp = ElementFingerprintInternal.from_pydantic(fingerprint)
        else:
            fp = fingerprint

        logger.info(f"开始自愈流程，原始定位器: '{failed_locator}'")

        # ═══ Step 2: 生成备选定位策略 ═══
        # 调用 LocatorStrategyGenerator.generate() 基于指纹属性
        # 生成 text_fuzzy / xpath_contains / class_match / resource_id_partial / fallback_index
        # 等策略，按优先级升序排列
        alternatives = self.strategy_gen.generate(fp)
        result["alternatives"] = [
            {"type": a.strategy_type, "locator": a.locator} for a in alternatives
        ]
        logger.info(f"生成 {len(alternatives)} 个备选定位策略")

        # ═══ Step 3: 模糊匹配 — 在 UI 层级树中寻找最相似元素 ═══
        # 使用 difflib.SequenceMatcher 算法，计算每个 XML 节点与目标指纹的相似度，
        # 返回相似度 ≥ self.similarity_threshold 的最佳匹配节点
        best_node = self.matcher.find_best_match(
            xml_content, fp, self.similarity_threshold
        )

        # 匹配失败：无节点达到阈值，直接返回失败结果
        if best_node is None:
            result["message"] = "模糊匹配未找到相似度达标的元素"
            logger.warning(result["message"])
            return result

        # ═══ Step 4a: 从匹配节点中提取最可用的新定位器 ═══
        # 优先级顺序：resource-id（唯一性最强）→ text（语义明确）
        # → content-desc（无障碍描述）→ 备选策略第一项（兜底）
        new_locator = self._extract_best_locator(best_node, alternatives)
        if not new_locator:
            result["message"] = "无法从匹配节点提取有效定位器"
            return result

        # ═══ Step 4b: 更新 YAML 文件中的定位器 ═══
        # 调用 YamlUpdater.update_locator() 先备份原文件，再将旧定位器替换为新定位器
        success = self.updater.update_locator(yaml_path, failed_locator, new_locator)
        if success:
            result["healed"] = True
            result["new_locator"] = new_locator
            result["message"] = f"自愈成功: '{failed_locator}' → '{new_locator}'"
            logger.info(result["message"])
        else:
            result["message"] = f"YAML 更新失败，无法替换定位器"

        return result

    def _extract_best_locator(
        self,
        node: dict[str, Any],
        alternatives: list[LocatorStrategy],
    ) -> str | None:
        """
        从模糊匹配到的节点中提取最可用的定位器字符串。

        【参数】
          - node：FuzzyElementMatcher.find_best_match() 返回的节点属性字典。
          - alternatives：备选定位策略列表（兜底用）。

        【返回值】
          提取到的定位器字符串，若节点无任何可用属性则返回 None。

        【提取优先级逻辑】
          按以下顺序检查节点的属性，返回第一个非空值：
            1. resource-id：Android 资源 ID，唯一性最强，优先使用。
               示例：node["resource-id"] = "com.example:id/login_btn"
            2. text：元素显示文本，用户可见，语义明确。
               示例：node["text"] = "登录"
            3. content-desc：无障碍描述，在图标/图片元素上常用。
               示例：node["content-desc"] = "返回上一页"
            4. 回退到备选策略列表中优先级最高的策略的定位器。
               这确保即使匹配节点的所有自然属性都为空，仍能生成一个可用的定位器。

          此优先级设计基于属性稳定性经验：
          resource-id 是开发者显式指定的，变化概率最低；
          text 可能因国际化/文案调整而变化但仍较稳定；
          content-desc 通常用于无障碍，质量参差不齐；
          备选策略作为最后兜底。
        """
        # 优先级 1：resource-id（最稳定、唯一性最强）
        if rid := node.get("resource-id"):
            return rid
        # 优先级 2：text（用户可见文本，语义明确）
        if text := node.get("text"):
            return text
        # 优先级 3：content-desc（无障碍描述）
        if desc := node.get("content-desc"):
            return desc
        # 优先级 4：回退到第一个备选策略的定位器（兜底）
        if alternatives:
            return alternatives[0].locator
        return None

    def build_fingerprint_from_error(
        self, error_message: str
    ) -> ElementFingerprintInternal:
        """
        从 Maestro 错误信息字符串中提取元素指纹。

        【参数】
          error_message：Maestro 执行失败时抛出的错误消息文本。

        【返回值】
          ElementFingerprintInternal 实例，包含从错误信息中提取的属性。

        【背景 — Maestro 错误信息格式】
          Maestro 在找不到元素时会输出包含目标元素属性信息的错误消息，
          典型格式如下：
            - "Element with text 'Login' not found"
            - "No view matching id 'com.example:id/login_btn' found"
            - "Element with text 'Submit' and content-desc '提交按钮' not visible"
          本方法通过正则表达式从这些自由文本中提取可用的元素属性。

        【正则表达式模式详解】

          模式 1 — 提取 text 属性：
            regex: text\s*['"]([^'"]+)['"]
            说明：
              - text     ：匹配字面量 "text"
              - \s*      ：匹配零或多个空白字符（空格/tab）
              - ['"]     ：匹配单引号或双引号（开引号）
              - ([^'"]+) ：捕获组：匹配一个或多个非引号字符（即引号内的文本内容）
              - ['"]     ：匹配单引号或双引号（闭引号）
              - re.I     ：忽略大小写（Text / TEXT 均可匹配）
            匹配示例：
              "Element with text 'Login' not found"  → 捕获 "Login"
              'text "提交"'                          → 捕获 "提交"

          模式 2 — 提取 resource-id 属性：
            regex: (?:id|resource-id)\s*['"]([^'"]+)['"]
            说明：
              - (?:id|resource-id) ：非捕获组，匹配 "id" 或 "resource-id"
              - \s*                ：匹配零或多个空白字符
              - ['"]               ：开引号
              - ([^'"]+)           ：捕获组：引号内的 ID 值
              - ['"]               ：闭引号
              - re.I               ：忽略大小写
            匹配示例：
              "No view matching id 'com.example:id/btn' found" → 捕获 "com.example:id/btn"
              "resource-id 'android:id/content' not found"     → 捕获 "android:id/content"

          模式 3 — 提取 content-desc 属性：
            regex: content-desc\s*['"]([^'"]+)['"]
            说明：
              - content-desc ：匹配字面量 "content-desc"
              - 其余与上述模式相同
            匹配示例：
              "content-desc '返回按钮' not found" → 捕获 "返回按钮"

        【设计考量】
          - 三个正则独立执行，互不影响，可同时提取多个属性。
          - 使用非贪婪捕获组 ([^'"]+) 而非 (.+?) 以确保不会跨越引号边界。
          - 若某种属性未在错误信息中出现，对应字段保持 None。
        """
        fp = ElementFingerprintInternal()

        # 模式 1：尝试匹配 text='xxx' 或 text="xxx"
        # regex 分解：text → 零或多个空白 → 开引号 → 捕获内容 → 闭引号
        text_match = re.search(r"text\s*['\"]([^'\"]+)['\"]", error_message, re.I)
        if text_match:
            fp.text = text_match.group(1)  # group(1) 是捕获组的内容（引号内的文本）

        # 模式 2：尝试匹配 id='xxx' 或 resource-id='xxx'
        # (?:id|resource-id) 是非捕获组，同时匹配两种 ID 表示形式
        rid_match = re.search(
            r"(?:id|resource-id)\s*['\"]([^'\"]+)['\"]", error_message, re.I
        )
        if rid_match:
            fp.resource_id = rid_match.group(1)

        # 模式 3：尝试匹配 content-desc='xxx'
        desc_match = re.search(
            r"content-desc\s*['\"]([^'\"]+)['\"]", error_message, re.I
        )
        if desc_match:
            fp.content_desc = desc_match.group(1)

        logger.info(f"从错误信息提取指纹: {fp.to_dict()}")
        return fp
