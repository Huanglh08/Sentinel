# Ragent MCP Server (Python / FastMCP)

将原 Java 版 `mcp-server`（`McpServerApplication` + 4 个 Executor）用 Python 官方
`mcp` SDK 的 **FastMCP** 高层 API 重写，工具名/入参/描述/返回格式**与原版完全一致**，
对调用方（Java MCP 客户端）协议级兼容，无需改动 Java 端业务代码。

## 工具清单

| 工具名 | 对应 Java Executor | 说明 |
|---|---|---|
| `weather_query` | `WeatherMcpExecutor` | 天气查询（当前/预报），支持 20 个城市 |
| `ticket_query` | `TicketMcpExecutor` | 客户工单查询（汇总/列表/统计） |
| `sales_query` | `SalesMcpExecutor` | 销售数据查询（汇总/排名/明细/趋势） |
| `youcom_search` | `YouComSearchMcpExecutor` | 联网搜索（**需 `YDC_API_KEY`，缺失则不注册**） |

## 运行

```bash
pip install -r requirements.txt

# 1) stdio 模式（默认）—— 作为本地子进程被 Java 端拉起
python server.py

# 2) SSE 模式 —— 作为独立 HTTP 服务，Java 端通过 URL 远程接入
python server.py --transport sse
#   默认监听 http://127.0.0.1:8000/sse
```

## 与 Java 端集成

原 Java 端通过 MCP 客户端 discover/调用工具。两种接法二选一：

### A. stdio 子进程（最贴近原 Java 版）
Java 端把"启动命令"从 `java -jar mcp-server.jar` 换成
`python <path>/mcp-server-py/server.py`。工具名/参数 schema 不变，业务零改动。

### B. SSE 远程接入（推荐，便于独立部署/扩缩容）
1. 启动 `python server.py --transport sse`（监听 8000）。
2. Java MCP 客户端配置 SSE endpoint：`http://127.0.0.1:8000/sse`。
3. `youcom_search` 需要 Java 侧/运行环境注入 `YDC_API_KEY` 环境变量；
   未配置时该工具不注册（与原 `@ConditionalOnProperty` 行为一致）。

## 与原 Java 版的差异

- **语言/运行时**：Java 17 + Spring Boot → Python 3.10+ + FastMCP
- **工具注册**：`@Tool` 注解 + 显式 inputSchema → `@mcp.tool()` 装饰器 + 类型注解自动推导
- **逻辑一致性**：天气/工单/销售的生成算法按原版复刻，返回文本格式逐行对齐
- **联网搜索**：保留"无 `YDC_API_KEY` 则不注册"语义

## 迁移位置（对应 ARCHITECTURE_SPLIT.md）

属于切分图中 **`mcp-server/**` → Python 重写 (FastMCP)**，是跨语言落地的第一个、
风险最低的试点（原为独立进程，与 Java 主服务解耦）。
