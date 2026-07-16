"""
Pydantic V2 数据模型 — 定义平台中所有的数据结构。

本模块是整个平台的数据基础层，所有模块之间的数据传递均通过这些 Pydantic 模型进行。
遵循 Pydantic V2 规范，使用 Field 描述和类型注解提供完整的运行时校验。

数据模型分类:
  - 元素指纹: ElementFingerprint, LocatorStrategy
  - 枚举类型: Platform, TestStatus, RetryLevel
  - 测试用例: TestStep, TestCase
  - 执行结果: StepResult, ExecutionResult
  - AI 分析: AIInsight, TestReport
  - 设备管理: DeviceInfo
  - MCP 协议: MCPRequest, MCPResponse
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════
# 元素指纹 — 自愈匹配的核心数据结构
# ═══════════════════════════════════════════

class ElementFingerprint(BaseModel):
    """
    UI 元素的指纹信息，用于自愈匹配。

    指纹由多个属性组成，描述一个 UI 元素在多维度上的特征。
    当 Maestro 因找不到元素而失败时，自愈引擎会基于这些属性
    在当前 UI 层级树中模糊匹配最相似的元素。

    属性说明:
        text:        元素的显示文本（如 "登录"）
        resource_id: Android resource-id（如 "com.example:id/login_btn"）
        content_desc: 无障碍描述（Accessibility content-desc）
        xpath:       元素在 XML 层级树中的完整 XPath 路径
        class_name:   元素的类名（如 "android.widget.Button"）
        index:        同类型元素中的索引位置（从 0 开始）
    """
    text: str | None = Field(default=None, description="元素的 text 属性")
    resource_id: str | None = Field(default=None, description="元素的 resource-id")
    content_desc: str | None = Field(default=None, description="元素的 content-desc")
    xpath: str | None = Field(default=None, description="元素的 XPath 路径")
    class_name: str | None = Field(default=None, description="元素的 class 属性")
    index: int | None = Field(default=None, description="同类型元素的索引")

    def to_locator_str(self) -> str:
        """
        将指纹转换为主要的定位器字符串，按优先级返回。

        优先级: resource_id > text > content_desc > xpath > "unknown"
        这个方法在自愈成功后用于获取新的定位器写入 YAML 文件。
        """
        if self.resource_id:
            return self.resource_id
        if self.text:
            return self.text
        if self.content_desc:
            return self.content_desc
        if self.xpath:
            return self.xpath
        return "unknown"


# ═══════════════════════════════════════════
# 备选定位器 — 自愈策略的数据载体
# ═══════════════════════════════════════════

class LocatorStrategy(BaseModel):
    """
    备选定位策略。

    当主定位器（如 resource-id）失效时，自愈引擎会生成 3 种备选策略。
    每种策略有不同的定位方式和优先级，按 priority 从低到高尝试。

    策略类型:
        - text_fuzzy:           基于 text 的模糊匹配（contains）
        - xpath_contains:       基于 content-desc 的 XPath contains 匹配
        - class_match:          基于元素类名匹配
        - resource_id_partial:  基于 resource-id 的部分匹配
        - fallback_index:       回退到索引定位
    """
    strategy_type: str = Field(description="策略类型: xpath_contains, text_fuzzy, class_match 等")
    locator: str = Field(description="具体的定位器表达式，如 contains(@text, 'Login')")
    priority: int = Field(default=1, description="优先级，1 最高，数字越大优先级越低")
    description: str = Field(default="", description="策略的人类可读描述")


# ═══════════════════════════════════════════
# 平台与状态枚举
# ═══════════════════════════════════════════

class Platform(str, Enum):
    """目标测试平台枚举。"""
    ANDROID = "android"
    IOS = "ios"


class TestStatus(str, Enum):
    """
    测试执行状态枚举。

    状态流转:
        passed:  全部步骤通过
        failed:  执行失败（所有重试级别均未恢复）
        healed:  自愈后通过（L2 或 L3 修复成功）
        skipped: 跳过执行
        error:   系统级错误（非测试逻辑错误）
    """
    PASSED = "passed"
    FAILED = "failed"
    HEALED = "healed"      # 自愈后通过
    SKIPPED = "skipped"
    ERROR = "error"


class RetryLevel(str, Enum):
    """
    重试级别枚举，对应编排器的三层恢复机制。

    L1: Maestro 原生命令重试（subprocess 级别）
    L2: 元素自愈重试（ElementHealer 模糊匹配后更新 YAML）
    L3: AI 自主修复（LLM 分析错误日志后重写 YAML）
    """
    L1 = "L1"  # 即时重试 (Maestro 原生)
    L2 = "L2"  # 自愈重试
    L3 = "L3"  # AI 自主修复


# ═══════════════════════════════════════════
# 测试用例模型
# ═══════════════════════════════════════════

class TestStep(BaseModel):
    """
    单个测试步骤。

    代表 Maestro YAML 中的一个命令，如 tapOn、inputText、assertVisible 等。

    字段说明:
        action:                命令动作名称
        target:                目标元素定位器（如 "Login" 或 "com.example:id/btn"）
        value:                 输入值或断言文本
        index:                 步骤在用例中的序号（从 1 开始）
        original_yaml_snippet: 原始 YAML 片段（用于追溯）
    """
    action: str = Field(description="动作类型: tap, input, swipe, assertVisible, launchApp 等")
    target: str | None = Field(default=None, description="目标元素定位器")
    value: str | None = Field(default=None, description="输入值或断言文本")
    index: int = Field(description="步骤序号")
    original_yaml_snippet: str | None = Field(default=None)


class TestCase(BaseModel):
    """
    完整的测试用例。

    包含从自然语言生成或手动编写的完整测试用例信息。
    可通过 TestCaseGenerator.generate() 创建，或从 YAML 文件解析得到。

    关键字段:
        app_id:   目标 App 的 bundleId (iOS) 或 packageName (Android)
        platform: 目标平台，决定了 Maestro 的执行环境
        yaml_path: 生成的 YAML 文件在 flows/ 目录下的路径
    """
    name: str = Field(description="用例名称")
    description: str | None = Field(default=None, description="自然语言描述")
    app_id: str = Field(description="目标 App 的 bundleId / packageName")
    platform: Platform = Field(default=Platform.ANDROID)
    steps: list[TestStep] = Field(default_factory=list)
    yaml_path: Path | None = Field(default=None, description="生成的 YAML 文件路径")
    created_at: datetime = Field(default_factory=datetime.now)


# ═══════════════════════════════════════════
# 执行结果模型
# ═══════════════════════════════════════════

class StepResult(BaseModel):
    """
    单个步骤的执行结果。

    记录每一步的耗时、状态、截图路径，以及如果发生自愈时的修复信息。

    关键字段:
        step_index:     步骤序号
        status:         执行状态（passed / failed / healed）
        retry_level:    最终通过的重试级别（L1/L2/L3），仅在通过时记录
        healed_locator: 自愈后的新定位器（仅在 L2 自愈成功时记录）
    """
    step_index: int
    action: str
    status: TestStatus
    duration_ms: float = 0
    error_message: str | None = None
    screenshot_path: str | None = None
    retry_level: RetryLevel | None = None
    healed_locator: str | None = None


class ExecutionResult(BaseModel):
    """
    完整的测试执行结果。

    这是编排器 MaestroOrchestrator.execute() 的返回值，
    也是 AIInsightReporter 生成报告的输入数据。

    统计字段:
        total_steps:  总步骤数
        passed_steps: 通过步骤数
        failed_steps: 失败步骤数
        healed_steps: 通过自愈修复的步骤数
    """
    test_name: str
    platform: Platform
    device_id: str | None = None
    status: TestStatus
    start_time: datetime
    end_time: datetime | None = None
    duration_ms: float = 0
    total_steps: int = 0
    passed_steps: int = 0
    failed_steps: int = 0
    healed_steps: int = 0
    step_results: list[StepResult] = Field(default_factory=list)
    raw_maestro_log: str | None = None       # Maestro CLI 原始 stdout + stderr
    yaml_path: Path | None = None            # 执行的 YAML 文件路径


# ═══════════════════════════════════════════
# AI 分析结果模型
# ═══════════════════════════════════════════

class AIInsight(BaseModel):
    """
    AI 生成的测试洞察。

    由 AIAnalyzer 调用 LLM 分析失败测试后生成。
    每条洞察针对一个失败步骤，包含根因、建议和可选的 YAML 修复代码。

    字段说明:
        root_cause:              失败根因分析（如 "定位器 resource-id 已变更"）
        suggestion:               修复建议（如 "建议改用 text 模糊匹配"）
        confidence:               置信度 0.0 ~ 1.0
        visual_defect_detected:   是否检测到视觉缺陷（UI 渲染问题）
        recommended_yaml_fix:     AI 推荐的 YAML 修复片段
    """
    step_index: int
    root_cause: str = Field(description="失败根因分析")
    suggestion: str = Field(description="修复建议")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    visual_defect_detected: bool = False
    recommended_yaml_fix: str | None = None


class TestReport(BaseModel):
    """
    聚合测试报告。

    包含执行结果、AI 洞察、截图列表等完整信息。
    通过 AIInsightReporter.generate_report() 创建。

    报告 ID 格式: YYYYMMDD_HHMMSS
    HTML 报告路径: reports/report_{report_id}.html
    """
    report_id: str = Field(default_factory=lambda: datetime.now().strftime("%Y%m%d_%H%M%S"))
    execution_result: ExecutionResult
    ai_insights: list[AIInsight] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=datetime.now)
    html_path: Path | None = None


# ═══════════════════════════════════════════
# 设备信息模型
# ═══════════════════════════════════════════

class DeviceInfo(BaseModel):
    """
    设备信息。

    对应 config/devices.yaml 中的设备配置条目。
    通过 is_connected 标记设备是否当前可用。
    """
    name: str
    platform: Platform
    avd: str | None = None            # Android AVD 名称
    udid: str | None = None           # iOS 模拟器 UDID
    is_default: bool = False          # 是否为默认设备
    is_connected: bool = False        # 当前是否已连接


# ═══════════════════════════════════════════
# MCP 协议消息模型
# ═══════════════════════════════════════════

class MCPRequest(BaseModel):
    """
    MCP (Model Context Protocol) 请求。

    用于 Trae IDE 与平台之间的通信。
    支持的方法: generate, run, status, report
    """
    method: str = Field(description="方法名: generate | run | status | report")
    params: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class MCPResponse(BaseModel):
    """
    MCP (Model Context Protocol) 响应。

    success=True 时 data 包含业务数据
    success=False 时 error 包含错误描述
    """
    request_id: str | None = None
    success: bool
    data: Any | None = None
    error: str | None = None
