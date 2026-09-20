"""
MCP Client 桥接层

MCP 官方 SDK 是 async 的，而 Gradio 的回调是同步的。
这里在后台线程里跑一个专属 asyncio 事件循环，维持与 MCP Server 的
**持久连接**（而不是每次请求都重启子进程），对外暴露同步方法。

对外主要提供两个方法：
    list_tools()                  -> 拿到 Server 声明的工具，转成 OpenAI/Qwen 风格
    call_tool(name, arguments)    -> 通过 MCP 协议真正执行工具

理解要点：
    模型（Qwen）只负责「决定调哪个工具、传什么参数」；
    真正「执行」这一步，是通过 MCP Client -> JSON-RPC -> MCP Server 完成的。
    这两件事分属不同层次 —— 前者是 Function Call，后者是 MCP。
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """按顺序尝试多个属性名，返回第一个非 None 的值。

    MCP Python SDK 在 2.0 把数据模型的字段从 camelCase 改成了 snake_case：
        1.x:  inputSchema  / structuredContent / isError
        2.x:  input_schema / structured_content / is_error
    这个辅助函数让同一份代码在两个大版本上都能跑。
    """
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


class MCPBridge:
    """把异步的 MCP Client 包装成同步可调用的样子，并复用同一条连接。"""

    def __init__(self, server_params: StdioServerParameters) -> None:
        self._server_params = server_params
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="mcp-event-loop"
        )
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._started = False

    # ------------------------------------------------------------------ #
    # 内部：事件循环线程
    # ------------------------------------------------------------------ #

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro) -> Any:
        """把协程丢进后台事件循环执行，并同步等待结果。"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """启动事件循环线程，并拉起 MCP Server 子进程完成握手。"""
        if self._started:
            return
        self._thread.start()
        self._submit(self._connect())
        self._started = True

    async def _connect(self) -> None:
        """建立 stdio 连接、创建会话、完成 MCP initialize 握手。"""
        self._stack = AsyncExitStack()
        read_stream, write_stream = await self._stack.enter_async_context(
            stdio_client(self._server_params)
        )
        self._session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()

    def stop(self) -> None:
        """关闭会话、终止子进程、停止事件循环。"""
        if self._stack is not None:
            try:
                self._submit(self._stack.aclose())
            except Exception:
                pass
            self._stack = None
        self._loop.call_soon_threadsafe(self._loop.stop)

    # ------------------------------------------------------------------ #
    # 对外 API：工具发现
    # ------------------------------------------------------------------ #

    def list_tools(self) -> list[dict[str, Any]]:
        """向 Server 请求工具清单，并转成 OpenAI / Qwen 的 tools 格式。

        这一步体现了 MCP 的核心优势：工具是「动态发现」的。
        Client 事先并不知道有哪些工具，是运行时问 Server 得到的。
        """
        if self._session is None:
            raise RuntimeError("MCPBridge 尚未 start()，无法列出工具")

        result = self._submit(self._session.list_tools())

        tools: list[dict[str, Any]] = []
        for tool in result.tools:
            schema = _attr(tool, "input_schema", "inputSchema") or {
                "type": "object",
                "properties": {},
            }
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": schema,
                    },
                }
            )
        return tools

    # ------------------------------------------------------------------ #
    # 对外 API：工具调用
    # ------------------------------------------------------------------ #

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """通过 MCP 协议执行工具，返回结构化结果。

        注意这里和原 demo 的差别：
            原 demo  ->  应用代码直接 requests.get(...)
            这里     ->  session.call_tool(...)  走 JSON-RPC 到独立 Server 进程
        """
        if self._session is None:
            raise RuntimeError("MCPBridge 尚未 start()，无法调用工具")

        result = self._submit(self._session.call_tool(name, arguments))

        if _attr(result, "is_error", "isError", default=False):
            return {"ok": False, "error": _extract_text(result) or "工具执行失败"}

        return _extract_payload(result)


# --------------------------------------------------------------------------- #
# 结果解析工具函数
# --------------------------------------------------------------------------- #

def _extract_text(result: Any) -> str:
    """把 MCP 返回的 content 数组里的文本片段拼起来。"""
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        if getattr(item, "type", None) == "text":
            parts.append(getattr(item, "text", ""))
    return "\n".join(part for part in parts if part)


def _extract_payload(result: Any) -> dict[str, Any]:
    """优先取 structuredContent；否则把 text 当 JSON 解析。"""
    structured = _attr(result, "structured_content", "structuredContent")
    if isinstance(structured, dict) and structured:
        # FastMCP 有时会把返回值包一层 {"result": {...}}
        if set(structured.keys()) == {"result"} and isinstance(
            structured["result"], dict
        ):
            return structured["result"]
        return structured

    raw_text = _extract_text(result)
    if not raw_text:
        return {"ok": True, "text": ""}

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"ok": True, "text": raw_text}

    if isinstance(parsed, dict) and set(parsed.keys()) == {"result"} and isinstance(
        parsed["result"], dict
    ):
        return parsed["result"]
    if isinstance(parsed, dict):
        return parsed
    return {"ok": True, "text": raw_text}
