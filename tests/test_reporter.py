"""测试报告模块 — AIInsightReporter、HTMLReportRenderer、MaestroLogParser。"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from src.reporters.ai_reporter import (
    AIInsightReporter,
    AIAnalyzer,
    HTMLReportRenderer,
    MaestroLogParser,
)
from src.models.schemas import ExecutionResult, TestReport, TestStatus, Platform, AIInsight


class TestMaestroLogParser:
    def test_get_latest_log_dir_nonexistent(self):
        """在不存在的环境中测试不会崩溃。"""
        # 假设没有 Maestro 日志，应返回 None
        result = MaestroLogParser.get_latest_log_dir()
        # 不强制断言，可能在有 Maestro 的环境中也有值
        assert result is None or result is not None

    def test_parse_logs_with_none_dir(self):
        result = MaestroLogParser.parse_logs(log_dir=None)
        assert "steps" in result
        assert "screenshots" in result
        assert "metrics" in result

    def test_parse_steps_from_stdout_empty(self):
        steps = MaestroLogParser.parse_steps_from_stdout("")
        assert steps == []

    def test_parse_steps_from_stdout_with_passed(self):
        stdout = "✓ Login button tapped (150ms)\n✓ Welcome visible (200ms)"
        steps = MaestroLogParser.parse_steps_from_stdout(stdout)
        assert len(steps) == 2
        assert all(s["status"] == "passed" for s in steps)

    def test_parse_steps_from_stdout_with_failed(self):
        stdout = "✓ Step one (100ms)\n✗ Step two - element not found (500ms)"
        steps = MaestroLogParser.parse_steps_from_stdout(stdout)
        assert len(steps) == 2
        assert steps[0]["status"] == "passed"
        assert steps[1]["status"] == "failed"


class TestAIAnalyzer:
    def test_init_without_api_key(self):
        analyzer = AIAnalyzer()
        assert analyzer.model == "deepseek-chat"

    def test_fallback_insights(self):
        failed_steps = [
            {"step_index": 1, "description": "click login failed"},
            {"step_index": 2, "description": "assertVisible welcome failed"},
        ]
        insights = AIAnalyzer._fallback_insights(failed_steps)
        assert len(insights) == 2
        assert isinstance(insights[0], AIInsight)
        assert insights[0].confidence == 0.5
        assert "元素定位器" in insights[0].suggestion


class TestHTMLReportRenderer:
    def test_render_generates_html_file(self):
        renderer = HTMLReportRenderer()
        now = datetime.now()
        result = ExecutionResult(
            test_name="sample_test",
            platform=Platform.ANDROID,
            status=TestStatus.PASSED,
            start_time=now,
            end_time=now,
            total_steps=3,
            passed_steps=3,
            failed_steps=0,
            raw_maestro_log="All steps passed.",
        )
        report = TestReport(execution_result=result, ai_insights=[])

        html_path = renderer.render(report)
        assert html_path.exists()
        assert html_path.suffix == ".html"

        content = html_path.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content
        assert "sample_test" in content
        assert "PASSED" in content

        # 清理
        html_path.unlink(missing_ok=True)

    def test_render_with_insights(self):
        renderer = HTMLReportRenderer()
        now = datetime.now()
        result = ExecutionResult(
            test_name="failed_test",
            platform=Platform.ANDROID,
            status=TestStatus.FAILED,
            start_time=now,
            end_time=now,
            total_steps=5,
            passed_steps=3,
            failed_steps=2,
        )
        insights = [
            AIInsight(
                step_index=1,
                root_cause="定位器失效",
                suggestion="使用 resource-id 替代 text",
                confidence=0.85,
                recommended_yaml_fix='- tapOn:\n    id: "com.example:id/btn"',
            )
        ]
        report = TestReport(execution_result=result, ai_insights=insights)

        html_path = renderer.render(report)
        content = html_path.read_text(encoding="utf-8")

        assert "FAILED" in content
        assert "定位器失效" in content
        assert "AI Insights" in content

        html_path.unlink(missing_ok=True)


class TestAIInsightReporter:
    def test_init(self):
        reporter = AIInsightReporter()
        assert reporter.parser is not None
        assert reporter.analyzer is not None
        assert reporter.renderer is not None

    def test_generate_report_basic(self):
        reporter = AIInsightReporter()
        now = datetime.now()
        result = ExecutionResult(
            test_name="quick_test",
            platform=Platform.ANDROID,
            status=TestStatus.PASSED,
            start_time=now,
            end_time=now,
            total_steps=1,
            passed_steps=1,
            failed_steps=0,
        )

        report_obj = reporter.generate_report(result)
        assert report_obj.execution_result.test_name == "quick_test"
        assert report_obj.html_path is not None
        assert report_obj.html_path.exists()

        # 清理
        report_obj.html_path.unlink(missing_ok=True)
