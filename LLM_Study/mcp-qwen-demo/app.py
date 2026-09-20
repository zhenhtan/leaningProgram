"""
Gradio 网页界面

运行：
    python app.py
访问：
    http://127.0.0.1:7860

环境变量（PowerShell）：
    $env:DASHSCOPE_API_KEY = "sk-xxxxxxxx"      # 阿里云百炼
    $env:GAODE_API_KEY     = "xxxxxxxxxxxx"     # 高德开放平台（不填则天气工具不可用）

架构：
    Gradio(同步)  ──>  QwenAgent(同步)  ──>  MCPBridge(同步外壳/异步内核)
                                                   │
                                                   │ stdio + JSON-RPC
                                                   ▼
                                        weather_stock_server.py (独立进程)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 必须在导入任何会写 stdout 的库之前设置，保证子进程与主进程编码一致
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import gradio as gr
from mcp import StdioServerParameters

from mcp_bridge import MCPBridge
from qwen_agent import AgentReply, QwenAgent

HERE = Path(__file__).resolve().parent
SERVER_SCRIPT = HERE / "weather_stock_server.py"


# --------------------------------------------------------------------------- #
# 启动时：拉起 MCP Server，建立持久连接，动态发现工具
# --------------------------------------------------------------------------- #

def build_bridge() -> MCPBridge:
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
        env={
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        },
    )
    bridge = MCPBridge(server_params)
    bridge.start()
    return bridge


BRIDGE = build_bridge()
AGENT = QwenAgent(BRIDGE, model=os.getenv("QWEN_MODEL", "qwen-plus"))

# 启动时把发现到的工具打印到控制台，便于确认 MCP 链路通了
print("[MCP] 已连接的 Server 工具：")
for _tool in AGENT.tools:
    print(f"  - {_tool['function']['name']}: {_tool['function']['description'][:40]}...")


# --------------------------------------------------------------------------- #
# 界面辅助
# --------------------------------------------------------------------------- #

def render_steps(steps: list) -> str:
    """把工具调用轨迹渲染成 Markdown，便于观察 MCP 的完整链路。"""
    if not steps:
        return ""

    lines: list[str] = ["---", "**🔧 本轮 MCP 工具调用轨迹**", ""]
    for step in steps:
        data = step.to_dict()
        lines.append(f"**第 {data['round']} 轮 · `{data['tool']}`**")
        lines.append(
            f"- 📥 模型给出的参数（Function Call）："
            f"`{json.dumps(data['arguments'], ensure_ascii=False)}`"
        )
        if data["error"]:
            lines.append(f"- ❌ MCP 调用异常：`{data['error']}`")
        else:
            lines.append(
                "- 📤 MCP Server 返回：\n```json\n"
                + json.dumps(data["result"], ensure_ascii=False, indent=2)
                + "\n```"
            )
        lines.append("")

    lines.append(
        "_链路：Qwen 决定调什么（Function Call） → MCP Client 通过 stdio + JSON-RPC "
        "转发 → MCP Server 真正执行 → 结果回传给 Qwen 生成最终回复_"
    )
    return "\n".join(lines)


def _as_text(content: object) -> str:
    """history 里的 content 可能是 str / dict / 组件对象，统一转成文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or content.get("path") or "")
    if content is None:
        return ""
    return str(content)


def _normalize_history(history: object) -> list[tuple[str, str]]:
    """把不同 Gradio 版本的 history 统一成 [(user, assistant), ...] 形式。

    - Gradio 4    : [("你好", "你好呀"), ...]                        元组列表
    - Gradio 5/6  : [{"role": "user", "content": "..."}, ...]        OpenAI 风格
    """
    if not history:
        return []
    items = list(history)  # type: ignore[arg-type]
    if not items:
        return []

    # 新版：OpenAI 风格的字典列表
    if isinstance(items[0], dict):
        pairs: list[tuple[str, str]] = []
        pending_user: str | None = None
        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = _as_text(item.get("content"))
            if role == "user":
                pending_user = content
            elif role == "assistant":
                pairs.append((pending_user or "", content))
                pending_user = None
        if pending_user is not None:
            pairs.append((pending_user, ""))
        return pairs

    # 旧版：元组列表
    pairs = []
    for item in items:
        try:
            user_msg, bot_msg = item  # type: ignore[misc]
        except (TypeError, ValueError):
            continue
        pairs.append((_as_text(user_msg), _as_text(bot_msg)))
    return pairs


def chat_fn(message: str, history: object) -> str:
    if not message or not str(message).strip():
        return ""

    try:
        reply: AgentReply = AGENT.chat(str(message), _normalize_history(history))
    except Exception as exc:  # noqa: BLE001
        return f"❌ 出错：`{type(exc).__name__}`\n\n```\n{exc}\n```"

    if reply.steps:
        return f"{reply.content}\n\n{render_steps(reply.steps)}"
    return reply.content


# --------------------------------------------------------------------------- #
# 构建界面
# --------------------------------------------------------------------------- #

_gaode_hint = (
    "已配置 ✅" if os.getenv("GAODE_API_KEY") else "未配置 ⚠️（天气工具会返回提示）"
)

demo = gr.ChatInterface(
    fn=chat_fn,
    title="Qwen × MCP · 天气 + 股票助手",
    description=(
        "这个 demo 演示 **MCP 架构**：工具运行在独立的 MCP Server 进程中，"
        "Qwen 通过标准 Function Call 决定调用意图，"
        "再由 MCP Client 经 JSON-RPC 转发执行。\n\n"
        f"高德 API Key：{_gaode_hint}　｜　"
        f"模型：`{os.getenv('QWEN_MODEL', 'qwen-plus')}`　｜　"
        f"可用工具：{', '.join(t['function']['name'] for t in AGENT.tools)}"
    ),
    examples=[
        "北京现在天气怎么样？",
        "上海和深圳的天气分别如何？",
        "帮我查一下 600519 的行情",
        "上证指数现在多少点？",
        "宁德时代（300750）今天涨了多少？",
        "创业板指和沪深300哪个涨得多？",
    ],
    cache_examples=False,
)


if __name__ == "__main__":
    try:
        demo.launch(server_name="127.0.0.1", server_port=7860, show_error=True)
    finally:
        BRIDGE.stop()
