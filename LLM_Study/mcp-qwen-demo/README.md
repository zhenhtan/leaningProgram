# Qwen × MCP · 天气 + 股票助手

把原来的「单文件 Function Call demo」重构成一个**标准 MCP 架构**的可运行项目：
工具跑在独立的 MCP Server 进程里，Qwen 通过 Function Call 决定调用意图，
再由 MCP Client 经 JSON-RPC 转发执行。

---

## 一、它和原 demo 的区别

| 维度 | 原 demo（单文件） | 本项目（MCP 架构） |
|---|---|---|
| 工具定义 | 硬编码在 `weather_tool` 字典 | MCP Server 运行时动态暴露 |
| 工具发现 | 程序员写死 | `session.list_tools()` 自动发现 |
| 工具执行 | 业务代码里直接 `requests.get` | MCP Client → `session.call_tool()` |
| 进程模型 | 单进程 | Client 与 Server 是两个独立进程 |
| 加新工具 | 改业务代码 | 新写一个 MCP Server，**Client 不用改** |
| 工具复用 | 只能被这个 demo 用 | 任何支持 MCP 的客户端都能用 |
| 多轮回传 | 简化处理（跳过第二轮） | 完整实现标准 tool_calls 往返 |

> **一句话**：原 demo 展示的是 Function Call「是什么」，
> 本项目展示的是 MCP 如何把工具调用「工程化」。

---

## 二、项目结构

```
mcp-qwen-demo/
├── weather_stock_server.py   # 【MCP Server】独立进程，暴露天气 + 股票两个工具
├── mcp_bridge.py             # 【MCP Client】后台事件循环 + 持久连接，同步化外壳
├── qwen_agent.py             # 【Qwen Agent】Function Call 多轮循环
├── app.py                    # 【Gradio UI】启动入口
├── requirements.txt
└── README.md
```

调用链路：

```
Gradio(同步) ──> QwenAgent(同步) ──> MCPBridge(同步壳 / 异步核)
                                          │
                                          │ stdio + JSON-RPC
                                          ▼
                              weather_stock_server.py（独立进程）
                                  ├── get_current_weather  高德天气
                                  └── get_stock_quote      腾讯财经行情
```

---

## 三、快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

> 需要 Python 3.10 及以上（MCP SDK 的要求）。

### 2. 设置环境变量

**PowerShell**

```powershell
$env:DASHSCOPE_API_KEY = "sk-xxxxxxxxxxxxxxxx"   # 阿里云百炼，必填
$env:GAODE_API_KEY     = "xxxxxxxxxxxxxxxx"      # 高德开放平台，天气工具用
```

**Bash**

```bash
export DASHSCOPE_API_KEY="sk-xxxxxxxxxxxxxxxx"
export GAODE_API_KEY="xxxxxxxxxxxxxxxx"
```

| 变量 | 必填 | 用途 | 申请地址 |
|---|---|---|---|
| `DASHSCOPE_API_KEY` | ✅ | 调 Qwen 模型 | https://bailian.console.aliyun.com/ |
| `GAODE_API_KEY` | ⭕ | 查天气 | https://lbs.amap.com/dev/key/app |
| `QWEN_MODEL` | ⭕ | 默认 `qwen-plus` | — |

> **股票行情不需要任何 Key**，走的是腾讯财经公开接口。
> 未配置 `GAODE_API_KEY` 时程序仍可启动，只是天气工具会返回友好提示。

### 3. 运行

```bash
python app.py
```

打开 http://127.0.0.1:7860

启动时控制台会打印 MCP Server 暴露的工具清单：

```
[MCP] 已连接的 Server 工具：
  - get_current_weather: 查询中国城市的实时天气。...
  - get_stock_quote: 查询 A 股个股或指数的实时行情...
```

---

## 四、试试这些问题

**天气**

- 北京现在天气怎么样？
- 上海和深圳的天气分别如何？

**股票**

- 帮我查一下 600519 的行情
- 上证指数现在多少点？
- 宁德时代（300750）今天涨了多少？
- 创业板指和沪深300哪个涨得多？

**混合**

- 北京天气和上证指数一起告诉我

每次回答下方都会附上 **MCP 工具调用轨迹**，可以看到：
模型给出什么参数 → MCP Server 返回了什么 → 最终如何被总结成自然语言。

---

## 五、代码看点

### 1. 工具是「动态发现」的（`mcp_bridge.py`）

```python
self.tools = bridge.list_tools()   # 运行时问 Server，而不是写死
```

Client 事先并不知道有哪些工具，是启动时通过 MCP 协议问出来的。
这就是为什么「新增工具不需要改 Client」。

### 2. 工具执行是「跨进程」的（`mcp_bridge.py`）

```python
result = self._submit(self._session.call_tool(name, arguments))
```

这一行背后发生的事情：序列化成 JSON-RPC → 写进子进程 stdin →
Server 执行 → 结果写回 stdout → 反序列化。

### 3. 异步包装成同步（`mcp_bridge.py`）

MCP SDK 是 async 的，Gradio 回调是同步的。桥接层用
「后台线程 + 专属事件循环 + `run_coroutine_threadsafe`」
把两者缝合，同时保持连接常驻，避免每次请求都重启子进程。

### 4. 标准的 Function Call 多轮往返（`qwen_agent.py`）

```python
messages.append({"role": "assistant", "tool_calls": [...]})   # 回填调用意图
messages.append({"role": "tool", "tool_call_id": call.id, ...})  # 回填执行结果
```

原 demo 为了规避兼容性问题跳过了第二次模型调用，这里改用
DashScope 的 **OpenAI 兼容模式**，多轮链路是标准且稳定的。

---

## 六、版本兼容性（实测）

本项目对依赖做了**双版本兼容**，避免因为 SDK 大版本升级踩坑：

| 依赖 | 版本差异 | 本项目处理方式 |
|---|---|---|
| `mcp` | 1.x 用 `FastMCP`，2.x 更名为 `MCPServer` | `try/except ImportError` 自动选择 |
| `mcp` 数据模型 | 1.x 字段是 `inputSchema`，2.x 改成 `input_schema` | `_attr()` 辅助函数按双名查找 |
| `gradio` | 4.x 的 history 是元组列表，5/6.x 变成 OpenAI 风格字典 | `_normalize_history()` 统一归一化 |
| `openai` | 3.x 的 `chat.completions.create` 接口未变 | 无需处理 |

**实测环境**：Python 3.13.12 · mcp 2.2.0 · openai 3.15.0 · gradio 6.27.0

### 实测记录

| 验证项 | 结果 |
|---|---|
| MCP Server 子进程启动 + stdio 握手 | ✅ 通过 |
| `list_tools()` 动态发现 2 个工具 | ✅ 通过 |
| `call_tool()` 经 MCP 协议调用股票工具 | ✅ 通过（贵州茅台 / 平安银行 / 上证指数 / 宁德时代） |
| 指数别名识别（「上证指数」→ `sh000001`） | ✅ 通过 |
| 错误处理（无效代码 / 缺少 API Key） | ✅ 通过 |
| Gradio 服务启动 + HTTP 200 响应 | ✅ 通过 |
| history 双版本归一化 | ✅ 通过 |

---

## 七、常见问题

**Q：为什么股票不用高德？**
高德提供的是地图服务，没有行情数据。这里用腾讯财经公开接口，免 Key、字段全。

**Q：为什么「000001」查出来是平安银行而不是上证指数？**
两者代码相同。项目内置了别名表，问「上证指数」会走 `sh000001`，
问「000001」按深市股票处理。

**Q：能不能换成 DeepSeek 或其他模型？**
可以。改 `qwen_agent.py` 里的 `base_url` 和 `model` 即可，
只要该模型支持 OpenAI 格式的 function calling。

**Q：为什么每次启动都要重启一次 Server？**
本项目是「一次启动、常驻连接」。只有程序重启时才会重新拉起 Server 进程。

**Q：能接入 Claude Desktop 或 WorkBuddy 吗？**
可以。Server 是标准 MCP 实现，把它写进对应的 MCP 配置里即可，无需改代码。

**Q：我装的是 mcp 1.x，能跑吗？**
能。`weather_stock_server.py` 里的 import 做了双版本判断，
1.x 走 `FastMCP`，2.x 走 `MCPServer`，两个版本都能正常运行。

**Q：为什么用 openai SDK 而不是 dashscope SDK？**
DashScope 提供了 OpenAI 兼容端点，用标准客户端写出来的 function calling
代码格式更规范、更通用 —— 想换成 DeepSeek、Kimi 等模型时，
只需要改 `base_url` 和 `model` 两个值。
