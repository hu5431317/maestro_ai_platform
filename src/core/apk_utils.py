"""APK 工具模块 — 获取已安装应用的 packageId。

提供通过 adb / 设备直接查询 packageId 的功能，
无需手动解析 APK 二进制文件。
"""

import subprocess
import sys


def get_current_focused_app(device: str | None = None) -> str | None:
    """获取设备上当前正在前台运行的 App 的 packageId。

    通过 adb shell dumpsys window 获取当前焦点窗口，
    从中解析出 packageId。

    Args:
        device: 设备ID，None 表示使用默认设备

    Returns:
        packageId（如 "com.tencent.mm"），失败返回 None

    示例输出解析:
        mCurrentFocus=Window{abc com.tencent.mm/com.tencent.mm.ui.LauncherUI}
        → com.tencent.mm
    """
    cmd = ["adb"]
    if device:
        cmd.extend(["-s", device])
    cmd.extend(["shell", "dumpsys", "window"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        for line in result.stdout.split("\n"):
            if "mCurrentFocus" in line or "mFocusedApp" in line:
                # 从输出中提取 packageId
                # 格式: mCurrentFocus=Window{... com.example.app/com.example...}
                import re
                match = re.search(r'([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)/', line)
                if match:
                    return match.group(1)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


def list_installed_apps(device: str | None = None, filter_keyword: str = "") -> list[str]:
    """列出设备上已安装的第三方应用。

    Args:
        device: 设备ID
        filter_keyword: 过滤关键字（如 "wechat"）

    Returns:
        packageId 列表
    """
    cmd = ["adb"]
    if device:
        cmd.extend(["-s", device])
    cmd.extend(["shell", "pm", "list", "packages", "-3"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        packages = []
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("package:"):
                pkg = line[8:]
                if not filter_keyword or filter_keyword.lower() in pkg.lower():
                    packages.append(pkg)
        return packages
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def get_package_id_guide() -> str:
    """返回获取 packageId 的完整指南。"""
    return """
========== 获取 Android App packageId 的方法 ==========

【方法1】adb 命令（推荐，需要手机连接电脑）
  1. 打开目标 App
  2. 执行: adb shell dumpsys window | findstr mCurrentFocus
  3. 输出示例: com.tencent.mm/com.tencent.mm.ui.LauncherUI
               ^^^^^^^^^^^^^^ 这就是 packageId

【方法2】列出所有已安装应用
  adb shell pm list packages -3 | findstr <关键字>

【方法3】从 Google Play URL 获取
  打开 https://play.google.com/store/apps/details?id=com.tencent.mm
                                                     ^^^^^^^^^^^^^^

【方法4】从源码项目获取
  查看 android/app/build.gradle 中的 applicationId 字段
  或 AndroidManifest.xml 中的 package 属性

========================================================
"""


if __name__ == "__main__":
    # 命令行工具：直接获取当前前台 App 的 packageId
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        keyword = sys.argv[2] if len(sys.argv) > 2 else ""
        packages = list_installed_apps(filter_keyword=keyword)
        print(f"已安装应用 ({len(packages)} 个):")
        for p in packages:
            print(f"  {p}")
    elif len(sys.argv) > 1 and sys.argv[1] == "--guide":
        print(get_package_id_guide())
    else:
        # 默认：获取当前前台 App
        pkg = get_current_focused_app()
        if pkg:
            print(f"当前前台 App: {pkg}")
        else:
            print("未检测到 adb 连接或前台 App。请确保:")
            print("  1. 手机/模拟器已通过 USB 连接")
            print("  2. 已开启 USB 调试")
            print("  3. 执行 'adb devices' 确认设备已连接")
