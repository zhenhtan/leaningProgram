"""
自检脚本 —— 验证 MCP 链路是否正常。

不需要任何 API Key：股票工具走公开接口，天气工具只做连通性检查。

用法：
    python selftest.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:  # Windows 控制台也能正确显示中文
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"

failures: list[str] = []


def section(title: str) -> None:
    print()
    print("=" * 66)
    print(title)
    print("=" * 66)


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = PASS if ok else FAIL
    line = f"{mark} {label}"
    if detail:
        line += f"  —— {detail}"
    print(line)
    if not ok:
        failures.append(label)


# --------------------------------------------------------------------------- #
# 1. 依赖检查
# --------------------------------------------------------------------------- #

section("1 / 4  依赖检查")

missing: list[str] = []
for mod, pip_name in [
    ("mcp", "mcp"),
    ("openai", "openai"),
    ("gradio", "gradio"),
    ("requests", "requests"),
]:
    try:
        m = __import__(mod)
        version = getattr(m, "__version__", "?")
        print(f"{PASS} {pip_name:<10} {version}")
    except ImportError:
        print(f"{FAIL} {pip_name:<10} 未安装")
        missing.append(pip_name)

if missing:
    print()
    print(f"请先安装缺失依赖：pip install {' '.join(missing)}")
    sys.exit(1)

print()
print(f"{PASS} Python {sys.version.split()[0]}")

# MCP SDK 大版本探测
try:
    from mcp.server.mcpserver import MCPServer  # noqa: F401

    mcp_generation = "2.x (MCPServer)"
except ImportError:
    from mcp.server.fastmcp import FastMCP  # noqa: F401

    mcp_generation = "1.x (FastMCP)"
print(f"{PASS} MCP SDK 版本线：{mcp_generation}")

# --------------------------------------------------------------------------- #
# 2. 环境变量检查
# --------------------------------------------------------------------------- #

section("2 / 4  环境变量检查")

if os.getenv("DASHSCOPE_API_KEY"):
    print(f"{PASS} DASHSCOPE_API_KEY 已设置")
else:
    print(f"{WARN} DASHSCOPE_API_KEY 未设置 —— app.py 将无法启动（自检不受影响）")

if os.getenv("GAODE_API_KEY"):
    print(f"{PASS} GAODE_API_KEY 已设置")
else:
    print(f"{WARN} GAODE_API_KEY 未设置 —— 天气工具会返回提示信息")

# --------------------------------------------------------------------------- #
# 3. MCP 连接与工具发现
# --------------------------------------------------------------------------- #

section("3 / 4  MCP 连接与工具发现")

from mcp import StdioServerParameters  # noqa: E402

from mcp_bridge import MCPBridge  # noqa: E402

server_script = HERE / "weather_stock_server.py"
check("MCP Server 脚本存在", server_script.exists(), server_script.name)
if not server_script.exists():
    sys.exit(1)

params = StdioServerParameters(
    command=sys.executable,
    args=[str(server_script)],
    env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
)

bridge = MCPBridge(params)

try:
    bridge.start()
    print(f"{PASS} 子进程启动 + stdio 握手成功")
except Exception as exc:  # noqa: BLE001
    check("子进程启动 + stdio 握手", False, f"{type(exc).__name__}: {exc}")
    sys.exit(1)

try:
    tools = bridge.list_tools()
except Exception as exc:  # noqa: BLE001
    check("list_tools()", False, f"{type(exc).__name__}: {exc}")
    bridge.stop()
    sys.exit(1)

check("list_tools() 动态发现工具", len(tools) > 0, f"共 {len(tools)} 个")

tool_names = []
for t in tools:
    fn = t["function"]
    tool_names.append(fn["name"])
    props = list(fn["parameters"].get("properties", {}).keys())
    print(f"      · {fn['name']}({', '.join(props)})")

check("天气工具已注册", "get_current_weather" in tool_names)
check("股票工具已注册", "get_stock_quote" in tool_names)

# --------------------------------------------------------------------------- #
# 4. 通过 MCP 协议真实调用工具
# --------------------------------------------------------------------------- #

section("4 / 4  通过 MCP 协议调用工具")

quote_cases = [
    ("600519", "贵州茅台"),
    ("000001", "平安银行"),
    ("300750", "宁德时代"),
    ("上证指数", "上证指数"),
    ("sh000001", "上证指数"),
]

ok_count = 0
for symbol, expected_name in quote_cases:
    try:
        result = bridge.call_tool("get_stock_quote", {"symbol": symbol})
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} {symbol:<10} 异常 {type(exc).__name__}: {exc}")
        continue

    if not result.get("ok"):
        print(f"{FAIL} {symbol:<10} {result.get('error')}")
        continue

    name = result.get("name", "")
    price = result.get("price")
    direction = result.get("up_or_down", "")
    pct = result.get("change_pct")
    match = expected_name in name
    mark = PASS if match else WARN
    print(f"{mark} {symbol:<10} {name:<8} {price}  {direction} {pct}%")
    if match:
        ok_count += 1

check(
    "股票工具调用",
    ok_count >= 4,
    f"{ok_count}/{len(quote_cases)} 个用例返回预期标的",
)

# 参数校验
try:
    bad = bridge.call_tool("get_stock_quote", {"symbol": "not-a-real-code"})
    check("无效代码被正确拒绝", bad.get("ok") is False, bad.get("error", ""))
except Exception as exc:  # noqa: BLE001
    check("无效代码被正确拒绝", False, f"{type(exc).__name__}: {exc}")

# 天气工具（无 Key 时应当返回友好错误而不是崩溃）
try:
    weather = bridge.call_tool("get_current_weather", {"location": "北京"})
    if weather.get("ok"):
        print(
            f"{PASS} 天气工具        {weather.get('city')} "
            f"{weather.get('weather')} {weather.get('temperature_c')}℃"
        )
    else:
        print(f"{WARN} 天气工具        {weather.get('error')}")
except Exception as exc:  # noqa: BLE001
    check("天气工具调用不崩溃", False, f"{type(exc).__name__}: {exc}")

bridge.stop()
print(f"{PASS} 连接已关闭")

# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #

section("自检结果")

if failures:
    print(f"{FAIL} 有 {len(failures)} 项未通过：")
    for f in failures:
        print(f"    - {f}")
    sys.exit(1)

print(f"{PASS} MCP 链路完全正常 —— 可以执行 python app.py 启动界面了")
print()
print("提示：若天气工具显示 WARN，请设置 GAODE_API_KEY 后重试。")
