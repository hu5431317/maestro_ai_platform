"""测试数据模型 (Pydantic schemas)。"""

from __future__ import annotations

import pytest
from datetime import datetime

from src.models.schemas import (
    ElementFingerprint,
    LocatorStrategy,
    Platform,
    TestStatus,
    RetryLevel,
    TestStep,
    TestCase,
    StepResult,
    ExecutionResult,
    AIInsight,
    TestReport,
    DeviceInfo,
    MCPRequest,
    MCPResponse,
)


class TestElementFingerprint:
    """ElementFingerprint 模型测试。"""

    def test_create_full_fingerprint(self):
        fp = ElementFingerprint(
            text="Login",
            resource_id="com.example:id/login_btn",
            content_desc="Login button",
            xpath="//android.widget.Button",
            class_name="android.widget.Button",
            index=0,
        )
        assert fp.text == "Login"
        assert fp.resource_id == "com.example:id/login_btn"
        assert fp.class_name == "android.widget.Button"

    def test_to_locator_str_prefers_resource_id(self):
        fp = ElementFingerprint(
            text="Login",
            resource_id="com.example:id/login_btn",
        )
        assert fp.to_locator_str() == "com.example:id/login_btn"

    def test_to_locator_fallback_to_text(self):
        fp = ElementFingerprint(text="Login")
        assert fp.to_locator_str() == "Login"

    def test_default_values(self):
        fp = ElementFingerprint()
        assert fp.text is None
        assert fp.resource_id is None
        assert fp.to_locator_str() == "unknown"


class TestLocatorStrategy:
    def test_create_strategy(self):
        s = LocatorStrategy(
            strategy_type="text_fuzzy",
            locator='contains(@text, "Login")',
            priority=1,
            description="Text fuzzy match",
        )
        assert s.strategy_type == "text_fuzzy"
        assert s.priority == 1


class TestPlatformEnum:
    def test_platform_values(self):
        assert Platform.ANDROID.value == "android"
        assert Platform.IOS.value == "ios"

    def test_platform_from_string(self):
        assert Platform("android") == Platform.ANDROID
        assert Platform("ios") == Platform.IOS


class TestExecutionResult:
    def test_create_result(self):
        now = datetime.now()
        result = ExecutionResult(
            test_name="login_test",
            platform=Platform.ANDROID,
            status=TestStatus.PASSED,
            start_time=now,
            end_time=now,
            total_steps=5,
            passed_steps=5,
            failed_steps=0,
        )
        assert result.test_name == "login_test"
        assert result.status == TestStatus.PASSED


class TestAIInsight:
    def test_create_insight(self):
        insight = AIInsight(
            step_index=1,
            root_cause="Element not found",
            suggestion="Check resource-id",
            confidence=0.9,
            visual_defect_detected=False,
        )
        assert insight.step_index == 1
        assert insight.confidence == 0.9

    def test_confidence_range(self):
        insight = AIInsight(
            step_index=1,
            root_cause="test",
            suggestion="test",
            confidence=0.5,
        )
        assert 0 <= insight.confidence <= 1


class TestMCPProtocol:
    def test_mcp_request(self):
        req = MCPRequest(method="generate", params={"description": "test"})
        assert req.method == "generate"
        assert req.params["description"] == "test"

    def test_mcp_response_success(self):
        resp = MCPResponse(success=True, data={"yaml_path": "flows/test.yaml"})
        assert resp.success is True
        assert resp.data["yaml_path"] == "flows/test.yaml"

    def test_mcp_response_error(self):
        resp = MCPResponse(success=False, error="Generation failed")
        assert resp.success is False
        assert resp.error == "Generation failed"
