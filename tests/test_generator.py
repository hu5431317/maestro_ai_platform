"""测试自然语言 → YAML 生成器模块 — TestCaseGenerator。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.ai.generator import EntityExtractor, TestCaseGenerator, MAESTRO_SYSTEM_PROMPT


class TestEntityExtractor:
    def test_extract_chinese_click(self):
        steps = EntityExtractor.extract("点击登录按钮")
        # 第一步是自动插入的 launchApp
        assert len(steps) >= 2
        actions = [s["action"] for s in steps]
        assert "click" in actions

    def test_extract_chinese_input(self):
        steps = EntityExtractor.extract("输入账号admin")
        has_input = any(s["action"] == "input" for s in steps)
        assert has_input, f"应该包含输入动作，实际: {steps}"

    def test_extract_chinese_assert(self):
        steps = EntityExtractor.extract("验证首页是否显示欢迎语")
        assert any(s["action"] == "assert" for s in steps)

    def test_extract_chinese_launch(self):
        steps = EntityExtractor.extract("打开App")
        assert any(s["action"] == "launch" for s in steps)

    def test_extract_full_description(self):
        text = "打开App，点击登录，输入账号admin和密码123456，验证首页显示欢迎语"
        steps = EntityExtractor.extract(text)
        # 至少应有 launch + click + input + assert = 4 步
        assert len(steps) >= 4, f"提取步骤数不足: {len(steps)} steps: {steps}"
        assert steps[0]["action"] == "launch"

    def test_extract_english_click(self):
        steps = EntityExtractor.extract("Click the Login button")
        assert any(s["action"] == "click" for s in steps)

    def test_extract_english_input(self):
        steps = EntityExtractor.extract("Enter username as admin")
        assert any(s["action"] == "input" for s in steps)


class TestSystemPrompt:
    def test_prompt_contains_required_commands(self):
        assert "appId:" in MAESTRO_SYSTEM_PROMPT
        assert "launchApp" in MAESTRO_SYSTEM_PROMPT
        assert "tapOn" in MAESTRO_SYSTEM_PROMPT
        assert "assertVisible" in MAESTRO_SYSTEM_PROMPT
        assert "inputText" in MAESTRO_SYSTEM_PROMPT

    def test_prompt_prohibits_markdown_fences(self):
        assert "triple backticks" in MAESTRO_SYSTEM_PROMPT.lower()


class TestTestCaseGenerator:
    def test_init_without_api_key_does_not_fail(self):
        """无 API Key 时创建不会立即失败。"""
        gen = TestCaseGenerator()
        assert gen.flows_dir is not None

    def test_client_raises_without_api_key(self):
        gen = TestCaseGenerator(api_key=None)
        with pytest.raises(ValueError, match="未设置 API Key"):
            _ = gen.client

    def test_client_works_with_api_key(self):
        gen = TestCaseGenerator(api_key="test_key_123")
        client = gen.client
        assert client is not None

    def test_validate_yaml_valid(self):
        gen = TestCaseGenerator()
        with Path(__file__).with_name("_temp_valid.yaml") as p:
            p.write_text("appId: com.example.app\n---\n- launchApp\n- tapOn: Login\n",
                         encoding="utf-8")
            try:
                assert gen.validate_yaml(p) is True
            finally:
                p.unlink(missing_ok=True)

    def test_validate_yaml_invalid(self):
        gen = TestCaseGenerator()
        with Path(__file__).with_name("_temp_invalid.yaml") as p:
            p.write_text("invalid: [yaml: content", encoding="utf-8")
            try:
                assert gen.validate_yaml(p) is False
            finally:
                p.unlink(missing_ok=True)

    def test_clean_yaml_output_removes_fences(self):
        raw = '```yaml\nappId: com.example.app\n---\n- launchApp\n```'
        cleaned = TestCaseGenerator._clean_yaml_output(raw)
        assert "```" not in cleaned
        assert "appId:" in cleaned
