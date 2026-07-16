"""
阶段五：AI 报告与洞察模块 — AIInsightReporter

解析 maestro test --analyze 数据，结合 AI 生成高可读性 HTML 报告。

本模块的整体架构:
  MaestroLogParser  →  解析 Maestro 的原始日志输出（JSON 文件 / stdout 文本）
  AIAnalyzer        →  调用大语言模型对失败步骤进行根因分析与修复建议
  HTMLReportRenderer →  基于 Jinja2 模板将分析结果渲染为精美的 HTML 报告
  AIInsightReporter  →  主入口，编排上述三个组件完成端到端报告生成流程
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from src.models.schemas import AIInsight, ExecutionResult, TestReport, TestStatus

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
# Maestro 日志解析
# ═══════════════════════════════════════════

class MaestroLogParser:
    """
    解析 ~/.maestro/tests/ 下的 JSON 日志。

    设计思路:
      Maestro 每次测试执行后会在 ~/.maestro/tests/<timestamp>/ 下生成:
        - 每个步骤一个 .json 文件（包含步骤名、耗时、截图路径、状态等元数据）
        - 截图文件 (.png/.jpg)
        - metrics*.json（性能指标，如内存、CPU、帧率）
      本类负责读取这些文件并将其结构化，供后续分析和报告使用。
    """

    @staticmethod
    def get_latest_log_dir() -> Path | None:
        """
        获取最新的 Maestro 测试日志目录。

        工作原理:
          1. 读取 ~/.maestro/tests/ 目录（Maestro 默认日志存储路径）
          2. 按文件修改时间（st_mtime）降序排列，最新的在前
          3. 遍历目录列表，返回第一个有效的子目录

        这样总能拿到最近一次测试的日志，无需手动指定目录路径。
        """
        # Maestro 日志默认存储路径：用户主目录下的 .maestro/tests/
        base = Path.home() / ".maestro" / "tests"
        # 安全检查：如果目录不存在（比如 Maestro 从未运行过），直接返回 None
        if not base.exists():
            logger.warning(f"Maestro 测试日志目录不存在: {base}")
            return None

        # 按文件修改时间降序排列，最新的目录排在最前面
        dirs = sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        # 遍历找到第一个子目录（跳过可能的非目录文件）
        for d in dirs:
            if d.is_dir():
                return d
        return None

    @staticmethod
    def parse_logs(log_dir: Path | None = None) -> dict[str, Any]:
        """
        解析 Maestro 日志目录中的 JSON 数据。

        遍历日志目录，提取三类数据:
          - steps: 每个 .json 文件代表一个测试步骤的详细执行记录
          - screenshots: 所有截图文件的路径列表
          - metrics: 性能指标数据（如果有 metrics*.json 文件）

        Args:
            log_dir: 日志目录路径，None 则自动获取最新目录

        Returns:
            {"steps": [...], "screenshots": [...], "metrics": {...}}
            如果日志目录不存在或为空，返回空结构体
        """
        # 如果未指定日志目录，自动获取最新的
        if log_dir is None:
            log_dir = MaestroLogParser.get_latest_log_dir()

        # 目录不存在则返回空结果，避免后续处理异常
        if log_dir is None or not log_dir.exists():
            return {"steps": [], "screenshots": [], "metrics": {}}

        # 初始化返回结构，三个字段分别对应步骤、截图、性能指标
        result: dict[str, Any] = {"steps": [], "screenshots": [], "metrics": {}}

        try:
            # ---- 第一步：遍历日志文件夹，分类提取 JSON 和图片 ----
            # 按文件名排序确保步骤顺序一致性
            for f in sorted(log_dir.iterdir()):
                # .json 文件 → 步骤执行记录
                if f.suffix == ".json":
                    try:
                        # 以 UTF-8 编码读取 JSON，解析为 Python 字典后追加到 steps 列表
                        data = json.loads(f.read_text(encoding="utf-8"))
                        result["steps"].append(data)
                    except json.JSONDecodeError:
                        # 跳过损坏的 JSON 文件，不会中断整个解析流程
                        continue
                # 图片文件 → 截图证据
                elif f.suffix in (".png", ".jpg", ".jpeg"):
                    # 存储为绝对路径字符串，方便 HTML 报告引用
                    result["screenshots"].append(str(f))

            # ---- 第二步：提取性能指标 metrics ----
            # 查找所有以 "metrics" 开头的 JSON 文件（如 metrics.json, metrics_001.json）
            metrics_files = list(log_dir.glob("metrics*.json"))
            if metrics_files:
                # 取第一个匹配到的 metrics 文件即可
                result["metrics"] = json.loads(
                    metrics_files[0].read_text(encoding="utf-8")
                )

        except Exception as e:
            # 用宽泛的异常捕获防止解析过程中的意外错误中断整个报告生成
            logger.error(f"解析 Maestro 日志失败: {e}")

        return result

    @staticmethod
    def parse_steps_from_stdout(stdout: str) -> list[dict[str, Any]]:
        """
        从 maestro --analyze 的 stdout 解析步骤执行信息。

        适用场景:
          当没有完整的日志目录（如 CI/CD 中只捕获了终端输出），
          可以直接从 maestro test 命令的 stdout 文本中解析测试结果。

        解析逻辑:
          - Maestro 输出中每行以 "✓"（已通过）或 "✗"（失败）开头
          - 同时支持 Unicode 变体字符 ✔ / ✘ / ×
          - 行尾可能包含耗时信息，如 "(1234ms)"
          - 去掉符号前缀后剩余部分为步骤描述文本

        Args:
            stdout: maestro test 命令的完整标准输出文本

        Returns:
            步骤信息列表，每个元素包含 status / description / duration_ms 等字段
        """
        steps: list[dict[str, Any]] = []

        # 逐行扫描 stdout 输出
        for line in stdout.splitlines():
            line = line.strip()
            # 跳过空行
            if not line:
                continue

            step_info: dict[str, Any] = {}

            # ---- 状态判定：通过行首符号识别 ----
            # Maestro 用 ✓ / ✔ 标记通过的步骤
            if line.startswith("✓") or line.startswith("✔"):
                step_info["status"] = "passed"
            # 用 ✗ / ✘ / × 标记失败的步骤
            elif line.startswith("✗") or line.startswith("✘") or line.startswith("×"):
                step_info["status"] = "failed"
            else:
                # 不以状态符号开头的行，不是步骤执行行，跳过
                continue

            # ---- 提取步骤描述 ----
            # 去掉行首的状态符号（1个字符），剩余部分即为步骤描述
            desc = line[1:].strip()
            step_info["description"] = desc

            # ---- 提取耗时 duration ----
            # Maestro 输出格式如: ✓ Tap on "Login" (1234ms)
            # 用正则从括号中提取毫秒数
            duration_match = re.search(r"\((\d+)ms\)", line)
            if duration_match:
                step_info["duration_ms"] = int(duration_match.group(1))

            steps.append(step_info)

        return steps


# ═══════════════════════════════════════════
# AI 深度分析
# ═══════════════════════════════════════════

class AIAnalyzer:
    """
    调用 AI 对测试失败进行深度分析。

    设计目的:
      传统测试报告只告诉你"某步骤失败了"，但不会解释为什么。
      AIAnalyzer 利用大语言模型的语义理解能力，自动分析失败原因，
      并提供以下维度的智能洞察:
        - root_cause: 失败的根本原因（如元素未找到、超时、断言不匹配等）
        - suggestion: 可操作的修复建议
        - visual_defect_detected: 是否检测到视觉缺陷（布局错乱、UI 渲染异常等）
        - recommended_yaml_fix: 推荐的 YAML 修复片段（可直接复制使用）
        - confidence: 置信度评分（0-1，表示 AI 对该分析的确定程度）

    兼容性:
      默认使用 DeepSeek API，也支持 OpenAI 兼容接口（通过环境变量切换）。
      若 API Key 未配置或调用失败，自动降级为 _fallback_insights（静态规则分析）。
    """

    # ─── AI 分析提示词设计 ───
    # 这是一个精心设计的系统提示词，遵循以下原则:
    #   1. 角色设定: "You are a mobile QA analyst" — 赋予 LLM 专业的 QA 分析师角色
    #   2. 上下文注入: 通过 {summary} 和 {failed_steps} 占位符注入测试摘要和失败步骤
    #   3. 结构化输出: 要求以 JSON 格式返回，便于程序解析
    #   4. 多维度分析: 要求 LLM 同时输出根因、修复建议、视觉缺陷检测、置信度
    #   5. 可操作性强: 要求提供 "actionable fix suggestions" 和 "corrected YAML snippets"
    # temperature=0.3 用于调用时控制输出稳定性，避免分析结果波动过大
    ANALYSIS_PROMPT = """You are a mobile QA analyst reviewing Maestro test results.

## Test Result Summary:
{summary}

## Failed Steps:
{failed_steps}

## Task:
1. Analyze the root cause of each failure.
2. Determine if there are visual defects (UI rendering issues, layout problems, missing elements).
3. Provide specific, actionable fix suggestions for each failed step.
4. If possible, suggest corrected YAML snippets.

Respond in JSON format:
```json
{{
  "insights": [
    {{
      "step_index": 1,
      "root_cause": "...",
      "suggestion": "...",
      "confidence": 0.9,
      "visual_defect_detected": false,
      "recommended_yaml_fix": "..."
    }}
  ]
}}
```"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        """
        初始化 AI 分析器。

        API 配置优先级:
          1. 构造参数 api_key / base_url（代码显式传入）
          2. 环境变量 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL（DeepSeek 专用）
          3. 环境变量 OPENAI_API_KEY（OpenAI 兼容通用）

        模型选择: 通过 AI_MODEL 环境变量指定，默认使用 deepseek-chat。
        """
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        self.model = os.getenv("AI_MODEL", "deepseek-chat")

    def analyze(
        self, execution_result: ExecutionResult, failed_steps: list[dict[str, Any]]
    ) -> list[AIInsight]:
        """
        对失败用例进行 AI 深度分析。

        调用流程:
          1. 检查 API Key 是否已配置，未配置则直接使用 fallback
          2. 构建包含测试摘要和失败步骤的上下文 prompt
          3. 调用 OpenAI 兼容的 Chat Completions API
          4. 解析 LLM 返回的 JSON 响应，转换为 AIInsight 对象列表
          5. 如果解析失败或 API 调用异常，回退到 fallback 机制

        Args:
            execution_result: 测试执行结果（包含测试名、平台、状态、耗时等元数据）
            failed_steps: 失败的步骤列表（来自 MaestroLogParser 解析结果）

        Returns:
            AIInsight 列表，每个失败步骤对应一个分析洞察
        """
        # ---- 前置检查：无 API Key 直接走 fallback ----
        # 这样即使在离线环境或未配置 AI 的环境中也能正常生成报告
        if not self.api_key:
            logger.warning("未配置 AI API Key，跳过 AI 深度分析")
            return self._fallback_insights(failed_steps)

        try:
            # ---- 延迟导入 OpenAI SDK ----
            # 只在真正需要调用 AI 时才导入，避免在无 AI 环境中导入失败
            from openai import OpenAI

            # ---- 构建测试摘要文本 ----
            # 将 ExecutionResult 结构体转换为可读的文本描述，注入到 prompt 中
            summary = (
                f"Test: {execution_result.test_name}\n"
                f"Platform: {execution_result.platform}\n"
                f"Status: {execution_result.status}\n"
                f"Duration: {execution_result.duration_ms:.0f}ms\n"
                f"Steps: {execution_result.total_steps} total, "
                f"{execution_result.passed_steps} passed, "
                f"{execution_result.failed_steps} failed"
            )

            # ---- 构建失败步骤描述文本 ----
            failed_text = "\n".join(
                f"Step {s.get('step_index', i + 1)}: {s.get('description', 'unknown')}"
                for i, s in enumerate(failed_steps)
            )

            # ---- 调用 OpenAI 兼容 API ----
            # 使用 Chat Completions 接口，temperature=0.3 降低随机性确保分析结果稳定
            # max_tokens=2048 足够容纳多个失败步骤的详细分析
            client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": self.ANALYSIS_PROMPT.format(
                            summary=summary, failed_steps=failed_text
                        ),
                    }
                ],
                temperature=0.3,
                max_tokens=2048,
            )

            # ---- 提取 LLM 返回的文本内容 ----
            content = response.choices[0].message.content
            if content is None:
                # 返回内容为空（极少见的情况，如被安全过滤器拦截），走 fallback
                return self._fallback_insights(failed_steps)

            # ---- 解析 JSON 响应 ----
            # LLM 可能在 JSON 前后添加额外文本（如解释性语句），
            # 使用正则提取第一个完整的 JSON 对象 `{...}`
            json_match = re.search(r"\{[\s\S]*\}", content)
            if json_match:
                data = json.loads(json_match.group())
                # 将 JSON 中的 insights 数组映射为 AIInsight 数据类实例
                return [
                    AIInsight(**item) for item in data.get("insights", [])
                ]
            # 无法提取有效 JSON，走 fallback
            return self._fallback_insights(failed_steps)

        except Exception as e:
            # ---- 异常处理：任何异常都降级为 fallback ----
            # 确保即使 AI 服务不可用，报告生成流程也不会中断
            logger.error(f"AI 分析失败: {e}")
            return self._fallback_insights(failed_steps)

    @staticmethod
    def _fallback_insights(failed_steps: list[dict[str, Any]]) -> list[AIInsight]:
        """
        当 AI 不可用时的回退洞察。

        设计思路:
          作为 AI 分析的降级方案，基于简单的启发式规则生成基础洞察:
            - root_cause: 直接引用步骤描述作为失败原因
            - suggestion: 提供通用的排查建议（检查元素定位器、增加等待时间）
            - confidence: 固定为 0.5，表示这是规则推断而非 AI 分析，确定性较低

        这种 fallback 机制保证了:
          1. 报告在任何情况下都能完整生成（不会因为 AI 调用失败而中断）
          2. 即使没有 AI，用户仍能看到每个失败步骤的基本信息
          3. 置信度 0.5 明确传达"这是猜测而非精确诊断"的信号
        """
        return [
            AIInsight(
                step_index=i + 1,
                root_cause=f"步骤执行失败: {s.get('description', '未知')}",
                suggestion="建议检查元素定位器是否正确，或增加等待时间。",
                confidence=0.5,
            )
            for i, s in enumerate(failed_steps)
        ]


# ═══════════════════════════════════════════
# HTML 报告渲染
# ═══════════════════════════════════════════

class HTMLReportRenderer:
    """
    基于 Jinja2 模板生成美观的 HTML 测试报告。

    设计决策:
      - 使用内联模板字符串（TEMPLATE 类属性）而非外部 .html 文件，
        避免部署时需要管理额外的模板文件依赖
      - CSS 采用内联 `<style>` 标签，确保单个 HTML 文件即可完整呈现，
        无需外部样式表
      - 所有样式零外部依赖（无 CDN、无第三方 CSS 框架），纯原生设计
      - 截图使用 file:// 协议直接引用本地文件，无需搭建 Web 服务器

    CSS 样式设计说明:
      - 整体风格: 现代扁平化设计，浅灰背景 (#f5f7fa) 配白色卡片
      - 色彩体系:
        * header 渐变: 紫色渐变 (#667eea → #764ba2)，品牌感强且专业
        * 通过: 绿色系 (#d4edda / #155724)，符合直觉的"好结果"色
        * 失败: 红色系 (#f8d7da / #721c24)，警示性强
        * 自愈: 黄色系 (#fff3cd / #856404)，表示"需要注意"
      - 卡片设计: 白色背景、8px 圆角、轻微阴影，层叠关系清晰
      - 字体栈: 系统原生字体优先 (-apple-system, Segoe UI, Roboto)，
        确保跨平台一致的阅读体验且无需加载外部字体
      - 代码块: 深色背景仿终端风格 (#1e1e1e)，等宽字体，适合展示日志和 YAML
      - 响应式: summary-grid 和 screenshot-gallery 使用 CSS Grid 的
        auto-fit + minmax 实现自适应列数
    """

    # ─── Jinja2 内联模板 ───
    # 模板包含以下区域（按渲染顺序）:
    #   1. header        — 测试名称、平台、设备、生成时间的横幅区域
    #   2. summary-grid  — 状态、耗时、步骤统计的数据卡片网格
    #   3. AI Insights   — AI 分析洞察卡片，含根因、建议、置信度、YAML 修复代码
    #   4. Screenshots   — 测试截图画廊，网格自适应排列
    #   5. Raw Log       — 原始 Maestro 日志（深色终端风格，可滚动）
    #   6. footer        — 页脚信息
    TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Maestro AI Test Report - {{ report.execution_result.test_name }}</title>
    <style>
        /* 全局重置：清除浏览器默认边距/内边距，统一盒模型为 border-box */
        * { margin: 0; padding: 0; box-sizing: border-box; }
        /* 主体样式：系统字体栈 + 浅灰背景 + 深灰文字 + 舒适行高 */
        body { font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; background: #f5f7fa; color: #333; line-height: 1.6; }
        /* 内容容器：最大宽度 960px 居中，适合桌面阅读，移动端自适应 */
        .container { max-width: 960px; margin: 0 auto; padding: 24px; }
        /* Header 横幅：紫色渐变背景，白色文字，大圆角，视觉焦点 */
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; padding: 32px; border-radius: 12px; margin-bottom: 24px; }
        .header h1 { font-size: 24px; margin-bottom: 8px; }
        .header .meta { opacity: 0.85; font-size: 14px; }
        /* 状态徽章：圆角胶囊形，用于 passed/failed/healed 状态展示 */
        .status-badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-weight: 600; font-size: 13px; }
        .status-passed { background: #d4edda; color: #155724; }
        .status-failed { background: #f8d7da; color: #721c24; }
        .status-healed { background: #fff3cd; color: #856404; }
        /* 通用卡片：白色背景，圆角，适当内边距，底部间距，微阴影 */
        .card { background: #fff; border-radius: 8px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
        .card h2 { font-size: 18px; margin-bottom: 12px; color: #555; border-bottom: 2px solid #eee; padding-bottom: 8px; }
        /* 摘要网格：使用 CSS Grid auto-fit 实现自适应列数，最小列宽 140px */
        .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 24px; }
        .summary-item { background: #fff; border-radius: 8px; padding: 16px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
        .summary-item .value { font-size: 32px; font-weight: 700; }
        .summary-item .label { font-size: 13px; color: #888; margin-top: 4px; }
        /* AI 洞察卡片：左侧紫色边框 + 淡紫色背景，视觉上与普通卡片区分 */
        .insight-card { border-left: 4px solid #667eea; padding: 12px 16px; background: #f8f9ff; border-radius: 0 8px 8px 0; margin-bottom: 12px; }
        .insight-card .cause { color: #d32f2f; font-weight: 600; margin-bottom: 4px; }
        .insight-card .suggestion { color: #555; }
        .insight-card .confidence { font-size: 12px; color: #999; }
        /* 截图画廊：CSS Grid 自适应网格，最小列宽 200px */
        .screenshot-gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
        .screenshot-gallery img { width: 100%; border-radius: 6px; border: 1px solid #e0e0e0; }
        /* YAML 修复建议代码块：深色终端风格背景，等宽字体 */
        .yaml-fix { background: #1e1e1e; color: #d4d4d4; padding: 12px; border-radius: 6px; font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px; overflow-x: auto; white-space: pre; }
        /* 页脚：居中对齐，小号灰色文字 */
        .footer { text-align: center; padding: 20px; color: #aaa; font-size: 13px; }
        /* 错误/原始日志区域：深色背景 + 红色错误文字，最大高度 300px 可滚动 */
        .error-log { background: #1e1e1e; color: #f48771; padding: 12px; border-radius: 6px; font-family: monospace; font-size: 12px; max-height: 300px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
    </style>
</head>
<body>
    <div class="container">
        <!-- ====== 1. Header 横幅 ====== -->
        <div class="header">
            <h1>{{ report.execution_result.test_name }}</h1>
            <div class="meta">
                Platform: {{ report.execution_result.platform.value }} |
                Device: {{ report.execution_result.device_id or 'N/A' }} |
                Generated: {{ report.generated_at.strftime('%Y-%m-%d %H:%M:%S') }}
            </div>
        </div>

        <!-- ====== 2. Summary Grid 摘要网格 ====== -->
        <div class="summary-grid">
            <!-- 状态卡片：用 Unicode 符号展示 passed(✔) / healed(⚠) / failed(✘) -->
            <div class="summary-item">
                <div class="value">
                    {% if report.execution_result.status.value == 'passed' %}&#10004;{% elif report.execution_result.status.value == 'healed' %}&#9888;{% else %}&#10008;{% endif %}
                </div>
                <div class="label">Status: {{ report.execution_result.status.value.upper() }}</div>
            </div>
            <!-- 耗时卡片 -->
            <div class="summary-item">
                <div class="value">{{ "%.0f"|format(report.execution_result.duration_ms) }}ms</div>
                <div class="label">Duration</div>
            </div>
            <!-- 总步骤数 -->
            <div class="summary-item">
                <div class="value">{{ report.execution_result.total_steps }}</div>
                <div class="label">Total Steps</div>
            </div>
            <!-- 通过步骤数（绿色） -->
            <div class="summary-item">
                <div class="value" style="color:#155724;">{{ report.execution_result.passed_steps }}</div>
                <div class="label">Passed</div>
            </div>
            <!-- 失败步骤数（红色） -->
            <div class="summary-item">
                <div class="value" style="color:#721c24;">{{ report.execution_result.failed_steps }}</div>
                <div class="label">Failed</div>
            </div>
            <!-- 自愈步骤数（黄色，仅当有自愈步骤时显示） -->
            {% if report.execution_result.healed_steps %}
            <div class="summary-item">
                <div class="value" style="color:#856404;">{{ report.execution_result.healed_steps }}</div>
                <div class="label">Healed</div>
            </div>
            {% endif %}
        </div>

        <!-- ====== 3. AI Insights 洞察卡片区 ====== -->
        {% if report.ai_insights %}
        <div class="card">
            <h2>AI Insights & Analysis</h2>
            <!-- 每个失败步骤对应一张洞察卡片 -->
            {% for insight in report.ai_insights %}
            <div class="insight-card">
                <!-- 根因分析：红色加粗，突出问题所在 -->
                <div class="cause">Step {{ insight.step_index }}: {{ insight.root_cause }}</div>
                <!-- 修复建议 -->
                <div class="suggestion">Suggestion: {{ insight.suggestion }}</div>
                <!-- 置信度和视觉缺陷检测标记 -->
                <div class="confidence">Confidence: {{ "%.0f"|format(insight.confidence * 100) }}%{% if insight.visual_defect_detected %} | Visual defect detected{% endif %}</div>
                <!-- 推荐的 YAML 修复代码（如果有） -->
                {% if insight.recommended_yaml_fix %}
                <div style="margin-top:8px;">
                    <strong>Recommended Fix:</strong>
                    <div class="yaml-fix">{{ insight.recommended_yaml_fix }}</div>
                </div>
                {% endif %}
            </div>
            {% endfor %}
        </div>
        {% endif %}

        <!-- ====== 4. Screenshots 截图画廊 ====== -->
        {% if report.screenshots %}
        <div class="card">
            <h2>Screenshots</h2>
            <div class="screenshot-gallery">
                {% for src in report.screenshots %}
                <!-- 使用 file:// 协议直接引用本地截图文件 -->
                <!-- onerror 处理：如果截图文件已被清理或路径无效，隐藏该图片而非显示破损图标 -->
                <img src="file://{{ src }}" alt="Screenshot" onerror="this.style.display='none'">
                {% endfor %}
            </div>
        </div>
        {% endif %}

        <!-- ====== 5. Raw Log 原始日志区 ====== -->
        {% if report.execution_result.raw_maestro_log %}
        <div class="card">
            <h2>Raw Log</h2>
            <!-- 深色终端风格展示原始 maestro 输出，最大高度 300px 可滚动 -->
            <div class="error-log">{{ report.execution_result.raw_maestro_log }}</div>
        </div>
        {% endif %}

        <!-- ====== 6. Footer 页脚 ====== -->
        <div class="footer">
            Generated by Maestro AI Platform | {{ report.generated_at.strftime('%Y-%m-%d %H:%M:%S') }}
        </div>
    </div>
</body>
</html>"""

    def __init__(self, output_dir: Path | None = None):
        """
        初始化 HTML 报告渲染器。

        output_dir 默认为项目根目录下的 reports/ 文件夹，
        自动创建目录（包括必要的父目录）。
        """
        self.output_dir = output_dir or (Path(__file__).parent.parent.parent / "reports")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def render(self, report: TestReport) -> Path:
        """
        渲染 HTML 报告并保存到文件。

        渲染策略:
          1. 创建 Jinja2 Environment（虽然使用的内联模板，仍需要 Environment 上下文）
          2. 使用 env.from_string() 从类属性 TEMPLATE 字符串创建模板，
             这样无需依赖外部 .html 模板文件，部署更简单
          3. 将 TestReport 对象作为 report 变量传入模板进行渲染
          4. 输出文件名格式: report_{report_id}.html（如 report_rpt_abc123.html）
          5. 写入输出目录，UTF-8 编码

        Returns:
            生成的 HTML 文件的完整路径
        """
        try:
            # 创建 Jinja2 环境（loader 指向模板目录，但实际使用内联模板）
            env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "templates")))

            # 使用内联模板（避免依赖外部文件）
            # from_string() 将字符串解析为 Jinja2 模板对象
            html = env.from_string(self.TEMPLATE).render(report=report)

            # 文件名格式: report_{report_id}.html
            filename = f"report_{report.report_id}.html"
            output_path = self.output_dir / filename
            # 以 UTF-8 编码写入文件
            output_path.write_text(html, encoding="utf-8")

            logger.info(f"HTML 报告已生成: {output_path}")
            return output_path

        except Exception as e:
            logger.error(f"HTML 报告生成失败: {e}")
            raise


# ═══════════════════════════════════════════
# AIInsightReporter — 主类
# ═══════════════════════════════════════════

class AIInsightReporter:
    """
    AI 报告与洞察模块主类。

    职责:
      作为本模块的外观（Facade），编排 MaestroLogParser、AIAnalyzer、
      HTMLReportRenderer 三个子组件，完成端到端的"日志 → 分析 → 报告"流程。

    组件依赖关系:
      AIInsightReporter
        ├── MaestroLogParser    — 解析 Maestro 原始日志
        ├── AIAnalyzer          — 调用 LLM 分析失败原因
        └── HTMLReportRenderer  — 渲染 HTML 报告

    使用方式:
      reporter = AIInsightReporter(api_key="sk-xxx")
      report = reporter.generate_report(execution_result, open_browser=True)
      print(f"Report saved to: {report.html_path}")
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        output_dir: Path | None = None,
    ):
        """
        初始化报告生成器。

        将 API 参数透传给 AIAnalyzer，output_dir 透传给 HTMLReportRenderer。
        三者的初始化彼此独立，便于单元测试时 mock 单个组件。
        """
        self.parser = MaestroLogParser()
        self.analyzer = AIAnalyzer(api_key=api_key, base_url=base_url)
        self.renderer = HTMLReportRenderer(output_dir=output_dir)

    def generate_report(
        self,
        execution_result: ExecutionResult,
        open_browser: bool = False,
    ) -> TestReport:
        """
        生成完整的 AI 测试报告。

        这是本模块的核心方法，完成 6 步端到端报告生成流水线:
          ┌──────────────────────────────────────────────────────┐
          │ Step 1: parse_logs()     — 解析 Maestro 日志目录     │
          │ Step 2: 提取 failed_steps — 筛选出所有失败步骤       │
          │ Step 3: AI analyze()     — 调用 LLM 深度分析失败原因 │
          │ Step 4: 构建 TestReport  — 组装报告数据模型          │
          │ Step 5: render()         — Jinja2 渲染 HTML 报告     │
          │ Step 6: 可选打开浏览器   — webbrowser.open()          │
          └──────────────────────────────────────────────────────┘

        Args:
            execution_result: 测试执行结果（来自 maestro runner）
            open_browser: 是否在生成后自动用默认浏览器打开 HTML 报告

        Returns:
            TestReport — 包含 AI 洞察、截图路径、HTML 文件路径的完整报告对象
        """
        # ── Step 1: 解析 Maestro 日志 ──
        # 自动获取最新测试日志目录，读取其中的 JSON 步骤记录和截图文件
        log_data = self.parser.parse_logs()

        # ── Step 2: 提取失败步骤 ──
        # 从所有步骤中筛选出 status == "failed" 的步骤，
        # 只对这些失败步骤进行 AI 分析（通过步骤无需浪费 API tokens）
        failed_steps = [
            s for s in log_data.get("steps", [])
            if s.get("status") == "failed"
        ]

        # ── Step 3: AI 深度分析 ──
        # 只有当存在失败步骤时才调用 AI 分析，避免不必要的 API 调用
        ai_insights: list[AIInsight] = []
        if failed_steps:
            logger.info(f"开始 AI 分析 {len(failed_steps)} 个失败步骤")
            ai_insights = self.analyzer.analyze(execution_result, failed_steps)

        # ── Step 4: 构建报告数据模型 ──
        # 组装 TestReport，将执行结果、AI 洞察、截图路径合并为一个完整的报告对象
        report = TestReport(
            execution_result=execution_result,
            ai_insights=ai_insights,
            screenshots=log_data.get("screenshots", []),
        )

        # ── Step 5: 渲染 HTML ──
        # 使用 Jinja2 模板将报告数据渲染为美观的 HTML 文件，
        # 返回的路径存储在 report.html_path 中供后续使用
        html_path = self.renderer.render(report)
        report.html_path = html_path

        # ── Step 6: 可选 — 自动打开浏览器 ──
        # 使用 Python 标准库 webbrowser 模块，
        # 以 file:// 协议在默认浏览器中打开生成的 HTML 报告
        if open_browser:
            import webbrowser
            webbrowser.open(f"file://{html_path}")

        return report

    def generate_from_stdout(
        self,
        stdout: str,
        test_name: str = "unknown",
        platform: str = "android",
        device_id: str | None = None,
    ) -> TestReport:
        """
        从 maestro --analyze 的 stdout 直接生成报告。

        适用场景:
          当没有完整的 Maestro 日志目录（~/.maestro/tests/），
          只有 maestro test 命令的终端输出时，可以使用此方法快速生成报告。

        典型用例:
          1. CI/CD 流水线中只捕获了 stdout 文本
          2. 手动运行 maestro test 后复制了终端输出
          3. 远程执行环境中没有文件系统访问权限

        与 generate_report() 的区别:
          - generate_report() 需要本地有 ~/.maestro/tests/ 日志目录
          - generate_from_stdout() 只需要 stdout 文本字符串，
            不依赖本地文件系统（除了最终报告输出目录）

        Args:
            stdout: maestro test 命令的完整标准输出文本
            test_name: 测试名称（默认为 "unknown"）
            platform: 平台标识，如 "android" / "ios"
            device_id: 设备 ID（可选）

        Returns:
            TestReport — 包含 AI 洞察的完整报告对象
        """
        # 以当前时间作为报告的生成时间和测试执行时间
        now = datetime.now()
        # 从 stdout 文本中解析步骤信息（✓/✗ 符号 + 描述 + 耗时）
        steps = self.parser.parse_steps_from_stdout(stdout)

        # 统计通过/失败步骤数
        passed = sum(1 for s in steps if s.get("status") == "passed")
        failed = sum(1 for s in steps if s.get("status") == "failed")

        # 根据失败步骤数构造 ExecutionResult
        # 注意：stdout 场景下没有精确的时间戳和截图，
        # 所有时间统一使用当前时间，截图列表为空
        result = ExecutionResult(
            test_name=test_name,
            platform=platform,
            device_id=device_id,
            status=TestStatus.FAILED if failed > 0 else TestStatus.PASSED,
            start_time=now,
            end_time=now,
            total_steps=len(steps),
            passed_steps=passed,
            failed_steps=failed,
            raw_maestro_log=stdout,  # 原始 stdout 保存为 raw_maestro_log，用于报告展示
        )

        # 复用 generate_report() 完成后续的 AI 分析和 HTML 渲染流程
        return self.generate_report(result)
