"""
Maestro AI Platform - 安装配置

============================================================================
本文件是 Python 包的标准安装配置文件（setup.py），使用 setuptools 构建。
通过 pip install . 或 pip install -e . 安装本平台及其依赖。

关键配置项说明:
  - name / version          : 包名和版本号，用于 PyPI 发布和版本管理
  - python_requires         : Python 最低版本要求，低于此版本将拒绝安装
  - packages                : 自动发现需要包含到包中的 Python 模块
  - install_requires        : 核心依赖，安装包时自动安装
  - extras_require          : 可选依赖分组，按需安装（如 pip install .[web]）
  - entry_points            : 注册命令行入口，安装后可直接在终端调用
  - classifiers             : PyPI 分类标签，帮助用户搜索和筛选
============================================================================
"""

from setuptools import setup, find_packages

setup(
    name="maestro-ai-platform",
    version="1.0.0",
    description="具备自愈、AI生成、智能分析的移动端自动化测试平台",
    author="Maestro AI Team",

    # ──────────────────────────────────────────
    # Python 版本要求
    # ──────────────────────────────────────────
    # 要求 Python >= 3.11，主要原因是:
    #   1. 使用了 PEP 604 联合类型语法（X | Y），该语法在 3.10+ 可用
    #   2. 使用了 PEP 673 Self 类型（from __future__ import annotations 配合）
    #   3. 更好的异常处理和性能特性
    # 如果不满足版本要求，pip 会报错并拒绝安装
    python_requires=">=3.11",

    # ──────────────────────────────────────────
    # 包发现与目录配置
    # ──────────────────────────────────────────
    # find_packages(where="."):
    #   从当前目录（setup.py 所在目录）开始自动发现所有包含 __init__.py 的目录作为包。
    #   区别于手动列举 packages=["src", "src.core", ...] 的方式，
    #   find_packages 会自动递归扫描，无需手动维护包列表。
    #
    # package_dir={"": "."}:
    #   将当前目录作为包的根目录。这意味着 src/ 目录下的 src/ai/、src/core/ 等
    #   都会被识别为 Python 包，导入时使用 from src.ai.generator import ... 的形式。
    packages=find_packages(where="."),
    package_dir={"": "."},

    # include_package_data=True:
    #   安装时包含 MANIFEST.in 或 package_data 中定义的非 Python 文件
    #   （如 YAML 模板、配置文件、静态资源等）。
    #   这对于包含 flows/*.yaml 模板、config/*.yaml 配置等非代码文件非常重要。
    include_package_data=True,

    # ──────────────────────────────────────────
    # 核心依赖（install_requires）
    # ──────────────────────────────────────────
    # 这些是平台运行所必需的基础依赖，pip install maestro-ai-platform 时自动安装。
    # 每个依赖都设置了最低版本约束，确保兼容性。
    install_requires=[
        "openai>=1.30.0",         # OpenAI API 客户端，用于调用 GPT 模型生成测试用例和报告分析
        "pydantic>=2.5.0",        # 数据验证和序列化，用于定义配置模型和 API schema
        "pyyaml>=6.0",            # YAML 解析和生成，Maestro 测试用例使用 YAML 格式
        "jinja2>=3.1.0",          # HTML 报告模板引擎，渲染 AI 测试报告
        "click>=8.1.0",           # CLI 命令行框架，用于 maestro-ai 命令行工具
        "httpx>=0.27.0",          # 异步 HTTP 客户端，用于 API 调用和设备通信
        "python-dotenv>=1.0.0",   # 环境变量加载，从 .env 文件读取 API Key 等配置
    ],

    # ──────────────────────────────────────────
    # 可选依赖分组（extras_require）
    # ──────────────────────────────────────────
    # 为了减小基础安装体积，将非核心功能拆分为可选分组。
    # 用户按需安装:
    #   pip install maestro-ai-platform[web]       → 安装 Web UI 依赖
    #   pip install maestro-ai-platform[anthropic] → 安装 Anthropic Claude 支持
    #   pip install maestro-ai-platform[mcp]       → 安装 MCP 协议支持
    #   pip install maestro-ai-platform[web,anthropic,mcp] → 同时安装所有可选依赖
    extras_require={
        # web 分组: 提供 Web 管理界面和 HTTP API 服务
        # fastapi: 高性能 Web 框架，用于 RESTful API 和 MCP HTTP 模式
        # uvicorn: ASGI 服务器，用于运行 FastAPI 应用
        "web": ["fastapi>=0.110.0", "uvicorn>=0.27.0"],

        # anthropic 分组: 提供 Anthropic Claude 大模型支持
        # 允许用户选择使用 Claude 替代 OpenAI 进行测试生成和报告分析
        # 安装后可在配置中切换 AI provider 为 anthropic
        "anthropic": ["anthropic>=0.25.0"],

        # mcp 分组: 提供 MCP (Model Context Protocol) 协议支持
        # 用于与 Trae IDE 等支持 MCP 的编辑器进行集成
        # 注意: 核心 MCP 服务器实现在 mcp_server.py 中，不需要额外依赖
        # 此分组主要用于安装官方的 MCP SDK（可选增强功能）
        "mcp": ["mcp>=1.0.0"],
    },

    # ──────────────────────────────────────────
    # 命令行入口点（entry_points.console_scripts）
    # ──────────────────────────────────────────
    # 定义安装后可在系统终端直接调用的命令。
    #
    # 格式: "命令名=模块路径:函数名"
    #   - 命令名 "maestro-ai": 用户终端输入 "maestro-ai" 即可调用
    #   - 模块路径 "src.cli.main": Python 导入路径，指向 src/cli/main.py 模块
    #   - 函数名 "cli": main.py 中定义的 Click 命令行组入口函数
    #
    # 安装后效果:
    #   $ maestro-ai --help          → 显示 CLI 帮助
    #   $ maestro-ai generate "..."  → 调用生成功能
    #   $ maestro-ai run test.yaml   → 调用执行功能
    #
    # 这是通过 setuptools 在 Python 环境的 Scripts/ 目录下生成可执行包装脚本实现的，
    # 在 Windows 上生成 maestro-ai.exe，在 Linux/macOS 上生成 maestro-ai 脚本。
    entry_points={
        "console_scripts": [
            "maestro-ai=src.cli.main:cli",
        ],
    },

    # ──────────────────────────────────────────
    # PyPI 分类标签（classifiers）
    # ──────────────────────────────────────────
    # 帮助用户在 PyPI 上按语言版本、用途等维度搜索和筛选包。
    # 标注了支持的 Python 3.11 和 3.12 版本。
    classifiers=[
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
