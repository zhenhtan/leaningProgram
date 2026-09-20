"""
MCP Server —— 对外暴露「天气查询」与「股票行情」两个工具

这个文件是一个独立进程，通过 stdio 上的 JSON-RPC 与 MCP Client 通信。
它自己不知道 Qwen 的存在，也不关心谁在调用它 —— 这正是 MCP 的价值：
工具与模型解耦，任何支持 MCP 的客户端都能复用。

手动调试（可选）：
    python weather_stock_server.py

环境变量：
    GAODE_API_KEY  高德开放平台 Key，用于天气查询
                   https://lbs.amap.com/dev/key/app
    股票行情使用腾讯财经公开接口，无需 Key。
"""

from __future__ import annotations

import os
import re
from typing import Any

import requests

# MCP SDK 版本兼容：
#   1.x -> mcp.server.fastmcp.FastMCP
#   2.x -> mcp.server.mcpserver.MCPServer（FastMCP 已更名）
try:  # MCP SDK >= 2.0
    from mcp.server.mcpserver import MCPServer  # type: ignore[import-not-found]
except ImportError:  # MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

GAODE_API_KEY = os.getenv("GAODE_API_KEY", "")

GAODE_WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="

HTTP_TIMEOUT = 10

mcp = MCPServer("weather-stock")


# --------------------------------------------------------------------------- #
# 工具 1：天气
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_current_weather(location: str = "", adcode: str = "") -> dict[str, Any]:
    """查询中国城市的实时天气。

    适用于「北京天气怎么样」「上海今天冷不冷」这类问题。

    Args:
        location: 城市名称，例如「北京」「上海」「深圳」。
        adcode:   城市行政区划编码，例如北京是 110000、上海是 310000。
                  填写后会优先使用 adcode，比城市名更精确。

    Returns:
        包含天气、温度、湿度、风向、风力、发布时间等字段的字典。
    """
    if not GAODE_API_KEY:
        return {
            "ok": False,
            "error": "服务端未配置环境变量 GAODE_API_KEY，无法查询天气",
        }

    city = adcode.strip() or location.strip()
    if not city:
        return {"ok": False, "error": "必须提供 location 或 adcode 其中之一"}

    try:
        resp = requests.get(
            GAODE_WEATHER_URL,
            params={"key": GAODE_API_KEY, "city": city, "extensions": "base"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return {"ok": False, "error": f"请求高德接口失败：{exc}"}
    except ValueError:
        return {"ok": False, "error": "高德接口返回了非 JSON 内容"}

    if data.get("status") != "1" or not data.get("lives"):
        return {"ok": False, "error": data.get("info") or f"未查询到「{city}」的天气数据"}

    live = data["lives"][0]
    return {
        "ok": True,
        "province": live.get("province", ""),
        "city": live.get("city", city),
        "weather": live.get("weather", ""),
        "temperature_c": live.get("temperature", ""),
        "humidity_pct": live.get("humidity", ""),
        "wind_direction": live.get("winddirection", ""),
        "wind_power": live.get("windpower", ""),
        "report_time": live.get("reporttime", ""),
    }


# --------------------------------------------------------------------------- #
# 工具 2：股票行情
# --------------------------------------------------------------------------- #

# 指数常见别名 —— 因为「上证指数」和「平安银行」的代码都是 000001，
# 纯数字无法区分，必须靠别名表把意图固定下来。
INDEX_ALIASES: dict[str, str] = {
    "上证指数": "sh000001",
    "上证综指": "sh000001",
    "沪指": "sh000001",
    "深证成指": "sz399001",
    "深成指": "sz399001",
    "创业板指": "sz399006",
    "创业板": "sz399006",
    "科创50": "sh000688",
    "科创板50": "sh000688",
    "沪深300": "sh000300",
    "中证500": "sh000905",
    "中证1000": "sh000852",
    "北证50": "bj899050",
}


def _normalize_symbol(symbol: str) -> str:
    """把用户/模型给的各种写法，归一化成腾讯接口要求的 sh600000 形式。

    支持的输入：
        "600000"     -> sh600000   （6 开头 = 沪市）
        "000001"     -> sz000001   （0/3 开头 = 深市）
        "430047"     -> bj430047   （4/8 开头 = 北交所）
        "sh600000"   -> sh600000   （已带前缀，原样返回）
        "上证指数"    -> sh000001   （别名表命中）
    """
    raw = (symbol or "").strip()
    if not raw:
        return ""

    # 1) 先查指数别名（大小写、空格不敏感）
    compact = raw.replace(" ", "")
    if compact in INDEX_ALIASES:
        return INDEX_ALIASES[compact]

    # 2) 已带市场前缀，例如 sh600000 / SZ399001
    lowered = compact.lower()
    if re.fullmatch(r"(sh|sz|bj)\d{6}", lowered):
        return lowered

    # 3) 带 .SH / .SZ 后缀的写法，例如 600000.SH
    m = re.fullmatch(r"(\d{6})\.(sh|sz|bj)", lowered)
    if m:
        return m.group(2) + m.group(1)
    m = re.fullmatch(r"(sh|sz|bj)\.(\d{6})", lowered)
    if m:
        return m.group(1) + m.group(2)

    # 4) 纯 6 位数字，按首位推断市场
    if re.fullmatch(r"\d{6}", compact):
        head = compact[0]
        if head == "6":
            return "sh" + compact
        if head in "03":
            return "sz" + compact
        if head in "48":
            return "bj" + compact
        return "sh" + compact

    # 5) 兜底：交给上游接口报错
    return compact.lower()


@mcp.tool()
def get_stock_quote(symbol: str) -> dict[str, Any]:
    """查询 A 股个股或指数的实时行情（数据来源：腾讯财经公开接口）。

    适用于「600519 现在多少钱」「上证指数今天涨了多少」「平安银行行情」这类问题。
    注意：本工具不支持按股票中文名搜索，请把名称转成代码后再调用
    （例如「贵州茅台」-> 600519，「平安银行」-> 000001）。

    Args:
        symbol: 股票代码或指数别名。支持以下写法：
                - 纯代码：600519、000001、430047（自动推断沪深北）
                - 带前缀：sh600519、sz000001、bj430047
                - 带后缀：600519.SH、000001.SZ
                - 指数别名：上证指数、深证成指、创业板指、沪深300、科创50

    Returns:
        包含现价、涨跌幅、成交额、换手率、市盈率、总市值等字段的字典。
    """
    code = _normalize_symbol(symbol)
    if not code:
        return {"ok": False, "error": "symbol 不能为空"}

    try:
        resp = requests.get(
            TENCENT_QUOTE_URL + code,
            timeout=HTTP_TIMEOUT,
            headers={
                # 腾讯接口对 Referer 有校验，缺少会被拒绝
                "Referer": "https://finance.qq.com/",
                "User-Agent": "Mozilla/5.0",
            },
        )
        resp.raise_for_status()
        # 该接口返回 GBK 编码，requests 猜不出，需要手动指定
        resp.encoding = "gbk"
        text = resp.text
    except requests.RequestException as exc:
        return {"ok": False, "error": f"请求行情接口失败：{exc}"}

    if "=" not in text:
        return {"ok": False, "error": f"行情接口未返回有效数据：{symbol}"}

    # 形如： v_sh600519="1~贵州茅台~600519~1688.00~...";
    payload = text.split("=", 1)[1].strip().rstrip(";").strip('"')
    fields = payload.split("~")

    # 腾讯行情约 50 个字段，少于 45 个说明代码无效或停牌数据异常
    if len(fields) < 45:
        return {"ok": False, "error": f"未识别到「{symbol}」的行情数据，请检查代码是否正确"}

    def num(idx: int) -> float | None:
        """按索引取浮点字段，取不到返回 None。"""
        try:
            value = fields[idx]
        except IndexError:
            return None
        if value in ("", "-", "null"):
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def text_at(idx: int) -> str:
        try:
            return fields[idx]
        except IndexError:
            return ""

    price = num(3)
    prev_close = num(4)
    change = num(31)
    change_pct = num(32)

    return {
        "ok": True,
        "symbol": code,
        "name": text_at(1),
        "code": text_at(2),
        "price": price,
        "prev_close": prev_close,
        "open": num(5),
        "high": num(33),
        "low": num(34),
        "change": change,
        "change_pct": change_pct,
        "up_or_down": (
            "涨" if (change or 0) > 0 else "跌" if (change or 0) < 0 else "平"
        ),
        "volume_hand": num(36),        # 成交量（手）
        "amount_wan": num(37),         # 成交额（万元）
        "turnover_rate_pct": num(38),  # 换手率 %
        "pe_ttm": num(39),             # 市盈率 TTM
        "amplitude_pct": num(43),      # 振幅 %
        "float_mv_yi": num(44),        # 流通市值（亿元）
        "total_mv_yi": num(45),        # 总市值（亿元）
        "pb": num(46),                 # 市净率
        "limit_up": num(47),           # 涨停价
        "limit_down": num(48),         # 跌停价
        "volume_ratio": num(49),       # 量比
        "update_time": text_at(30),    # 行情时间 yyyyMMddHHmmss
    }


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    # 默认走 stdio 传输，由 MCP Client 拉起
    mcp.run()
