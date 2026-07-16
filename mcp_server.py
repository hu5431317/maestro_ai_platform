"""
阶段七：MCP 服务器 — Maestro AI Platform MCP 协议桥接

实现 maestro-mcp 协议接口，使 Trae IDE 可以通过 MCP 直接调用
本平台的 generate 和 run 核心功能。

参考: Maestro MCP 文档

============================================================================
MCP (Model Context Protocol) 协议说明
============================================================================
MCP 是基于 JSON-RPC 2.0 的客户端-服务器协议，Trae IDE 作为 MCP 客户端，
通过 stdio（标准输入/输出）管道与本服务器通信。每条消息是一行完整的 JSON。

支持的 JSON-RPC 方法:
  - initialize             : 握手初始化，交换协议版本和能力声明
  - tools/list             : 返回本服务器提供的所有工具定义列表
  - tools/call             : 调用指定工具，传入参数并获取执行结果

服务器需要将响应以单行 JSON 写入 stdout，IDE 从 stdout 读取结果。
============================================================================
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# 添加项目根到路径，确保可以导入 src 包下的模块
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("maestro-mcp")


# ═══════════════════════════════════════════
# MCP 协议处理
# ═══════════════════════════════════════════

class MCPServer:
    """
    Maestro MCP 协议服务器。

    支持通过 stdio JSON-RPC 与 Trae IDE 通信。
    实现核心方法: generate, run, status, list_devices

    架构说明:
    ──────────────────────────────────────────
    本类实现了 MCP 协议的服务端核心逻辑，采用"处理器注册 + 路由分发"模式:
      1. 在 __init__ 中将 JSON-RPC 方法名映射到对应的处理方法
      2. handle_request() 作为统一入口，根据 method 字段查找并调用处理器
      3. 每个 _handle_* 方法返回符合 JSON-RPC 2.0 规范的响应字典

    stdio 通信流程（run_stdio）:
      stdin 逐行读取 → 解析 JSON → handle_request → 写入 stdout
      每条消息是一行完整的 JSON，以换行符分隔
    """

    # ──────────────────────────────────────────
    # 工具定义（MCP tools/list 响应内容）
    # 每个工具定义遵循 MCP Tool 规范，包含 name / description / inputSchema
    # inputSchema 使用 JSON Schema 格式描述参数类型、枚举值、默认值和必填字段
    # ──────────────────────────────────────────

    TOOL_DEFINITIONS = [
        # ── 工具 1: maestro_generate ──
        # 从自然语言描述生成 Maestro 测试 YAML 文件
        # 必填参数: description（测试场景描述）
        # 可选参数: app_id（应用包名）、platform（android/ios）
        {
            "name": "maestro_generate",
            "description": "从自然语言描述生成 Maestro 测试 YAML",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "自然语言测试描述（中文/英文）",
                    },
                    "app_id": {
                        "type": "string",
                        "description": "App 的 packageId / bundleId",
                        "default": "com.example.app",
                    },
                    "platform": {
                        "type": "string",
                        "enum": ["android", "ios"],
                        "default": "android",
                    },
                },
                "required": ["description"],
            },
        },
        # ── 工具 2: maestro_run ──
        # 执行 Maestro YAML 测试用例，支持自动自愈重试
        # 必填参数: yaml_path（测试文件路径）
        # 可选参数: device_id、platform、heal（是否自愈）、max_retries（最大重试次数）
        {
            "name": "maestro_run",
            "description": "执行 Maestro YAML 测试用例（支持自愈）",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "yaml_path": {
                        "type": "string",
                        "description": "YAML 测试文件路径",
                    },
                    "device_id": {
                        "type": "string",
                        "description": "目标设备 ID",
                    },
                    "platform": {
                        "type": "string",
                        "enum": ["android", "ios"],
                        "default": "android",
                    },
                    "heal": {
                        "type": "boolean",
                        "description": "是否启用自愈",
                        "default": True,
                    },
                    "max_retries": {
                        "type": "integer",
                        "description": "最大重试次数",
                        "default": 3,
                    },
                },
                "required": ["yaml_path"],
            },
        },
        # ── 工具 3: maestro_report ──
        # 为指定测试生成 AI 智能分析报告
        # 可选参数: test_name（测试名称）
        {
            "name": "maestro_report",
            "description": "生成 AI 测试报告",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "test_name": {
                        "type": "string",
                        "description": "测试名称",
                    },
                },
            },
        },
        # ── 工具 4: maestro_status ──
        # 获取平台运行状态概览（设备列表、最近报告、最近测试流程等）
        # 无需参数，直接返回当前平台快照
        {
            "name": "maestro_status",
            "description": "获取平台状态（设备列表、最新报告等）",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
    ]

    def __init__(self):
        """
        初始化 MCP 服务器，注册 JSON-RPC 方法处理器映射表。

        _handlers 字典将 MCP 协议定义的方法名（如 "tools/list"）映射到
        对应的 _handle_* 实例方法，实现统一的路由分发。

        当前支持的方法:
          - tools/list    → 返回 TOOL_DEFINITIONS 中定义的所有工具
          - tools/call    → 调用具体工具（通过 _dispatch_tool 进一步路由）
          - initialize    → 握手初始化，返回协议版本和服务器信息
        """
        self._handlers: dict[str, Any] = {
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
            "initialize": self._handle_initialize,
        }

    # ── MCP 协议入口 ──

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """
        处理 MCP JSON-RPC 请求的统一入口。

        工作流程:
          1. 从请求中提取 method（JSON-RPC 方法名）和 id（请求标识）
          2. 根据 method 在 _handlers 中查找对应的处理器
          3. 如果找不到匹配的处理器，返回 JSON-RPC 错误码 -32601（方法未找到）
          4. 如果处理器抛出异常，捕获并返回 JSON-RPC 错误码 -32000（服务器错误）
        """
        method = request.get("method", "")
        req_id = request.get("id")

        # 路由查找: 根据 method 名称分发到对应的处理方法
        handler = self._handlers.get(method)
        if handler is None:
            return self._error(req_id, -32601, f"Method not found: {method}")

        try:
            return handler(request.get("params", {}), req_id)
        except Exception as e:
            logger.exception(f"处理请求失败: {method}")
            return self._error(req_id, -32000, str(e))

    # ── 工具列表 ──

    def _handle_tools_list(self, params: dict, req_id: Any) -> dict:
        """
        处理 tools/list 请求。

        当 Trae IDE 首次连接或需要刷新可用工具列表时，会发送此请求。
        返回包含所有工具定义的 JSON-RPC 响应，每个工具定义遵循 MCP Tool Schema，
        包含 name、description 和 inputSchema（JSON Schema 格式的参数定义）。
        """
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": self.TOOL_DEFINITIONS},
        }

    # ── 初始化 ──

    def _handle_initialize(self, params: dict, req_id: Any) -> dict:
        """
        处理 initialize 请求（MCP 协议握手）。

        Trae IDE 在建立连接后首先发送此请求，用于协商协议版本。
        服务器需要返回:
          - protocolVersion: MCP 协议版本号
          - serverInfo:       服务器名称和版本标识
          - capabilities:     服务器能力声明（本服务器声明支持 tools 能力）

        capabilities.tools 表明本服务器可以提供工具列表和工具调用服务，
        IDE 收到后会继续发送 tools/list 请求获取具体工具。
        """
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {
                    "name": "maestro-ai-platform",
                    "version": "1.0.0",
                },
                "capabilities": {
                    "tools": {},
                },
            },
        }

    # ── 工具调用 ──

    def _handle_tools_call(self, params: dict, req_id: Any) -> dict:
        """
        处理 tools/call 请求。

        当 IDE 需要调用某个工具时发送此请求。params 中包含:
          - name:      工具名称（如 "maestro_generate"）
          - arguments: 调用参数（字典格式，键值对需符合对应 inputSchema）

        工作流程:
          1. 从 params 提取工具名和参数
          2. 通过 _dispatch_tool 路由到具体的 _tool_* 方法
          3. 将工具执行结果包装为 MCP 标准响应格式
             - content 数组中每个元素包含 type（"text"）和 text（JSON 字符串）
        """
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        # 分发到具体工具实现
        result = self._dispatch_tool(tool_name, arguments)

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False, indent=2),
                    }
                ]
            },
        }

    def _dispatch_tool(self, name: str, args: dict) -> dict[str, Any]:
        """
        工具路由分发器 —— 将工具名映射到对应的 _tool_* 方法。

        这是 tools/call 的第二层路由。handle_request 负责 JSON-RPC 方法级别的路由
        （tools/list / tools/call / initialize），而 _dispatch_tool 负责具体工具级别的路由
        （maestro_generate / maestro_run / maestro_report / maestro_status）。

        如果工具名不在已知列表中，返回包含 error 字段的字典（非 JSON-RPC 错误，
        而是工具级别的业务错误，会被包装在 tools/call 的 content.text 中返回）。
        """
        if name == "maestro_generate":
            return self._tool_generate(args)
        elif name == "maestro_run":
            return self._tool_run(args)
        elif name == "maestro_report":
            return self._tool_report(args)
        elif name == "maestro_status":
            return self._tool_status(args)
        else:
            return {"error": f"Unknown tool: {name}"}

    # ── 工具实现 ──

    def _tool_generate(self, args: dict) -> dict[str, Any]:
        """
        实现 maestro_generate 工具: 从自然语言描述生成 Maestro 测试 YAML。

        流程:
          1. 校验 description 参数非空
          2. 懒加载 TestCaseGenerator（仅在首次调用时导入，减少启动开销）
          3. 调用 generator.generate()，传入描述、app_id、platform
          4. 返回生成的 YAML 文件路径和内容
        """
        description = args.get("description", "")
        if not description:
            return {"error": "description is required"}

        try:
            from src.ai.generator import TestCaseGenerator

            generator = TestCaseGenerator()
            yaml_path = generator.generate(
                description=description,
                app_id=args.get("app_id", "com.example.app"),
                platform=args.get("platform", "android"),
            )

            return {
                "success": True,
                "yaml_path": str(yaml_path),
                "yaml_content": yaml_path.read_text(encoding="utf-8"),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _tool_run(self, args: dict) -> dict[str, Any]:
        """
        实现 maestro_run 工具: 执行 Maestro YAML 测试用例（支持自愈重试）。

        流程:
          1. 校验 yaml_path 参数非空
          2. 懒加载 MaestroOrchestrator
          3. 调用 orch.execute()，传入 YAML 路径、设备ID、平台、自愈开关、最大重试次数
          4. 返回执行结果状态、耗时、自愈步骤数、总步骤数、失败步骤数
        """
        yaml_path = args.get("yaml_path", "")
        if not yaml_path:
            return {"error": "yaml_path is required"}

        try:
            from src.core.orchestrator import MaestroOrchestrator

            orch = MaestroOrchestrator()
            result = orch.execute(
                yaml_path=Path(yaml_path),
                device_id=args.get("device_id"),
                platform=args.get("platform", "android"),
                heal=args.get("heal", True),
                max_retries=args.get("max_retries", 3),
            )

            return {
                "success": result.status.value != "failed",
                "status": result.status.value,
                "duration_ms": result.duration_ms,
                "healed_steps": result.healed_steps,
                "total_steps": result.total_steps,
                "failed_steps": result.failed_steps,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _tool_report(self, args: dict) -> dict[str, Any]:
        """
        实现 maestro_report 工具: 生成 AI 智能测试报告。

        流程:
          1. 懒加载 AIInsightReporter
          2. 根据传入参数构造 ExecutionResult 对象
          3. 调用 reporter.generate_report() 生成 HTML 报告和 AI 分析洞察
          4. 返回报告路径和 AI 洞察数量
        """
        try:
            from src.reporters.ai_reporter import AIInsightReporter

            reporter = AIInsightReporter()
            test_report = reporter.generate_report(
                execution_result=ExecutionResult(
                    test_name=args.get("test_name", "mcp_report"),
                    platform="android",
                    status=TestStatus.PASSED,
                    start_time=datetime.now(),
                )
            )

            return {
                "success": True,
                "html_path": str(test_report.html_path),
                "insights_count": len(test_report.ai_insights),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _tool_status(self, args: dict) -> dict[str, Any]:
        """
        获取平台状态（maestro_status 工具实现）。

        收集并返回当前平台运行状态快照，包括:
          - devices:        设备配置信息（从 config/devices.yaml 读取）
          - recent_reports: 最近5个 HTML 报告文件（按修改时间倒序）
          - recent_flows:   最近5个 YAML 测试流程文件（按修改时间倒序）
          - platform_version: 平台版本号

        所有数据均为只读快照，不会产生副作用。
        """
        try:
            import yaml

            # 读取设备配置
            config_path = Path(__file__).parent / "config" / "devices.yaml"
            devices_info = {}
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    devices_info = yaml.safe_load(f)

            # 检查报告
            reports_dir = Path(__file__).parent / "reports"
            recent_reports = []
            if reports_dir.exists():
                html_files = sorted(
                    reports_dir.glob("report_*.html"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                recent_reports = [str(f.name) for f in html_files[:5]]

            # 检查 flows
            flows_dir = Path(__file__).parent / "flows"
            recent_flows = []
            if flows_dir.exists():
                yaml_files = sorted(
                    flows_dir.glob("*.yaml"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                recent_flows = [str(f.name) for f in yaml_files[:5]]

            return {
                "success": True,
                "devices": devices_info.get("devices", {}),
                "recent_reports": recent_reports,
                "recent_flows": recent_flows,
                "platform_version": "1.0.0",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── 辅助方法 ──

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict:
        """
        构造 JSON-RPC 2.0 错误响应。

        JSON-RPC 标准错误码:
          - -32601: 方法未找到（Method not found）
          - -32000: 服务器内部错误（通用）

        Args:
            req_id:  原始请求的 id，用于关联请求和响应
            code:    JSON-RPC 错误码（负整数）
            message: 人类可读的错误描述
        """
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }

    def run_stdio(self):
        """
        以 stdio 模式运行 MCP 服务器（用于 IDE 集成）。

        这是 MCP 协议的标准通信方式:
          - Trae IDE 启动本脚本作为子进程
          - 通过 stdin 发送 JSON-RPC 请求（每行一个完整的 JSON）
          - 服务器处理后将 JSON-RPC 响应写入 stdout（每行一个完整的 JSON）
          - stderr 用于日志输出（IDE 不会读取 stderr，因此日志不影响协议通信）

        循环逻辑:
          1. 从 stdin 逐行读取（阻塞等待 IDE 发送请求）
          2. 跳过空行
          3. 解析 JSON，调用 handle_request 分发处理
          4. 将响应序列化为 JSON 写入 stdout 并立即 flush
          5. 捕获 JSON 解析错误，记录日志后继续（不中断循环）
          6. 进程退出时循环自然结束
        """
        import sys

        logger.info("Maestro MCP Server 启动 (stdio 模式)")

        # 逐行读取 stdin，每行是一个独立的 JSON-RPC 请求
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
                response = self.handle_request(request)
                # 响应写入 stdout，IDE 从此读取
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except json.JSONDecodeError as e:
                logger.error(f"JSON 解析错误: {e}")
            except Exception as e:
                logger.exception("处理请求时发生未知错误")


# ═══════════════════════════════════════════
# HTTP 模式入口（兼容某些 MCP 实现）
# ═══════════════════════════════════════════

def create_mcp_app():
    """
    创建 FastAPI 应用用于 HTTP 模式的 MCP 服务。

    某些 MCP 客户端实现支持通过 HTTP 而非 stdio 进行通信。
    此函数创建 FastAPI 应用，将 JSON-RPC 请求包装为 RESTful HTTP 接口。

    提供的端点:
      - POST /mcp    : MCP 主端点，接收 MCPRequest（method + params + request_id）
                       内部调用 MCPServer.handle_request() 处理，返回 MCPResponse
      - GET  /health : 健康检查端点，返回服务状态

    返回值:
        FastAPI 应用实例；如果 FastAPI 未安装则返回 None
    """
    try:
        from fastapi import FastAPI
        from src.models.schemas import MCPRequest, MCPResponse

        app = FastAPI(title="Maestro AI MCP Server", version="1.0.0")
        server = MCPServer()

        @app.post("/mcp")
        async def mcp_endpoint(request: MCPRequest) -> MCPResponse:
            """
            MCP HTTP 端点: 将 HTTP 请求转换为内部 JSON-RPC 调用。

            将 MCPRequest 的字段映射为 JSON-RPC 请求字典，
            调用 MCPServer.handle_request() 后根据结果构造 MCPResponse。
            """
            result = server.handle_request({
                "method": request.method,
                "params": request.params,
                "id": request.request_id,
            })

            if "error" in result:
                return MCPResponse(
                    request_id=result.get("id"),
                    success=False,
                    error=result["error"].get("message", "Unknown error"),
                )

            return MCPResponse(
                request_id=result.get("id"),
                success=True,
                data=result.get("result"),
            )

        @app.get("/health")
        async def health():
            """健康检查端点，用于监控和负载均衡探测。"""
            return {"status": "ok", "service": "maestro-mcp"}

        return app
    except ImportError:
        logger.warning("FastAPI 未安装，无法创建 HTTP MCP 服务")
        return None


# ═══════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════

if __name__ == "__main__":
    """
    主入口: 支持两种运行模式。

    1. stdio 模式（默认）:
       - 无需额外参数，IDE 以子进程方式启动
       - 通过 stdin/stdout 进行 JSON-RPC 通信
       - 示例: python mcp_server.py

    2. HTTP 模式（通过 --http 参数启用）:
       - 启动 uvicorn HTTP 服务器
       - 端口通过 MCP_PORT 环境变量配置，默认 8100
       - 适用于某些支持 HTTP 的 MCP 客户端实现
       - 需要安装 FastAPI + uvicorn 依赖
       - 示例: python mcp_server.py --http
    """
    import sys

    if "--http" in sys.argv:
        # HTTP 模式
        try:
            import uvicorn

            port = int(os.getenv("MCP_PORT", "8100"))
            app = create_mcp_app()
            if app:
                uvicorn.run(app, host="0.0.0.0", port=port)
        except ImportError:
            logger.error("HTTP 模式需要安装 FastAPI + uvicorn")
            sys.exit(1)
    else:
        # stdio 模式（默认）
        # 在 stdio 模式下需要预先导入执行结果和状态模型，
        # 因为这些类在 _tool_report 中会使用
        from datetime import datetime  # noqa: F811
        from src.models.schemas import ExecutionResult, TestStatus  # noqa: F811

        server = MCPServer()
        server.run_stdio()
