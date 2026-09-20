# 导入依赖库
import dashscope
import os

# 从环境变量中获取 API Key
# 设置方式（PowerShell）：
#   临时（当前会话）：  $env:DASHSCOPE_API_KEY = "你的API Key"
#   永久（用户级）：   [Environment]::SetEnvironmentVariable("DASHSCOPE_API_KEY", "你的API Key", "User")
api_key = os.getenv("DASHSCOPE_API_KEY")
if not api_key:
    raise EnvironmentError(
        "未检测到环境变量 DASHSCOPE_API_KEY，请先设置后再运行："
        "PowerShell 临时设置 `$env:DASHSCOPE_API_KEY = \"你的API Key\""
    )
dashscope.api_key = api_key

# 基于 prompt 生成文本
# 使用 deepseek-v3 模型
def get_completion(prompt, model="deepseek-v3"):
    messages = [{"role": "user", "content": prompt}]    # 将 prompt 作为用户输入
    response = dashscope.Generation.call(
        model=model,
        messages=messages,
        result_format='message',  # 将输出设置为message形式
        temperature=0,  # 模型输出的随机性，0 表示随机性最小
    )
    # 调用失败时直接抛出可读错误，避免后续 NoneType 报错难以定位
    if response.status_code != 200:
        raise RuntimeError(
            f"API 调用失败: status={response.status_code} "
            f"code={response.code} message={response.message} "
            f"request_id={response.request_id}"
        )
    return response.output.choices[0].message.content  # 返回模型生成的文本
    
user_prompt = """
做一个手机流量套餐的客服代表，叫小瓜。可以帮助用户选择最合适的流量套餐产品。可以选择的套餐包括：
经济套餐，月费50元，10G流量；
畅游套餐，月费180元，100G流量；
无限套餐，月费300元，1000G流量；
校园套餐，月费150元，200G流量，仅限在校生。"""

instruction = """
你是一名专业的提示词创作者。你的目标是帮助我根据需求打造更好的提示词。

你将生成以下部分：
提示词：{根据我的需求提供更好的提示词}
优化建议：{用简练段落分析如何改进提示词，需给出严格批判性建议}
问题示例：{提出最多3个问题，以用于和用户更好的交流}
"""

prompt = f"""
# 目标
{instruction}

# 用户提示词
{user_prompt}
"""

response = get_completion(prompt)
print(response)