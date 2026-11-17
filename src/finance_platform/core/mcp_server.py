"""Official MCP SDK stdio server over curated local product operations."""
import argparse
import asyncio
from contextlib import asynccontextmanager
from getpass import getpass
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit
from .errors import PlatformError
from .mcp_gateway import ProductGateway, checked_origin
from .mcp_proof import CLIENT_ID
from .mcp_operations import operations

DIAGNOSTIC_STATES = {
    "MCP_AUTHORIZATION_REQUIRED": "authorization_required",
    "MCP_PAIRING_REJECTED": "authorization_required",
    "CREDENTIAL_STORE_UNAVAILABLE": "authorization_required",
    "MCP_MANAGED_LISTENER_REQUIRED": "listener_mismatch",
    "MCP_LOCAL_ORIGIN_REQUIRED": "listener_mismatch",
    "MCP_WINDOWS_USER_MISMATCH": "listener_mismatch",
    "MCP_PRODUCT_UNAVAILABLE": "service_unavailable",
    "DATABASE_UNAVAILABLE": "service_unavailable",
}


def create_server(gateway):
    import os
    os.environ['OTEL_SDK_DISABLED']='true'
    from mcp.server import MCPServer
    import anyio
    from mcp_types import ListToolsResult, NotificationParams, ToolAnnotations
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS
    from mcp.server.mcpserver.exceptions import ToolError,ResourceError
    account_mode = getattr(gateway, "account_mode", False)
    public_tools = {"workbench_guide", "connection_diagnostics"}
    last_call = None
    watcher_group = None
    watcher_started = False
    if account_mode:
        from .mcp_access import account_operations, permitted_operations

    async def connection_status(*, observe=False):
        try:
            scope = await (gateway.account_scope(observe_tools=True) if observe else gateway.account_scope())
            return {"state": "ready", "reason_code": None, "scope": scope,
                    "tools": sorted(permitted_operations(scope)), "last_call": last_call}
        except PlatformError as error:
            return {
                "state": DIAGNOSTIC_STATES.get(error.code, "protocol_error"),
                "reason_code": error.code if error.code in DIAGNOSTIC_STATES else "MCP_PRODUCT_RESPONSE_INVALID",
                "scope": None, "tools": [], "last_call": last_call,
            }

    async def watch_permissions(session):
        async def probe():
            try:
                return await gateway.discovery_state()
            except PlatformError:
                return None

        revision = gateway.authorization_revision()
        previous = await probe()
        next_probe = anyio.current_time() + 30
        while True:
            await anyio.sleep(2)
            current = gateway.authorization_revision()
            if current == revision and anyio.current_time() < next_probe:
                continue
            revision = current
            next_probe = anyio.current_time() + 30
            available = await probe()
            if available != previous:
                try:
                    await session.send_tool_list_changed()
                except (OSError, RuntimeError, anyio.BrokenResourceError, anyio.ClosedResourceError):
                    return
                previous = available

    async def start_watcher(ctx, params=None):
        nonlocal watcher_started
        if (account_mode and watcher_group is not None and not watcher_started
                and ctx.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS):
            watcher_started = True
            watcher_group.start_soon(watch_permissions, ctx.session)

    class PrivateMCPServer(MCPServer):
        async def _visible_tools(self, *, observe=False):
            tools = await super().list_tools()
            if not account_mode:
                return tools
            status = await connection_status(observe=observe)
            allowed = public_tools.union(status["tools"])
            return [tool for tool in tools if tool.name in allowed]

        async def list_tools(self):
            return await self._visible_tools()

        async def _handle_list_tools(self, ctx, params):
            await start_watcher(ctx)
            return ListToolsResult(tools=await self._visible_tools(observe=account_mode))

        async def run_stdio_async(self):
            if not account_mode:
                return await super().run_stdio_async()
            from mcp.server.lowlevel import NotificationOptions
            from mcp.server.stdio import stdio_server

            api = self._lowlevel_server
            api.add_notification_handler("notifications/initialized", NotificationParams, start_watcher)
            async with stdio_server() as (reader, writer):
                setup = api.create_initialization_options(NotificationOptions(tools_changed=True))
                await api.run(reader, writer, setup)

        async def call_tool(self,name,arguments,context=None):
            # Public SDK execution boundary: argument validation happens inside
            # super().call_tool, before the business function. Never serialize
            # its Pydantic errors, input repr, cause or traceback to the peer.
            try:return await super().call_tool(name,arguments,context)
            except Exception:
                raise ToolError('MCP_TOOL_INPUT_OR_EXECUTION_ERROR：操作字段无效或执行失败，请按工具说明检查。')from None
        async def read_resource(self,uri,context=None):
            try:return await super().read_resource(uri,context)
            except Exception:
                raise ResourceError('MCP_RESOURCE_UNAVAILABLE：资源不可用，请检查应用状态。')from None
    @asynccontextmanager
    async def lifespan(server):
        nonlocal watcher_group, watcher_started
        try:
            if not account_mode:
                yield {}
                return
            async with anyio.create_task_group() as group:
                watcher_group = group
                try:
                    yield {}
                finally:
                    group.cancel_scope.cancel()
        finally:
            watcher_group = None
            watcher_started = False
            await gateway.close()
    server=PrivateMCPServer('FinanceAnalyze',instructions='先使用 workbench_guide 了解数据发现、策略能力、真实任务和保存报告流程。所有模型、数据及任务操作均走正常权限；没有任意命令或文件工具。',lifespan=lifespan,log_level='CRITICAL',subscriptions=False)
    from .mcp_workbench import guide

    async def current_guide():
        if not account_mode:
            return guide()
        status = await connection_status()
        return {"title": "FinanceAnalyze 本机工作台",
                "connection": status,
                "steps": ["在 FinanceAnalyze 网页打开「连接外部 AI」，选择 Codex 或 DeepSeek，"
                          "点击对应的连接按钮。系统提示时，完成身份验证。",
                          "使用工具发现查询当前授权能力，以已经保存的精确数据和策略引用创建任务。",
                          "复用已保存行情时只提交原引用和幂等标识。为回测添加 ETF 对照时，先下载并保存为专用价格对照数据集。",
                          "查看任务状态与保存结果。大报告可从分页导出清单读取。"]}

    @server.tool(name='workbench_guide',description='用中文了解工作台支持的发现数据、真实回测/研究、轮询取消和读取保存报告流程；不执行任务。',annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False))
    async def workbench_guide()->dict[str,Any]:return await current_guide()
    if account_mode:
        @server.tool(name='connection_diagnostics', description='查询当前授权范围、可用工具及固定连接失败原因。',
                     annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
        async def connection_diagnostics()->dict[str,Any]:
            return await connection_status()
    @server.resource('finance://workbench-guide')
    async def workflow_resource()->str:return json.dumps(await current_guide(),ensure_ascii=False)
    catalogue=account_operations() if account_mode else operations()
    for operation in catalogue:
        def build(op):
            async def execute(params):
                nonlocal last_call
                try:
                    if account_mode:
                        scope = await gateway.account_scope()
                        if op.name not in permitted_operations(scope):
                            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
                    value = await gateway.call(op, params)
                except PlatformError as error:
                    last_call = {"tool": op.name, "state": "failed", "reason_code": error.code}
                    return {'ok':False,'error':{'code':error.code,'message':error.message}}
                last_call = {"tool": op.name, "state": "succeeded", "reason_code": None}
                return {'ok':True,'data':value}
            execute.__name__=op.name
            execute.__annotations__={'params':op.input_model,'return':dict[str,Any]}
            return execute
        server.add_tool(build(operation),name=operation.name,description=operation.description,
            annotations=ToolAnnotations(readOnlyHint=operation.method=='GET',destructiveHint=operation.destructive,
                                        idempotentHint=operation.method=='GET' or operation.idempotent,openWorldHint=operation.external))
    @server.resource('finance://capabilities')
    async def capabilities()->str:
        listed = {tool.name for tool in await server.list_tools()} if account_mode else {op.name for op in catalogue}
        return json.dumps({'tools':[{'name':op.name,'description':op.description,'read_only':op.method=='GET'} for op in catalogue if op.name in listed],
                           'transport':'stdio','product_state':'通过 application_capabilities 查询实际状态。'},ensure_ascii=False)
    if not account_mode:
        @server.resource('finance://model-capabilities')
        async def model_capabilities()->str:
            op=next(op for op in catalogue if op.name=='model_capabilities')
            return json.dumps(await gateway.call(op,op.input_model()),ensure_ascii=False)
    @server.resource('finance://results')
    async def results()->str:
        op=next(op for op in catalogue if op.name=='results_list')
        return json.dumps(await gateway.call(op,op.input_model()),ensure_ascii=False)
    return server

def main(argv=None, *, root=None, binding=None):
    parser=argparse.ArgumentParser(description='FinanceAnalyze 本地 MCP（标准输入输出）')
    parser.add_argument('--api-url',required=True)
    parser.add_argument("--client", default="codex")
    parser.add_argument("--access-mode", choices=("accounts", "local-personal"), default="accounts")
    parser.add_argument("--pair", action="store_true")
    args=parser.parse_args(argv)
    # Process-local setting only, before importing the SDK. Never export tool
    # inputs, credentials or product results to an inherited telemetry backend.
    import os
    os.environ['OTEL_SDK_DISABLED']='true'
    try:
        if not CLIENT_ID.fullmatch(args.client):
            raise PlatformError("INVALID_INPUT", 422)
        if args.access_mode == "local-personal":
            if args.pair:
                raise PlatformError("INVALID_INPUT", 422)
            create_server(ProductGateway(args.api_url)).run(transport="stdio")
            return 0
        from finance_platform.operations.mcp_keystore import McpKeyStore
        from finance_platform.operations.mcp_peer import ManagedMcpTransport, WindowsPeer
        from finance_platform.operations.source_binding import inspect_source
        origin = checked_origin(args.api_url)
        endpoint = urlsplit(origin)
        if endpoint.hostname != "127.0.0.1" or endpoint.port is None or not 1024 <= endpoint.port <= 65535:
            raise PlatformError("MCP_LOCAL_ORIGIN_REQUIRED", 422)
        root = Path(root or Path(__file__).parents[3]).resolve()
        expected = binding or inspect_source(root)
        peer = WindowsPeer(root, expected, endpoint.port)
        gateway = ProductGateway(origin, transport=ManagedMcpTransport(peer, 30),
                                 key_store=McpKeyStore(root, endpoint.port, args.client), client_id=args.client,
                                 installation=expected["root_hash"])
        if not args.pair:
            create_server(gateway).run(transport="stdio")
            return 0
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            raise PlatformError("MCP_PAIRING_REJECTED", 401)

        async def pair():
            try:
                await gateway.pair(getpass("FinanceAnalyze 配对码（输入不回显）："))
            finally:
                await gateway.close()

        asyncio.run(pair())
        print("FinanceAnalyze 配对完成。", file=sys.stderr)
        return 0
    except PlatformError as error:
        print(str(error.code), file=sys.stderr)
        return 2
    except (OSError, EOFError, KeyboardInterrupt):
        print("MCP_PRODUCT_UNAVAILABLE", file=sys.stderr)
        return 2

if __name__=='__main__':raise SystemExit(main())
