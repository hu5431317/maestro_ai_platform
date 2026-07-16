"""测试编排器模块 — MaestroOrchestrator 与 MaestroCommand。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.core.orchestrator import MaestroCommand, ErrorParser, AIYamlFixer, MaestroOrchestrator
from src.models.schemas import Platform, TestStatus


class TestErrorParser:
    def test_is_element_not_found_english(self):
        assert ErrorParser.is_element_not_found("Element not found with id: login_btn")

    def test_is_element_not_found_chinese(self):
        assert ErrorParser.is_element_not_found("错误: 找不到元素 Login")

    def test_is_not_element_not_found(self):
        assert not ErrorParser.is_element_not_found("Network connection failed")

    def test_is_timeout(self):
        assert ErrorParser.is_timeout("Operation timed out after 30s")

    def test_is_not_timeout(self):
        assert not ErrorParser.is_timeout("All tests passed")

    def test_extract_failed_locator_text(self):
        locator = ErrorParser.extract_failed_locator(
            "Element with text 'Login' not found in hierarchy"
        )
        assert locator == "Login"

    def test_extract_failed_locator_id(self):
        locator = ErrorParser.extract_failed_locator(
            'No view matching id "com.example:id/btn" found'
        )
        assert locator == "com.example:id/btn"


class TestMaestroCommand:
    def test_init_default_path(self):
        cmd = MaestroCommand()
        # 默认路径可能是自动发现的 npm 路径或 "maestro"
        assert cmd.maestro_path in ("maestro",) or cmd.maestro_path.endswith("maestro.cmd")

    def test_init_custom_path(self):
        cmd = MaestroCommand(maestro_path="/usr/local/bin/maestro")
        assert cmd.maestro_path == "/usr/local/bin/maestro"

    def test_run_test_requires_maestro_cli(self):
        """测试在缺少 Maestro CLI 时应抛出 RuntimeError。"""
        cmd = MaestroCommand(maestro_path="__nonexistent_maestro_binary__")
        with pytest.raises(RuntimeError, match="未找到 maestro 命令"):
            cmd.run_test(yaml_path=Path("nonexistent.yaml"))


class TestMaestroOrchestrator:
    def test_init(self):
        orch = MaestroOrchestrator(
            maestro_path="maestro",
            l1_max_retries=3,
            l2_max_retries=2,
            l3_enabled=True,
        )
        assert orch.l1_max_retries == 3
        assert orch.l2_max_retries == 2
        assert orch.l3_enabled is True

    def test_init_l3_disabled(self):
        orch = MaestroOrchestrator(l3_enabled=False)
        assert orch.ai_fixer is None
        assert orch.l3_enabled is False

    def test_execute_missing_yaml(self):
        """测试 Maestro CLI 不可用时抛出 RuntimeError。"""
        orch = MaestroOrchestrator(maestro_path="maestro")
        with pytest.raises(RuntimeError, match="未找到 maestro 命令"):
            orch.execute(
                yaml_path=Path("flows/nonexistent.yaml"),
                device_id=None,
                heal=False,
            )


class TestAIYamlFixer:
    def test_init_without_api_key(self):
        fixer = AIYamlFixer()
        assert fixer.model == "deepseek-chat"

    def test_fix_returns_none_without_api_key(self):
        fixer = AIYamlFixer(api_key="fake_key_for_test")
        # 由于 API key 不是真实的，应该返回 None
        result = fixer.fix(
            error_log="Element not found",
            yaml_path=Path("nonexistent.yaml"),
        )
        # 可能抛异常或返回 None
        assert result is None

    def test_init_with_custom_model(self):
        os.environ["AI_MODEL"] = "gpt-4"
        try:
            fixer = AIYamlFixer()
            assert fixer.model == "gpt-4"
        finally:
            os.environ.pop("AI_MODEL", None)
