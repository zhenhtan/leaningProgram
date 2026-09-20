"""
Qwen Agent —— Function Call 多轮循环

职责边界：
    本文件只负责「和模型对话」以及「决定要调哪个工具」，
    至于工具怎么执行、去哪里执行，全部委托给 MCPBridge（即 MCP 层）。

对比原 demo 的两点改进：
    1. 使用 DashScope 的 OpenAI 兼容模式，tool_calls 走标准协议格式，
       多轮回传（assistant.tool_calls -> tool -> assistant）链路稳定；
    2. 工具不再硬编码在本地字典里，而是运行时从 MCP Server 动态发现。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from mcp_bridge import MCPBridge

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

SYSTEM_PROMPT = """你是一个智能助手，可以查询天气和 A 股行情。

工具使用规则：
1. 用户问天气 —— 调用 get_current_weather，把城市名放进 location 参数。
2. 用户问股票、指数、涨跌 —— 调用 get_stock_quote。
   - 用户给的是中文股票名时，请转换成 6 位代码再调用
     （例：贵州茅台 -> 600519，平安银行 -> 000001，宁德时代 -> 300750）。
   - 用户问大盘时直接传别名即可（上证指数、深证成指、创业板指、沪深300、科创50）。
3. 工具返回后，用简洁自然的中文总结，不要罗列原始 JSON。
4. 表示涨跌：涨用 ↑、跌用 ↓，并带上具体涨跌幅数字。
5. 如果一次提问涉及多个对象（例如「北京和上海天气」），请分别调用工具后再汇总。
"""

# 防止模型陷入无限调用，设定安全轮数上限
MAX_TOOL_ROUNDS = 6


@dataclass
class ToolStep:
    """记录一次工具调用的完整轨迹，用于界面展示。"""

    round_index: int
    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_index,
            "tool": self.tool,
            "arguments": self.arguments,
            "result": self.result,
            "error": self.error,
        }


@dataclass
class AgentReply:
    content: str
    steps: list[ToolStep] = field(default_factory=list)


class QwenAgent:
    """把 Qwen 的 Function Call 与 MCP 工具执行串起来。"""

    def __init__(self, bridge: MCPBridge, model: str = "qwen-plus") -> None:
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "未检测到环境变量 DASHSCOPE_API_KEY，请先设置后再运行\n"
                'PowerShell: $env:DASHSCOPE_API_KEY = "sk-xxxx"'
            )

        self.model = model
        self.bridge = bridge
        self.client = OpenAI(api_key=api_key, base_url=DASHSCOPE_BASE_URL)

        # 工具清单来自 MCP Server，而不是写在本地
        self.tools: list[dict[str, Any]] = bridge.list_tools()
        if not self.tools:
            raise RuntimeError("MCP Server 未暴露任何工具，请检查 weather_stock_server.py")

    # ------------------------------------------------------------------ #
    # 对外主入口
    # ------------------------------------------------------------------ #

    def chat(
        self,
        message: str,
        history: list[tuple[str, str]] | None = None,
    ) -> AgentReply:
        """处理一轮用户输入，返回最终回复 + 工具调用轨迹。"""
        messages = self._build_messages(message, history or [])
        steps: list[ToolStep] = []

        for round_index in range(1, MAX_TOOL_ROUNDS + 1):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tools,
                tool_choice="auto",
                temperature=0.3,
            )

            choice = response.choices[0]
            assistant_msg = choice.message

            # 模型认为不需要调工具 —— 直接给出自然语言答案，收工
            if not assistant_msg.tool_calls:
                return AgentReply(
                    content=assistant_msg.content or "(模型未返回内容)",
                    steps=steps,
                )

            # ---- 回填 assistant 的 tool_calls 消息（协议要求） ----
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_msg.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in assistant_msg.tool_calls
                    ],
                }
            )

            # ---- 逐个执行工具，结果以 role=tool 回填 ----
            for call in assistant_msg.tool_calls:
                tool_name = call.function.name
                arguments = _safe_json_loads(call.function.arguments)

                try:
                    result = self.bridge.call_tool(tool_name, arguments)
                    error = None
                except Exception as exc:  # noqa: BLE001 —— 工具异常不应中断整轮对话
                    result = {"ok": False, "error": str(exc)}
                    error = f"{type(exc).__name__}: {exc}"

                steps.append(
                    ToolStep(
                        round_index=round_index,
                        tool=tool_name,
                        arguments=arguments,
                        result=result,
                        error=error,
                    )
                )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        # 轮数用尽仍未收敛
        return AgentReply(
            content="抱歉，工具调用轮数已达上限，请把问题拆得更具体一些再试。",
            steps=steps,
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _build_messages(
        self,
        message: str,
        history: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """把系统提示词 + 历史对话 + 本轮输入组装成 messages。"""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

        for user_msg, bot_msg in history:
            if user_msg:
                messages.append({"role": "user", "content": user_msg})

            # 历史里可能带有「---」分隔的工具轨迹，只保留最终回复
            clean_bot = (bot_msg or "").split("\n\n---\n")[0].strip()
            if clean_bot:
                messages.append({"role": "assistant", "content": clean_bot})

        messages.append({"role": "user", "content": message})
        return messages


def _safe_json_loads(raw: str | None) -> dict[str, Any]:
    """解析模型给出的参数 JSON，失败时降级为空字典。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
