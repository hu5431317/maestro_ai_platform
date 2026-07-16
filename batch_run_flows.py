#!/usr/bin/env python3
"""
批量执行 flows/ 目录下所有 YAML 测试用例，并生成聚合测试报告。

用法:
    cd maestro_ai_platform
    python batch_run_flows.py
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 将项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# 将 adb 加入 PATH
adb_dir = Path.home() / "adb-platform-tools"
if adb_dir.exists():
    os.environ["PATH"] = str(adb_dir) + os.pathsep + os.environ.get("PATH", "")

# 设置 maestro CLI 路径 (Maestro 2.6.1)
os.environ["MAESTRO_CLI_PATH"] = r"C:\maestro\maestro\bin\maestro.bat"

# 设置 JAVA_HOME (Maestro 必需)
os.environ["JAVA_HOME"] = r"C:\Program Files\Java\jdk-17"

# 添加 Maestro bin 到 PATH
maestro_bin = r"C:\maestro\maestro\bin"
if maestro_bin not in os.environ["PATH"]:
    os.environ["PATH"] = maestro_bin + os.pathsep + os.environ["PATH"]

# 禁用 Maestro 分析通知横幅
os.environ["MAESTRO_CLI_ANALYSIS_NOTIFICATION_DISABLED"] = "true"

# 避免每次测试后卸载驱动 APK，保持 dev.mobile.maestro 常驻
os.environ["MAESTRO_CLI_NO_APP_UNINSTALL"] = "true"


def main():
    from src.core.orchestrator import MaestroOrchestrator
    from src.models.schemas import ExecutionResult, TestStatus, Platform
    from src.reporters.ai_reporter import AIInsightReporter

    flows_dir = PROJECT_ROOT / "flows"
    yaml_files = sorted(flows_dir.glob("generated_*.yaml"))

    if not yaml_files:
        print("未找到 YAML 测试文件!")
        return

    print(f"{'=' * 60}")
    print(f"批量执行测试用例 - {len(yaml_files)} 个文件")
    print(f"设备: 8073fcda (oppo A32)")
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}\n")

    orch = MaestroOrchestrator(l1_max_retries=1, l2_max_retries=1, l3_enabled=True)
    results: list[ExecutionResult] = []

    for idx, yaml_path in enumerate(yaml_files, 1):
        print(f"[{idx}/{len(yaml_files)}] 执行: {yaml_path.name}")
        print(f"  YAML 内容预览:")
        try:
            content = yaml_path.read_text(encoding="utf-8").strip()
            first_line = content.split("\n")[0] if content else "(空)"
            print(f"  appId: {first_line}")
            # 统计步骤数
            steps_count = sum(1 for line in content.split("\n") if line.strip().startswith("- "))
            print(f"  步骤数: {steps_count}")
        except Exception:
            pass

        start = time.time()
        try:
            result = orch.execute(
                yaml_path=yaml_path,
                device_id="8073fcda",
                platform="android",
                heal=True,
                max_retries=1,
            )
        except Exception as e:
            print(f"  [异常] 执行出错: {e}")
            result = ExecutionResult(
                test_name=yaml_path.stem,
                platform=Platform.ANDROID,
                device_id="8073fcda",
                status=TestStatus.ERROR,
                start_time=datetime.now(),
                end_time=datetime.now(),
                raw_maestro_log=str(e),
                yaml_path=yaml_path,
            )

        elapsed = time.time() - start
        results.append(result)

        status_icon = "PASS" if result.status == TestStatus.PASSED else (
            "HEAL" if result.status == TestStatus.HEALED else "FAIL"
        )
        print(f"  结果: {status_icon} | 耗时: {elapsed:.1f}s | 状态: {result.status.value}")
        print()

    # ── 生成聚合报告 ──
    print(f"{'=' * 60}")
    print("生成聚合测试报告...")

    passed = sum(1 for r in results if r.status == TestStatus.PASSED)
    healed = sum(1 for r in results if r.status == TestStatus.HEALED)
    failed = sum(1 for r in results if r.status in (TestStatus.FAILED, TestStatus.ERROR))
    total_duration = sum(r.duration_ms for r in results)

    # 生成 HTML 报告
    html = _generate_html_report(results, passed, healed, failed, total_duration)
    reports_dir = PROJECT_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"batch_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    report_path.write_text(html, encoding="utf-8")

    # ── 打印摘要 ──
    print(f"\n{'=' * 60}")
    print(f"                    测试报告摘要")
    print(f"{'=' * 60}")
    print(f"  总用例数:   {len(results)}")
    print(f"  通过:       {passed}")
    print(f"  自愈通过:   {healed}")
    print(f"  失败:       {failed}")
    print(f"  总耗时:     {total_duration:.0f}ms")
    print(f"  通过率:     {(passed + healed) / len(results) * 100:.1f}%")
    print(f"{'=' * 60}")
    print(f"  报告文件:   {report_path}")
    print(f"{'=' * 60}")

    # 对失败的用例生成 AI 分析报告
    if failed > 0:
        print("\n为失败用例生成 AI 分析报告...")
        reporter = AIInsightReporter()
        for r in results:
            if r.status in (TestStatus.FAILED, TestStatus.ERROR):
                try:
                    test_report = reporter.generate_report(r)
                    print(f"  {r.test_name} -> {test_report.html_path}")
                except Exception as e:
                    print(f"  {r.test_name} -> AI报告生成失败: {e}")


def _generate_html_report(
    results: list,
    passed: int,
    healed: int,
    failed: int,
    total_duration: float,
) -> str:
    """生成聚合 HTML 测试报告。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = len(results)
    pass_rate = (passed + healed) / total * 100 if total > 0 else 0

    rows_html = ""
    for i, r in enumerate(results, 1):
        status_cls = "status-passed" if r.status.value == "passed" else (
            "status-healed" if r.status.value == "healed" else "status-failed"
        )
        status_icon = "&#10004;" if r.status.value == "passed" else (
            "&#9888;" if r.status.value == "healed" else "&#10008;"
        )
        duration_s = r.duration_ms / 1000 if r.duration_ms else 0
        log_preview = ""
        if r.raw_maestro_log:
            log_preview = r.raw_maestro_log[-300:].replace("<", "&lt;").replace(">", "&gt;")

        rows_html += f"""
            <tr>
                <td>{i}</td>
                <td class="test-name">{r.test_name}</td>
                <td><span class="status-badge {status_cls}">{status_icon} {r.status.value.upper()}</span></td>
                <td>{duration_s:.1f}s</td>
                <td class="log-preview" title="{log_preview}">{log_preview[:80]}...</td>
            </tr>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Maestro AI - 批量测试报告</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; background: #f5f7fa; color: #333; line-height: 1.6; }}
        .container {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; padding: 32px; border-radius: 12px; margin-bottom: 24px; }}
        .header h1 {{ font-size: 24px; margin-bottom: 8px; }}
        .header .meta {{ opacity: 0.85; font-size: 14px; }}
        .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 24px; }}
        .summary-item {{ background: #fff; border-radius: 8px; padding: 20px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
        .summary-item .value {{ font-size: 32px; font-weight: 700; }}
        .summary-item .label {{ font-size: 13px; color: #888; margin-top: 4px; }}
        .card {{ background: #fff; border-radius: 8px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
        .card h2 {{ font-size: 18px; margin-bottom: 12px; color: #555; border-bottom: 2px solid #eee; padding-bottom: 8px; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{ background: #f8f9fa; padding: 10px 12px; text-align: left; font-size: 13px; color: #666; border-bottom: 2px solid #dee2e6; }}
        td {{ padding: 10px 12px; border-bottom: 1px solid #eee; font-size: 13px; }}
        tr:hover {{ background: #f8f9ff; }}
        .test-name {{ font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 12px; }}
        .status-badge {{ display: inline-block; padding: 3px 10px; border-radius: 12px; font-weight: 600; font-size: 12px; }}
        .status-passed {{ background: #d4edda; color: #155724; }}
        .status-failed {{ background: #f8d7da; color: #721c24; }}
        .status-healed {{ background: #fff3cd; color: #856404; }}
        .log-preview {{ font-family: monospace; font-size: 11px; color: #999; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .footer {{ text-align: center; padding: 20px; color: #aaa; font-size: 13px; }}
        .pass-rate-bar {{ background: #e9ecef; border-radius: 10px; height: 20px; margin-top: 8px; overflow: hidden; }}
        .pass-rate-fill {{ height: 100%; border-radius: 10px; background: linear-gradient(90deg, #28a745, #20c997); }}
        .pass-rate-fill.warn {{ background: linear-gradient(90deg, #ffc107, #fd7e14); }}
        .pass-rate-fill.danger {{ background: linear-gradient(90deg, #dc3545, #e83e8c); }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Maestro AI Platform - 批量测试报告</h1>
            <div class="meta">
                设备: oppo A32 (8073fcda) | 平台: Android |
                生成时间: {now}
            </div>
        </div>

        <div class="summary-grid">
            <div class="summary-item">
                <div class="value">{total}</div>
                <div class="label">总用例数</div>
            </div>
            <div class="summary-item">
                <div class="value" style="color:#155724;">{passed}</div>
                <div class="label">通过</div>
            </div>
            <div class="summary-item">
                <div class="value" style="color:#856404;">{healed}</div>
                <div class="label">自愈通过</div>
            </div>
            <div class="summary-item">
                <div class="value" style="color:#721c24;">{failed}</div>
                <div class="label">失败</div>
            </div>
            <div class="summary-item">
                <div class="value">{total_duration:.0f}ms</div>
                <div class="label">总耗时</div>
            </div>
        </div>

        <div class="card">
            <h2>通过率</h2>
            <div style="font-size: 24px; font-weight: 700; color: {'#155724' if pass_rate >= 80 else '#856404' if pass_rate >= 50 else '#721c24'};">{pass_rate:.1f}%</div>
            <div class="pass-rate-bar">
                <div class="pass-rate-fill{'' if pass_rate >= 80 else ' warn' if pass_rate >= 50 else ' danger'}" style="width: {pass_rate}%;"></div>
            </div>
        </div>

        <div class="card">
            <h2>用例执行详情</h2>
            <table>
                <thead>
                    <tr>
                        <th>#</th>
                        <th>用例名称</th>
                        <th>状态</th>
                        <th>耗时</th>
                        <th>日志预览</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
        </div>

        <div class="footer">
            Generated by Maestro AI Platform | {now}
        </div>
    </div>
</body>
</html>"""


if __name__ == "__main__":
    main()
