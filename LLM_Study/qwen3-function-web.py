"""
Qwen3 + 高德天气 API 的 function calling 网页 demo
基于 Gradio 实现，启动后访问 http://127.0.0.1:7860

环境变量（PowerShell）:
    $env:DASHSCOPE_API_KEY = "你的DashScope-API-Key"
    $env:GAODE_API_KEY     = "你的高德-API-Key"   # https://lbs.amap.com/dev/key/app
"""

import os
import json
from http import HTTPStatus

import requests
import dashscope
import gradio as gr

# 设置 DashScope API Key（阿里云百炼）
dashscope.api_key = os.getenv("DASHSCOPE_API_KEY")
if not dashscope.api_key:
    raise EnvironmentError(
        "未检测到环境变量 DASHSCOPE_API_KEY，请先设置后再运行"
    )

# 高德天气 API Key（与 DashScope Key 不同，需要单独申请）
# 申请地址：https://lbs.amap.com/dev/key/app
GAODE_API_KEY = os.getenv("GAODE_API_KEY")

# 天气工具定义（让模型知道有这个工具可调用）
weather_tool = {
    "type": "function",
    "function": {
        "name": "get_current_weather",
        "description": "Get the current weather in a given location",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "The city name, e.g. 北京",
                },
                "adcode": {
                    "type": "string",
                    "description": "The city code, e.g. 110000 (北京)",
                }
            },
            "required": ["location"],
        },
    },
}


def get_weather_from_gaode(location: str, adcode: str = None):
    """调用高德地图API查询天气"""
    if not GAODE_API_KEY:
        return {"error": "未设置环境变量 GAODE_API_KEY，无法调用高德天气API"}
    base_url = "https://restapi.amap.com/v3/weather/weatherInfo"
    params = {
        "key": GAODE_API_KEY,
        "city": adcode if adcode else location,
        "extensions": "base",  # 可改为 "all" 获取预报
    }
    response = requests.get(base_url, params=params)
    if response.status_code == 200:
        return response.json()
    else:
        return {"error": f"Failed to fetch weather: {response.status_code}"}


def chat_with_qwen(message, history):
    """Gradio ChatInterface 回调函数

    流程:
        1. 把用户输入 + 历史对话组装成 messages
        2. 第一次调用 Qwen，让它决定是否调用 get_current_weather 工具
        3. 如果调用了工具 -> 调高德 API -> 把工具结果回传给 Qwen -> 让它给出自然语言回复
        4. 在最终回复下方附上工具调用过程，方便学习理解 function calling
    """
    # 构造 messages（含历史对话）
    messages = [{"role": "system", "content": "你是一个智能助手，可以查询天气信息。"}]
    for user_msg, bot_msg in history:
        messages.append({"role": "user", "content": user_msg})
        # 历史中 bot_msg 可能含工具调用过程的分隔线，只保留最终回复部分
        clean_bot = bot_msg.split("\n\n---\n")[0] if bot_msg else ""
        messages.append({"role": "assistant", "content": clean_bot})
    messages.append({"role": "user", "content": message})

    # 第一次调用：让模型决定是否调用工具
    response = dashscope.Generation.call(
        model="qwen-turbo",
        messages=messages,
        tools=[weather_tool],
        tool_choice="auto",
    )

    if response.status_code != HTTPStatus.OK:
        return f"❌ 请求失败: {response.code} - {response.message}"

    msg = response.output.choices[0].message

    # 检查是否需要调用工具
    if "tool_calls" in msg:
        tool_call = msg.tool_calls[0]
        args = json.loads(tool_call["function"]["arguments"])
        location = args.get("location", "北京")
        adcode = args.get("adcode")

        # 调用高德天气API
        weather_data = get_weather_from_gaode(location, adcode)

        # 简化版兜底方案：跳过第二次 Qwen 调用，直接用模板格式化输出
        # 原因: qwen-turbo 在 tool 结果回传的多轮 function calling 上兼容性不稳，
        #       跳过这一步可以保证链路稳定，同时仍能展示完整的 function call 流程

        # 处理高德 API 错误
        if "error" in weather_data:
            final_text = f"查询失败：{weather_data['error']}"
        elif "lives" not in weather_data or not weather_data["lives"]:
            final_text = f"未查询到 {location} 的天气数据"
        else:
            # 提取第一条天气数据
            live = weather_data["lives"][0]
            final_text = (
                f"📍 {live.get('province', '')}{live.get('city', location)} "
                f"（更新时间：{live.get('reporttime', '未知')}）\n\n"
                f"🌤 天气：{live.get('weather', '未知')}\n"
                f"🌡 温度：{live.get('temperature', '未知')}℃\n"
                f"💧 湿度：{live.get('humidity', '未知')}%\n"
                f"🌬 风向：{live.get('winddirection', '未知')}风\n"
                f"🍃 风力：{live.get('windpower', '未知')}级"
            )

        # 在最终回复后附上工具调用过程，便于学习
        display = (
            f"{final_text}\n\n"
            f"---\n"
            f"🔧 调用工具: `get_current_weather`\n"
            f"📍 参数: `location={location}, adcode={adcode}`\n"
            f"📡 高德返回:\n```json\n"
            f"{json.dumps(weather_data, ensure_ascii=False, indent=2)}\n```"
        )
        return display
    else:
        # 不需要调用工具，直接返回模型回复
        return msg.content


# 构建 Gradio 网页界面
demo = gr.ChatInterface(
    fn=chat_with_qwen,
    title="Qwen3 天气查询助手",
    description=(
        "基于 Qwen + 高德地图 API 的 function calling demo。"
        "输入如「北京现在天气怎么样？」即可触发工具调用流程。"
    ),
    examples=["北京现在天气怎么样？", "上海的天气如何？", "深圳今天冷不冷？"],
)


if __name__ == "__main__":
    # 启动后访问 http://127.0.0.1:7860
    demo.launch()
