"""
阶段六：CLI 入口 — 基于 Click 的命令行工具

提供 generate / run / record / report 四个核心命令。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("maestro-ai")

# 将项目根加入 sys.path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ═══════════════════════════════════════════
# 帮助工具
# ═══════════════════════════════════════════

def _load_env():
    """加载 .env 环境变量。

    工作流程：
    1. 尝试从 config/.env 加载环境变量（使用 python-dotenv）。
    2. 若 .env 文件不存在，则静默跳过，仅依赖系统已有的环境变量。
    3. 若 python-dotenv 未安装（ImportError），同样静默跳过，
       确保 CLI 在没有该依赖时也能正常运行。

    该函数在每次 CLI 启动时由 cli() 入口组自动调用，
    确保所有环境变量（如 API_KEY、AI_MODEL 等）在子命令执行前已就绪。
    """
    try:
        from dotenv import load_dotenv
        env_path = PROJECT_ROOT / "config" / ".env"
        if env_path.exists():
            load_dotenv(env_path)
        else:
            # .env 文件缺失，记录调试日志后跳过
            logger.debug("未找到 .env 文件，使用系统环境变量")
    except ImportError:
        # python-dotenv 未安装，跳过加载
        pass


# ═══════════════════════════════════════════
# CLI 入口组
# ═══════════════════════════════════════════

# @click.group() 将 cli 函数标记为 CLI 的根命令组。
# 所有子命令（generate/run/record/report）都通过 @cli.command() 注册到该组下。
# @click.version_option 自动添加 --version 选项，显示版本号 1.0.0。
@click.group()
@click.version_option(version="1.0.0", prog_name="maestro-ai")
def cli():
    """Maestro AI Platform - 具备自愈与 AI 生成能力的移动端自动化测试平台。

    CLI 入口点：每次执行 maestro-ai 命令时，Click 会先调用此函数，
    然后根据子命令路由到对应的处理函数。
    """
    # 在所有子命令执行前，加载 .env 中的环境变量
    _load_env()


# ═══════════════════════════════════════════
# generate — 自然语言生成用例
# ═══════════════════════════════════════════

@cli.command()
@click.argument("description", required=False, default="")  # 可选：from-file 模式下不需要该参数
@click.option(
    "--platform",
    default="android",
    type=click.Choice(["android", "ios"]),  # 限定只能选 android 或 ios
    help="目标平台",
)
@click.option("--app-id", default="com.example.app", help="App 的 packageId / bundleId")
@click.option("--model", default=None, help="使用的 AI 模型（覆盖环境变量 AI_MODEL）")
@click.option("--validate", is_flag=True, help="生成后验证 YAML 语法")  # is_flag=True 表示布尔开关，不需要传值
@click.option("--from-file", "input_file", default=None, type=click.Path(exists=True),
              help="从文件读取自然语言描述（如 .md / .txt）")
def generate(description: str, platform: str, app_id: str, model: str | None,
             validate: bool, input_file: str | None):
    """从自然语言描述生成 Maestro YAML 测试用例。

    \b
    示例:
      maestro-ai generate "打开App，点击登录，输入账号admin和密码123456，验证首页显示欢迎语"
      maestro-ai generate --from-file testcases.md
      maestro-ai generate "Open app, tap Login" --platform ios

    \b
    两种输入方式:
      - 直接传入描述文本：maestro-ai generate "描述"
      - 从文件读取：maestro-ai generate --from-file testcases.md
    """
    # ── 确定描述来源 ──
    if input_file:
        # 从文件读取描述文本
        try:
            file_path = Path(input_file)
            description = file_path.read_text(encoding="utf-8").strip()
            click.echo(f"从文件读取用例描述: {input_file}")
        except Exception as e:
            click.secho(f"读取文件失败: {e}", fg="red", err=True)
            sys.exit(1)

    # 两个来源都没提供描述
    if not description.strip():
        click.secho(
            "请提供用例描述，或使用 --from-file 指定文件。\n"
            "示例: maestro-ai generate '打开App，点击登录'",
            fg="yellow",
        )
        sys.exit(1)

    try:
        from src.ai.generator import TestCaseGenerator
        from src.core.config_loader import resolve_app_id

        # 解析应用别名（如 @wechat → com.tencent.mm）
        resolved_app_id, resolved_platform = resolve_app_id(app_id)
        # CLI 未显式指定 platform 时，使用别名注册的 platform
        if resolved_platform and platform == "android":
            platform = resolved_platform

        # 初始化生成器，传入 model 参数以覆盖默认 AI 模型
        generator = TestCaseGenerator(model=model)
        # 调用 AI 生成 YAML 测试用例
        yaml_path = generator.generate(
            description=description,
            app_id=resolved_app_id,
            platform=platform,
        )

        # 输出生成的文件路径及内容
        click.echo(f"\nYAML 用例已生成: {yaml_path}")
        click.echo(yaml_path.read_text(encoding="utf-8"))

        # --validate 标志：在生成后执行 YAML 语法验证
        if validate:
            is_valid = generator.validate_yaml(yaml_path)
            if is_valid:
                click.secho("YAML 语法验证通过!", fg="green")
            else:
                click.secho("YAML 语法验证失败，请检查生成结果。", fg="yellow")

    except Exception as e:
        click.secho(f"生成失败: {e}", fg="red", err=True)
        sys.exit(1)


# ═══════════════════════════════════════════
# generate-batch — 从文件批量生成多个用例
# ═══════════════════════════════════════════

@cli.command()
@click.argument("file_path", type=click.Path(exists=True))
@click.option(
    "--platform",
    default="android",
    type=click.Choice(["android", "ios"]),
    help="目标平台",
)
@click.option("--app-id", default="com.example.app", help="App 的 packageId / bundleId")
@click.option("--model", default=None, help="使用的 AI 模型")
@click.option("--delimiter", default="##", help="用例分隔符，默认用 ## 标题分割")
def generate_batch(file_path: str, platform: str, app_id: str, model: str | None,
                   delimiter: str):
    """从 .md/.txt 文件批量生成多个 Maestro YAML 用例。

    \b
    文件格式示例 (testcases.md):
        ## 登录模块
        打开App，点击登录，输入账号admin和密码123456，验证首页显示欢迎语

        ## 注册模块
        打开App，点击注册，输入手机号13800138000，验证提示发送成功

    \b
    使用:
      maestro-ai generate-batch testcases.md
      maestro-ai generate-batch cases.txt --delimiter "===" --app-id com.my.app
    """
    try:
        from src.ai.generator import TestCaseGenerator
        from src.core.config_loader import parse_front_matter, resolve_app_id, resolve_device

        # ── 读取文件并解析 front matter ──
        raw_content = Path(file_path).read_text(encoding="utf-8").strip()
        fm_config, content = parse_front_matter(raw_content)

        # front matter 中的配置作为默认值，CLI 参数可覆盖
        fm_app_id = fm_config.get("app_id", "")
        fm_platform = fm_config.get("platform", platform)
        fm_device = fm_config.get("device", "")

        if fm_app_id:
            # 解析别名或直接使用 packageId
            resolved_app_id, resolved_platform = resolve_app_id(fm_app_id)
            final_app_id = app_id if app_id != "com.example.app" else resolved_app_id
            final_platform = platform if platform != "android" or not resolved_platform else resolved_platform
        else:
            final_app_id, _ = resolve_app_id(app_id)
            final_platform = platform

        # 解析设备名 → 真实 adb ID（如 "oppo A32" → "8073fcda"）
        if fm_device:
            actual_device = resolve_device(fm_device)
            click.echo(f"设备: {fm_device} (adb ID: {actual_device})  |  应用: {final_app_id}  |  平台: {final_platform}")

        # ── 按分隔符拆分 ──
        blocks = content.split(delimiter)

        generator = TestCaseGenerator(model=model)
        generated: list[Path] = []
        skipped: list[str] = []

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # ── 提取标题（第一行）和描述（剩余部分）──
            lines = block.split("\n", 1)
            title = lines[0].strip().lstrip("#").strip() if lines else "unnamed"
            desc = lines[1].strip() if len(lines) > 1 else lines[0].strip()

            # 跳过太短的块
            if len(desc) < 5:
                skipped.append(title)
                continue

            click.echo(f"\n{'─' * 50}")
            click.echo(f"生成用例: {title}")
            click.echo(f"描述: {desc[:80]}{'...' if len(desc) > 80 else ''}")

            try:
                yaml_path = generator.generate(
                    description=desc,
                    app_id=final_app_id,
                    platform=final_platform,
                )
                generated.append(yaml_path)
                click.secho(f"  -> 已生成: {yaml_path.name}", fg="green")
            except Exception as e:
                click.secho(f"  -> 失败: {e}", fg="red")
                skipped.append(f"{title}: {e}")

        # ── 汇总输出 ──
        click.echo(f"\n{'═' * 50}")
        click.secho(f"批量生成完成: 成功 {len(generated)} 个, 跳过 {len(skipped)} 个", fg="cyan")
        for yp in generated:
            click.echo(f"  {yp}")

        if skipped:
            click.secho(f"\n跳过的用例:", fg="yellow")
            for s in skipped:
                click.echo(f"  - {s}")

    except Exception as e:
        click.secho(f"批量生成失败: {e}", fg="red", err=True)
        sys.exit(1)


# ═══════════════════════════════════════════
# run — 执行已有 YAML 并开启自愈
# ═══════════════════════════════════════════

@cli.command()
@click.argument("yaml_path", type=click.Path(exists=True))  # exists=True 确保文件必须存在
@click.option("--heal/--no-heal", default=True, help="启用/禁用自愈")
# --heal/--no-heal 是一对互斥的布尔开关：
#   --heal      → heal=True  （默认行为，启用自愈）
#   --no-heal   → heal=False （禁用自愈，错误时直接失败）
@click.option("--max-retries", default=3, type=int, help="最大重试次数")
@click.option("--device", default=None, help="目标设备 ID")
@click.option("--platform", default="android", type=click.Choice(["android", "ios"]), help="目标平台")
@click.option("--report/--no-report", default=True, help="执行后生成 AI 报告")
# --report/--no-report 同样是互斥布尔开关，默认生成报告
def run(yaml_path: str, heal: bool, max_retries: int, device: str | None, platform: str, report: bool):
    """执行 YAML 测试用例，支持自愈和 AI 分析。

    \b
    示例:
      maestro-ai run flows/login.yaml --heal --max-retries 3
      maestro-ai run flows/login.yaml --device emulator-5554 --no-heal

    流程（orchestrator 编排 + 自愈 + 报告生成）：
    1. 创建 MaestroOrchestrator 编排器：
       - l1_max_retries: 控制 L1 层级的最大重试次数
       - l3_enabled=True: 启用 L3 层级的 AI 自愈策略
    2. 调用 orchestrator.execute() 执行测试：
       - 根据 --heal/--no-heal 决定是否启用自愈机制
       - 失败时自动重试（最多 --max-retries 次）
    3. 输出测试结果（状态、耗时）。
    4. 若 --report 启用且测试未通过（非 PASSED），则调用 AIInsightReporter
       生成带有 AI 分析洞察的测试报告（HTML 格式）。
    """
    try:
        from pathlib import Path

        from src.core.config_loader import get_default_device, resolve_app_id, resolve_device
        from src.core.orchestrator import MaestroOrchestrator
        from src.models.schemas import TestStatus
        from src.reporters.ai_reporter import AIInsightReporter

        # 解析设备：--device 名称 → devices.yaml 查找 → 默认设备
        raw_device = device or get_default_device()
        final_device = resolve_device(raw_device) if raw_device else None
        if final_device:
            click.echo(f"使用设备: {final_device}")

        # 初始化编排器，设置 L1 重试次数和 L3 自愈策略
        orch = MaestroOrchestrator(
            l1_max_retries=max_retries,
            l3_enabled=True,
        )

        click.echo(f"执行测试: {yaml_path}")
        # 核心执行入口：orchestrator 负责整个测试生命周期
        result = orch.execute(
            yaml_path=Path(yaml_path),
            device_id=final_device,
            platform=platform,
            heal=heal,              # 传递自愈开关
            max_retries=max_retries, # 传递重试次数
        )

        # 输出测试执行摘要
        click.echo(f"\n结果: {result.status.value.upper()}")
        click.echo(f"耗时: {result.duration_ms:.0f}ms")

        # 仅当 --report 启用 且 测试结果非 PASSED 时才生成 AI 分析报告
        # 这避免了为已通过的测试浪费 AI 调用资源
        if report and result.status != TestStatus.PASSED:
            click.echo("生成 AI 报告...")
            reporter = AIInsightReporter()
            test_report = reporter.generate_report(result)
            click.echo(f"报告已生成: {test_report.html_path}")

        # 测试失败时用非零退出码通知调用方（如 CI/CD 流水线）
        if result.status == TestStatus.FAILED:
            sys.exit(1)

    except Exception as e:
        click.secho(f"执行失败: {e}", fg="red", err=True)
        sys.exit(1)


# ═══════════════════════════════════════════
# record — 录制模式
# ═══════════════════════════════════════════

@cli.command()
@click.option("--save-to", default=None, type=click.Path(), help="保存录制结果的文件路径")
@click.option("--device", default=None, help="目标设备 ID")
def record(save_to: str | None, device: str | None):
    """启动 Maestro Studio 录制模式。

    \b
    此命令启动 maestro studio 并监听文件变化。
    录制完成后，生成的 YAML 将保存到 --save-to 指定路径。

    流程（子进程启动 + 中断处理 + YAML 保存）：
    1. 构建 maestro studio 命令，若指定 --device 则附加设备参数。
    2. 通过 subprocess.Popen 以子进程方式启动 Maestro Studio，
       不阻塞主进程，便于实时监听用户的中断信号。
    3. 轮询子进程状态（while process.poll() is None），等待用户完成录制。
    4. 捕获 KeyboardInterrupt（Ctrl+C）：
       - 优雅终止子进程（terminate() + wait()）
       - 避免僵尸进程残留
    5. 若指定 --save-to，则在 flows/ 目录下找到最新生成的 YAML 文件
       （按修改时间倒序排列），复制到目标路径。
    """
    try:
        import os
        import subprocess
        import time
        from pathlib import Path

        # 构建 maestro studio 命令
        cmd = ["maestro", "studio"]
        if device:
            # 可选：指定目标设备
            cmd.extend(["--device", device])

        click.echo(f"启动 Maestro Studio: {' '.join(cmd)}")
        click.echo("请在 Studio 中完成录制，按 Ctrl+C 结束录制。")

        # 以子进程方式启动 Maestro Studio，不阻塞当前进程
        # 这样可以在录制过程中继续监听用户输入
        process = subprocess.Popen(cmd)

        try:
            # 轮询等待子进程结束（用户可能在 Studio 中主动关闭）
            while process.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            # 用户按下 Ctrl+C：优雅地终止录制
            click.echo("\n录制已停止。")
            process.terminate()  # 向子进程发送终止信号
            process.wait()       # 等待子进程完全退出，防止僵尸进程

        # --save-to 选项：将录制结果保存到指定路径
        if save_to:
            # 查找 flows/ 目录下最新生成的 YAML 文件
            flows_dir = Path("flows")
            if flows_dir.exists():
                # 按文件修改时间降序排列，最新的排在最前
                yaml_files = sorted(
                    flows_dir.glob("*.yaml"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if yaml_files:
                    # 取最新的 YAML 文件复制到目标路径
                    latest = yaml_files[0]
                    import shutil
                    shutil.copy(latest, save_to)
                    click.echo(f"录制结果已保存: {save_to}")

    except FileNotFoundError:
        # Maestro CLI 未安装时的友好提示
        click.secho("未找到 maestro 命令。请确认 Maestro CLI 已安装。", fg="red", err=True)
        sys.exit(1)
    except Exception as e:
        click.secho(f"录制失败: {e}", fg="red", err=True)
        sys.exit(1)


# ═══════════════════════════════════════════
# report — 查看最新 AI 报告
# ═══════════════════════════════════════════

@cli.command()
@click.option("--open", "open_browser", is_flag=True, help="在浏览器中打开报告")
# 注意：--open 是 CLI 选项名，但实际映射到函数参数 open_browser
# 这是因为 Python 的 open 是内置函数，使用 open_browser 避免命名冲突
@click.option("--test-name", default="manual_test", help="测试名称")
def report(open_browser: bool, test_name: str):
    """生成并查看最新的 AI 测试报告。

    \b
    示例:
      maestro-ai report --open

    流程（日志解析 → 报告生成 → 浏览器打开）：
    1. 通过 reporter.parser.parse_logs() 解析最新的 Maestro 运行日志。
    2. 第一层回退（fallback）：若日志中无步骤数据（log_data["steps"] 为空），
       说明最近没有执行过测试，则查找 reports/ 目录下已有的报告文件：
       - 扫描 report_*.html 文件，按修改时间取最新的
       - 若 --open 启用，直接用浏览器打开该文件
    3. 第二层路径：若有日志数据，则根据日志中的步骤统计信息
       构建 ExecutionResult 对象，然后调用 generate_report() 生成新的 HTML 报告。
    4. 若 --open 启用，generate_report() 内部会自动在浏览器中打开报告。
    """
    try:
        from pathlib import Path

        from src.reporters.ai_reporter import AIInsightReporter

        reporter = AIInsightReporter()

        # 第一步：尝试从最新 Maestro 日志中解析测试数据
        log_data = reporter.parser.parse_logs()

        if not log_data["steps"]:
            # 回退策略：没有可解析的日志数据时，查找已有的报告文件
            click.echo("未找到最近的测试日志。请先执行 maestro test。")

            # 扫描 reports/ 目录下的历史报告（按 report_*.html 模式匹配）
            reports_dir = Path(__file__).parent.parent.parent / "reports"
            if reports_dir.exists():
                # 按修改时间倒序，获取最新的报告
                html_files = sorted(
                    reports_dir.glob("report_*.html"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if html_files:
                    latest = html_files[0]
                    click.echo(f"最新报告: {latest}")
                    # --open 标志：用系统默认浏览器打开报告文件
                    if open_browser:
                        import webbrowser
                        webbrowser.open(f"file://{latest}")
                    return
            # 既无日志也无历史报告，直接返回
            return

        # 第二步：有日志数据，根据日志步骤统计信息构建 ExecutionResult
        from src.models.schemas import ExecutionResult, TestStatus
        from datetime import datetime

        now = datetime.now()
        steps = log_data["steps"]
        # 统计失败和通过的步骤数
        failed = sum(1 for s in steps if s.get("status") == "failed")
        passed = sum(1 for s in steps if s.get("status") != "failed")

        # 构建标准化的执行结果对象
        result = ExecutionResult(
            test_name=test_name,
            platform="android",
            status=TestStatus.FAILED if failed > 0 else TestStatus.PASSED,
            start_time=now,
            end_time=now,
            total_steps=len(steps),
            passed_steps=passed,
            failed_steps=failed,
        )

        # 生成 AI 分析报告（HTML），若 --open 启用则在浏览器中打开
        test_report = reporter.generate_report(result, open_browser=open_browser)
        click.echo(f"报告已生成: {test_report.html_path}")

    except Exception as e:
        click.secho(f"生成报告失败: {e}", fg="red", err=True)
        sys.exit(1)


# ═══════════════════════════════════════════
# entry point
# ═══════════════════════════════════════════

if __name__ == "__main__":
    cli()
