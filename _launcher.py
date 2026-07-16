#!/usr/bin/env python3
"""maestro-ai CLI launcher"""
import sys, os
sys.path.insert(0, ".")

# 清除模块缓存
stale = [k for k in list(sys.modules) if k.startswith("src")]
for key in stale:
    del sys.modules[key]

# 直接设置 maestro CLI 路径，避免自动发现的不确定性
os.environ["MAESTRO_CLI_PATH"] = os.path.expandvars(
    r"%APPDATA%\npm\maestro.cmd"
)

from click.testing import CliRunner
from src.cli.main import cli

if __name__ == "__main__":
    runner = CliRunner()
    result = runner.invoke(cli, sys.argv[1:], standalone_mode=False)
    if result.exit_code != 0:
        sys.exit(result.exit_code)
