# Maestro AI Platform

基于 Maestro 框架构建的移动端自动化测试平台，具备 **AI 自愈**、**自然语言生成用例**、**智能报告分析** 三大核心能力。

## 核心特性

| 特性 | 描述 |
|------|------|
| **AI 自愈引擎** | 元素定位器失效时自动构建 3 种备选策略，通过 `difflib` 模糊匹配(相似度>85%)修复定位器并动态更新 YAML |
| **自然语言→YAML** | 接收中文/英文描述，调用 DeepSeek/OpenAI/Claude 大模型生成符合 Maestro 语法的测试用例 |
| **批量生成** | 从 `.md` 文件批量读取用例描述，通过 front matter 配置设备和应用，一键生成所有 YAML |
| **三层失败恢复** | L1 原生重试 → L2 自愈重试 → L3 AI 自主重写 YAML，最大化用例通过率 |
| **AI 智能报告** | 解析 Maestro 执行日志，AI 分析失败根因并输出修复建议，生成美观的 Jinja2 HTML 报告 |
| **跨平台** | 支持 Android 真机/模拟器和 iOS 模拟器，通过 `devices.yaml` 统一管理 |
| **设备/应用别名** | `devices.yaml` 配置设备名→adb ID 映射，`apps.yaml` 配置别名→packageId 映射，用例中直接引用名称 |
| **MCP 集成** | 提供 MCP 协议服务器，Trae IDE 可直接调用 `generate` / `run` 核心功能 |

## 项目结构

```
maestro_ai_platform/
├── config/
│   ├── .env                     # 环境变量（API Key 配置）
│   ├── .env.example             # 环境变量模板
│   ├── devices.yaml             # 设备列表（名称→avd/udid 映射）
│   └── apps.yaml                # 应用列表（别名→packageId 映射）
├── src/
│   ├── ai/
│   │   └── generator.py         # 自然语言 → YAML 生成器
│   ├── cli/
│   │   └── main.py              # CLI 命令行入口
│   ├── core/
│   │   ├── apk_utils.py         # APK 工具函数
│   │   ├── config_loader.py     # 设备和应用配置加载器
│   │   ├── healing.py           # AI 自愈引擎
│   │   └── orchestrator.py      # 三层重试编排器
│   ├── models/
│   │   └── schemas.py           # Pydantic V2 数据模型
│   └── reporters/
│       └── ai_reporter.py       # AI 报告与洞察模块
├── test_case/
│   └── testcases.md             # 批量测试用例（markdown 格式）
├── flows/                       # 生成的 Maestro YAML 存放目录
├── reports/                     # HTML 测试报告输出
├── templates/                   # Jinja2 报告模板
├── tests/                       # 单元测试
├── _launcher.py                 # CLI 启动器（绕过模块缓存）
├── maestro-ai.bat               # Windows 批处理入口
├── mcp_server.py                # MCP 协议服务器
├── setup.py                     # 安装配置
└── requirements.txt             # 依赖列表
```

## 环境要求

- Python 3.11+
- [Maestro CLI](https://maestro.mobile.dev/) 已安装并可在终端使用 `maestro` 命令
- Android 模拟器/真机 或 iOS 模拟器（用于实际执行测试）

## 快速开始

### 1. 安装依赖

```bash
cd maestro_ai_platform
pip install -r requirements.txt
```

### 2. 配置全局命令（Windows）

将 `maestro-ai.bat` 所在目录添加到系统 PATH，或在 Python Scripts 目录创建快捷批处理：

```
# 在 C:\Users\<用户名>\AppData\Local\Programs\Python\Python311\Scripts\ 下
# 创建 maestro-ai.bat，内容为:
@echo off
call d:\pycharm\Maestro_App_Automation\maestro_ai_platform\maestro-ai.bat %*
```

之后在任意终端可直接运行 `maestro-ai`。

### 3. 配置 API Key

```bash
# 复制环境变量模板
cp config/.env.example config/.env

# 编辑 .env，填入 API Key（支持 DeepSeek / OpenAI / Anthropic）
# 推荐使用 DeepSeek，性价比高
DEEPSEEK_API_KEY=sk-your-key-here
AI_PROVIDER=deepseek
AI_MODEL=deepseek-chat
```

### 4. 配置设备

编辑 `config/devices.yaml`，配置设备名称到真实 adb ID 的映射：

```yaml
devices:
  android:
    - name: "oppo A32"           # 用例中引用的名称
      avd: "8073fcda"            # adb devices 输出的真实设备ID
      platform: android
      default: true
```

> **关键**：`name` 是你在 testcases.md 或 CLI 中使用的名称，`avd` 是 `adb devices` 显示的真实设备 ID。系统会自动将名称解析为 adb ID。

### 5. 配置应用

编辑 `config/apps.yaml`，注册被测应用：

```yaml
apps:
  - alias: "@flyu"               # testcases.md 中引用的别名
    app_id: "com.flyu.aos"       # Android packageId
    name: "FlyU"
```

### 6. 验证安装

```bash
maestro-ai --version
```

## CLI 命令详解

### generate — 自然语言生成用例

将中文/英文描述转换为 Maestro YAML 测试用例。

```bash
# 中文描述
maestro-ai generate "打开App，点击登录按钮，输入账号admin和密码123456，验证首页显示欢迎语"

# 英文描述 + 指定平台
maestro-ai generate "Open app, tap Login" --platform ios

# 指定 App ID + 生成后验证 YAML 语法
maestro-ai generate "点击设置按钮" --app-id @flyu --validate

# 从文件读取描述
maestro-ai generate --from-file test_case/testcases.md
```

生成的 YAML 文件保存在 `flows/generated_<timestamp>.yaml`。

### generate-batch — 批量生成用例

从 `.md` 文件批量读取多条用例描述，一次性生成所有 YAML。支持 YAML front matter 配置默认设备和应用。

**testcases.md 文件格式：**

```markdown
---
app_id: "@flyu"          # 应用别名（在 apps.yaml 中注册）
platform: android        # android / ios
device: "oppo A32"       # 设备名称（在 devices.yaml 中注册）
---

## 登录模块
打开App，点击邮箱登录按钮，输入账号cc123@qq.com和密码123456，验证登陆状态是否登陆成功

## 进入MV studio页面
登录后点击底部MV studioTab，验证页面是否存在四个tab

## 设置页面
登录后点击底部个人中心tab，验证页面是否存在帮助中心按钮
```

```bash
# 从 md 文件批量生成
maestro-ai generate-batch test_case/testcases.md

# 使用 CLI 参数覆盖 front matter 中的默认值
maestro-ai generate-batch test_case/testcases.md --app-id @taobao --platform ios
```

> front matter（`---` 包裹的部分）中的 `app_id` 和 `device` 使用别名，系统自动从 `apps.yaml` 和 `devices.yaml` 查找真实的 packageId 和 adb ID。

### run — 执行测试（支持自愈）

执行已有的 Maestro YAML 用例，支持自动重试和元素自愈。

```bash
# 基本执行
maestro-ai run flows/login.yaml

# 启用自愈 + 最大重试5次
maestro-ai run flows/login.yaml --heal --max-retries 5

# 指定设备执行（直接使用 adb ID）
maestro-ai run flows/login.yaml --device 8073fcda

# 使用设备名称（自动解析）
maestro-ai run flows/login.yaml --device "oppo A32"

# 禁用报告
maestro-ai run flows/login.yaml --no-report
```

三层恢复机制自动执行：
1. **L1** — Maestro 原生重试（最多 3 次）
2. **L2** — 元素自愈：匹配成功后更新 YAML 定位器，重跑
3. **L3** — AI 自主修复：将错误日志发给大模型，重写失败的 YAML 步骤

### record — 录制模式

启动 Maestro Studio 进行交互式录制。

```bash
# 启动录制并保存
maestro-ai record --save-to flows/recorded.yaml

# 指定设备
maestro-ai record --device 8073fcda --save-to flows/my_test.yaml
```

录制过程中在 Studio 界面操作 App，完成后按 `Ctrl+C` 结束，YAML 自动保存。

### report — 查看 AI 报告

生成并查看包含 AI 分析的最新测试报告。

```bash
# 生成报告并在浏览器中打开
maestro-ai report --open

# 指定测试名称
maestro-ai report --test-name "login_test"
```

报告包含：
- 测试摘要（通过/失败/自愈步骤数、耗时）
- AI 失败根因分析
- 视觉缺陷检测
- YAML 修复建议
- 执行截图
- 原始日志

## 编写测试用例

### 方式一：testcases.md 批量模式（推荐）

在 `test_case/testcases.md` 中用自然语言编写用例，通过 front matter 配置设备和应用：

```markdown
---
app_id: "@flyu"          # apps.yaml 中注册的应用别名
platform: android
device: "oppo A32"       # devices.yaml 中注册的设备名称
---

## 用例标题（## 开头，作为用例名称）
用自然语言描述测试步骤

## 另一个用例
登录后点击搜索，输入关键词，验证搜索结果不为空
```

然后运行：`maestro-ai generate-batch test_case/testcases.md`

### 方式二：CLI 直接生成

```bash
maestro-ai generate "打开App，点击登录，输入账号admin和密码123456" --app-id @flyu
```

### 方式三：单独文件描述

```bash
echo "打开App，验证首页显示正确" > case.txt
maestro-ai generate --from-file case.txt --app-id @flyu
```

## Python API 使用

### 生成测试用例

```python
from src.ai.generator import TestCaseGenerator

generator = TestCaseGenerator()
yaml_path = generator.generate(
    description="打开App，点击登录，输入账号admin和密码123456",
    app_id="com.flyu.aos",
    platform="android",
)
print(f"用例已生成: {yaml_path}")
```

### 执行测试（含自愈）

```python
from pathlib import Path
from src.core.orchestrator import MaestroOrchestrator

orch = MaestroOrchestrator()
result = orch.execute(
    yaml_path=Path("flows/login.yaml"),
    device_id="8073fcda",
    heal=True,
    max_retries=3,
)
print(f"状态: {result.status.value}, 耗时: {result.duration_ms:.0f}ms")
```

### 解析设备和应用别名

```python
from src.core.config_loader import resolve_device, resolve_app_id

# "oppo A32" → "8073fcda"（从 devices.yaml 查找）
device_id = resolve_device("oppo A32")

# "@flyu" → "com.flyu.aos"（从 apps.yaml 查找）
app_id, platform = resolve_app_id("@flyu")
```

## MCP 集成

平台提供 MCP 协议服务器，可在 Trae IDE 中直接调用。

### stdio 模式（默认）

```bash
python mcp_server.py
```

### HTTP 模式

```bash
python mcp_server.py --http
# 服务运行在 http://localhost:8100
```

### 可用 MCP 工具

| 工具 | 描述 |
|------|------|
| `maestro_generate` | 自然语言 → 生成 Maestro YAML |
| `maestro_run` | 执行 YAML 测试用例（含自愈） |
| `maestro_report` | 生成 AI 测试报告 |
| `maestro_status` | 查看设备列表、报告、用例状态 |

## 配置参考

### .env 环境变量

| 变量 | 必填 | 默认值 | 描述 |
|------|------|--------|------|
| `DEEPSEEK_API_KEY` | 是* | — | DeepSeek API Key |
| `OPENAI_API_KEY` | 是* | — | OpenAI API Key |
| `ANTHROPIC_API_KEY` | 是* | — | Anthropic API Key |
| `DEEPSEEK_BASE_URL` | 否 | `https://api.deepseek.com/v1` | DeepSeek API 地址 |
| `AI_PROVIDER` | 否 | `deepseek` | AI 提供商 |
| `AI_MODEL` | 否 | `deepseek-chat` | 模型名称 |
| `MAESTRO_CLI_PATH` | 否 | 自动发现 | Maestro CLI 路径 |

> *至少配置一个 API Key

### devices.yaml — 设备配置

| 字段 | 描述 |
|------|------|
| `name` | 设备别名（testcases.md / CLI 中用此名称引用） |
| `avd` | Android 设备真实 adb ID（`adb devices` 查看） |
| `udid` | iOS 设备 UDID |
| `platform` | `android` 或 `ios` |
| `default` | 是否为默认设备 |

### apps.yaml — 应用配置

| 字段 | 描述 |
|------|------|
| `alias` | 应用别名（以 `@` 开头，如 `@flyu`） |
| `app_id` | Android packageId 或 iOS bundleId |
| `name` | 应用显示名称（可选） |

## 运行测试

```bash
cd maestro_ai_platform
python -m pytest tests/ -v

# 运行特定模块测试
python -m pytest tests/test_healing.py -v
python -m pytest tests/test_generator.py -v
```

## 技术栈

- **语言**: Python 3.11+
- **测试框架**: Maestro CLI
- **AI SDK**: OpenAI SDK（兼容 DeepSeek / OpenAI / Anthropic）
- **数据模型**: Pydantic V2
- **CLI**: Click
- **报告**: Jinja2 + HTML/CSS
- **MCP**: JSON-RPC over stdio / HTTP

## License

MIT
