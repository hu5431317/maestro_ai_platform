"""测试 AI 自愈引擎模块 — ElementHealer。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.core.healing import (
    ElementFingerprintInternal,
    ElementHealer,
    LocatorStrategyGenerator,
    FuzzyElementMatcher,
    YamlUpdater,
)
from src.models.schemas import LocatorStrategy


# ── 测试 XML fixture ──

SAMPLE_UI_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node index="0" text="Welcome" resource-id="com.example:id/welcome" class="android.widget.TextView" content-desc=""/>
  <node index="1" text="Login" resource-id="com.example:id/login_btn" class="android.widget.Button" content-desc="Login button"/>
  <node index="2" text="Username" resource-id="com.example:id/username_input" class="android.widget.EditText" content-desc=""/>
  <node index="3" text="Password" resource-id="com.example:id/password_input" class="android.widget.EditText" content-desc=""/>
  <node index="4" text="Submit" resource-id="com.example:id/submit_btn" class="android.widget.Button" content-desc="Submit form"/>
</hierarchy>"""


class TestElementFingerprintInternal:
    def test_from_dict(self):
        data = {"text": "Login", "resource-id": "com.example:id/login_btn"}
        fp = ElementFingerprintInternal.from_dict(data)
        assert fp.text == "Login"
        assert fp.resource_id == "com.example:id/login_btn"

    def test_to_dict_uses_resource_id_key(self):
        fp = ElementFingerprintInternal(text="Login", resource_id="com.example:id/btn")
        d = fp.to_dict()
        assert d["resource-id"] == "com.example:id/btn"
        assert d["text"] == "Login"


class TestLocatorStrategyGenerator:
    def test_generate_with_text(self):
        fp = ElementFingerprintInternal(text="Login")
        strategies = LocatorStrategyGenerator.generate(fp)
        assert len(strategies) >= 3
        assert any(s.strategy_type == "text_fuzzy" for s in strategies)

    def test_generate_with_resource_id_no_text(self):
        fp = ElementFingerprintInternal(
            resource_id="com.example:id/login_btn",
            class_name="android.widget.Button",
        )
        strategies = LocatorStrategyGenerator.generate(fp)
        assert len(strategies) >= 3
        types = [s.strategy_type for s in strategies]
        assert "class_match" in types or "resource_id_partial" in types

    def test_strategies_sorted_by_priority(self):
        fp = ElementFingerprintInternal(text="Login", content_desc="Login button")
        strategies = LocatorStrategyGenerator.generate(fp)
        priorities = [s.priority for s in strategies]
        assert priorities == sorted(priorities)


class TestFuzzyElementMatcher:
    def test_find_exact_match(self):
        # 使用与 XML 节点完全匹配的指纹（包含所有属性）
        fp = ElementFingerprintInternal(
            text="Login",
            resource_id="com.example:id/login_btn",
            class_name="android.widget.Button",
            content_desc="Login button",
        )
        match = FuzzyElementMatcher.find_best_match(SAMPLE_UI_XML, fp)
        assert match is not None
        assert match.get("resource-id") == "com.example:id/login_btn"

    def test_find_with_high_similarity(self):
        fp = ElementFingerprintInternal(
            text="Submit",
            resource_id="com.example:id/submit_btn",
            class_name="android.widget.Button",
            content_desc="Submit form",
        )
        match = FuzzyElementMatcher.find_best_match(SAMPLE_UI_XML, fp)
        assert match is not None
        assert match.get("text") == "Submit"

    def test_no_match_with_low_similarity(self):
        fp = ElementFingerprintInternal(text="NonExistentElementXYZ")
        match = FuzzyElementMatcher.find_best_match(SAMPLE_UI_XML, fp, threshold=0.85)
        assert match is None

    def test_custom_threshold(self):
        # 使用与 XML 相近的指纹，在宽松阈值下应匹配
        fp = ElementFingerprintInternal(
            text="Welcome",
            class_name="android.widget.TextView",
        )
        # 严格阈值不应匹配
        match_strict = FuzzyElementMatcher.find_best_match(SAMPLE_UI_XML, fp, threshold=0.95)
        assert match_strict is None

        # 宽松阈值应匹配
        match_loose = FuzzyElementMatcher.find_best_match(SAMPLE_UI_XML, fp, threshold=0.4)
        assert match_loose is not None


class TestYamlUpdater:
    def test_update_locator_success(self):
        yaml_content = """appId: com.example.app
---
- tapOn: "Login"
- inputText: "admin"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            result = YamlUpdater.update_locator(
                yaml_path, '"Login"', '"Login_v2"', backup=False
            )
            assert result is True
            updated = yaml_path.read_text(encoding="utf-8")
            assert '"Login_v2"' in updated
            assert '"Login"' not in updated  # old locator replaced
        finally:
            yaml_path.unlink(missing_ok=True)

    def test_update_locator_not_found(self):
        yaml_content = """appId: com.example.app
---
- tapOn: "Login"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            result = YamlUpdater.update_locator(
                yaml_path, "NonExistent", "NewThing", backup=False
            )
            assert result is False
        finally:
            yaml_path.unlink(missing_ok=True)


class TestElementHealer:
    def test_heal_full_flow(self):
        fp = ElementFingerprintInternal(
            text="Login",
            resource_id="com.example:id/login_btn",
            class_name="android.widget.Button",
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write('appId: com.example.app\n---\n- tapOn: "com.example:id/login_btn"\n')
            yaml_path = Path(f.name)

        try:
            healer = ElementHealer(similarity_threshold=0.8)
            result = healer.heal(
                fingerprint=fp,
                xml_content=SAMPLE_UI_XML,
                yaml_path=yaml_path,
                failed_locator='"com.example:id/login_btn"',
            )

            assert result["healed"] is True
            assert result["new_locator"] is not None
            assert len(result["alternatives"]) >= 3

        finally:
            yaml_path.unlink(missing_ok=True)

    def test_heal_fails_with_bad_xml(self):
        fp = ElementFingerprintInternal(text="Login")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write('- tapOn: "Login"\n')
            yaml_path = Path(f.name)

        try:
            healer = ElementHealer()
            result = healer.heal(
                fingerprint=fp,
                xml_content="<broken>xml",
                yaml_path=yaml_path,
                failed_locator='"Login"',
            )
            assert result["healed"] is False
        finally:
            yaml_path.unlink(missing_ok=True)

    def test_build_fingerprint_from_error_text(self):
        healer = ElementHealer()
        error = "Element with text 'Login' not found"
        fp = healer.build_fingerprint_from_error(error)
        assert fp.text == "Login"

    def test_build_fingerprint_from_error_resource_id(self):
        healer = ElementHealer()
        error = "No view matching id 'com.example:id/login_btn' found"
        fp = healer.build_fingerprint_from_error(error)
        assert fp.resource_id == "com.example:id/login_btn"
