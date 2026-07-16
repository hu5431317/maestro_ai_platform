"""配置加载与解析工具模块。

提供 YAML 配置文件读取、应用别名解析、以及 testcases.md 文件
头部 front matter（---...---）解析能力。
"""

from pathlib import Path
from typing import Any

import yaml


# ── 配置路径常量 ──
_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"

# 模块级缓存，避免重复读取磁盘
_devices_cache: dict[str, Any] | None = None
_apps_cache: dict[str, Any] | None = None


def _load_yaml(filename: str, cache_key: str) -> dict[str, Any]:
    """读取 YAML 配置文件并缓存。

    读取 config/ 下的 YAML 文件，返回解析后的字典。
    使用模块级缓存避免重复 IO。

    Args:
        filename: 配置文件名（如 "devices.yaml"）
        cache_key: 缓存键名

    Returns:
        解析后的配置字典
    """
    global _devices_cache, _apps_cache

    if cache_key == "devices" and _devices_cache is not None:
        return _devices_cache
    if cache_key == "apps" and _apps_cache is not None:
        return _apps_cache

    path = _CONFIG_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    if cache_key == "devices":
        _devices_cache = data
    elif cache_key == "apps":
        _apps_cache = data

    return data


def load_devices() -> dict[str, Any]:
    """加载设备配置（含默认设备查找）。

    Returns:
        完整的设备配置字典，包含 devices、default_platform、timeout 等字段。
    """
    return _load_yaml("devices.yaml", "devices")


def load_apps() -> list[dict[str, Any]]:
    """加载应用注册列表。

    Returns:
        apps 列表，每项含 alias、app_id、platform、name 字段。
    """
    data = _load_yaml("apps.yaml", "apps")
    return data.get("apps", [])


def get_default_device() -> str | None:
    """获取默认设备 ID。

    遍历 devices.yaml 中所有设备，返回第一个 default=true 的设备。
    Android 优先于 iOS。

    Returns:
        设备ID（avd 或 udid），无默认设备时返回 None。
    """
    config = load_devices()
    devices_section = config.get("devices", {})

    # 按 Android → iOS 顺序查找
    for platform in ("android", "ios"):
        for dev in devices_section.get(platform, []):
            if dev.get("default"):
                return dev.get("avd") or dev.get("udid")

    return None


def resolve_device(raw: str) -> str:
    """解析设备标识符，支持名称查找。

    查找优先级:
      1. 先按 name 匹配（如 "oppo A32" → "8073fcda"）
      2. 再按 avd/udid 直接匹配（如 "8073fcda" → "8073fcda"）
      3. 都没匹配到，直接返回原始字符串（兼容直接写 adb ID 的情况）

    Args:
        raw: 设备名称或 adb 设备ID

    Returns:
        真实的 adb 设备ID（avd）或 iOS UDID
    """
    config = load_devices()
    devices_section = config.get("devices", {})

    # 遍历所有平台的所有设备
    for platform in ("android", "ios"):
        for dev in devices_section.get(platform, []):
            # 按 name 匹配
            if dev.get("name") == raw:
                return dev.get("avd") or dev.get("udid") or raw
            # 按 avd/udid 直接匹配
            if dev.get("avd") == raw or dev.get("udid") == raw:
                return raw

    # 未注册的设备，直接返回原始值（用户可能直写 adb ID）
    return raw


def resolve_app_id(raw: str) -> tuple[str, str]:
    """解析应用ID，支持别名（@xxx）和直接 packageId。

    若以 @ 开头，从 apps.yaml 查找对应别名并解析。
    否则直接返回原始字符串。

    Args:
        raw: 应用ID 或 别名（如 "@wechat" 或 "com.tencent.mm"）

    Returns:
        (app_id, platform) 元组:
          - app_id: 解析后的实际 packageId / bundleId
          - platform: 对应的平台

    Raises:
        ValueError: 别名在 apps.yaml 中未找到注册时抛出
    """
    if not raw.startswith("@"):
        return raw, ""  # 直接 packageId，平台由调用方从 CLI/devices 推断

    alias = raw.strip()
    apps = load_apps()

    for app in apps:
        if app.get("alias") == alias:
            # platform 可选：不填则返回空字符串，由 CLI 或 devices.yaml 推断
            return app["app_id"], app.get("platform", "")

    raise ValueError(
        f"未找到应用别名 '{alias}'。"
        f"请在 config/apps.yaml 中注册该应用。"
        f"\n已注册的别名: {[a.get('alias') for a in apps]}"
    )


def parse_front_matter(md_content: str) -> tuple[dict[str, str], str]:
    """解析 testcases.md 文件头部的 YAML front matter。

    格式示例:
        ---
        app_id: "@wechat"
        platform: android
        device: emulator-5554
        ---

        ## 登录模块
        打开App，点击登录...

    Args:
        md_content: 完整的 markdown 文件内容

    Returns:
        (config_dict, body) 元组:
          - config_dict: 从 front matter 解析出的配置键值对
          - body: 去除 front matter 后的剩余内容
    """
    content = md_content.strip()

    # 检查是否以 --- 开头
    if not content.startswith("---"):
        return {}, content

    # 查找闭合的 ---
    end_idx = content.find("---", 3)
    if end_idx == -1:
        return {}, content

    # 提取 front matter 部分（去除 # 注释后解析）
    front_raw = content[3:end_idx].strip()

    # 简单键值对解析（兼容 # 注释行）
    config: dict[str, str] = {}
    for line in front_raw.split("\n"):
        line = line.strip()
        # 跳过空行和注释行
        if not line or line.startswith("#"):
            continue
        # 解析 key: value
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            # 去除 value 中的行尾注释
            value = value.split("#")[0].strip().strip('"').strip("'")
            if key and value:
                config[key] = value

    # 返回 body（front matter 之后的部分）
    body = content[end_idx + 3:].strip()

    return config, body
