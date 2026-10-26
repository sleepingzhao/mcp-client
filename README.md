# MCP 客户端 (Python)

连接 MCP Server、解析并执行其 Tools 的工具集，提供 **CLI** 和 **Web 页面** 两种使用方式。

支持两种传输：
- **stdio**：本地启动 MCP 服务进程（`npx` / `uvx` / `python` 等）
- **HTTP/SSE**：连接远程端点（`streamable_http` 或 `sse`）

## 安装

```bash
pip install -r requirements.txt
```

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `mcp_client.py` | 核心库：连接、`list_tools`、`call_tool`、参数解析 |
| `cli.py` | 命令行工具（交互式 + 一次性调用） |
| `web_server.py` | Flask Web 后端 + REST API |
| `static/index.html` | 前端页面（原生 HTML/JS，无构建） |
| `requirements.txt` | 依赖清单 |

## Web 用法

```bash
# 启动(括号里可选，启动时指定连接)
python web_server.py (--command npx -- -y @modelcontextprotocol/server-filesystem .)
```

浏览器打开 `http://127.0.0.1:5000`：

1. 左侧选择连接方式、填写配置，点击「连接」
2. 自动加载 Tools，点击某个 Tool 查看详情
3. 填写参数（JSON），点击「执行」查看结果

## CLI 用法

```bash
# 交互式（stdio）
python cli.py --command npx -- -y @modelcontextprotocol/server-filesystem .

# 交互式（远程 HTTP）
python cli.py --url http://localhost:8080/mcp --transport streamable_http

# 一次性调用：列出 tools (--list 是本脚本参数, 要写在 -- 之前)
python cli.py --list --command npx -- -y @modelcontextprotocol/server-filesystem .

# 一次性调用：执行某个 tool
python cli.py --call read_file --args-json '{"path": "README.md"}' \
    --command npx -- -y @modelcontextprotocol/server-filesystem .
```

> 注意：`--` 之后是 MCP server 的启动参数，`--` 之前是本脚本的参数（`--list` / `--call` / `--args-json` 都要写在 `--` 之前）。

交互式命令：`tools`/`list`（列出）、`desc <name>`（看说明）、`call <name> [json]`（执行）、`quit`（退出）。

## REST API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/status` | 连接状态 |
| POST | `/api/connect` | 建立连接 `{"command", "args", "env"}` 或 `{"url", "transport"}` |
| POST | `/api/tools` | 列出 tools |
| POST | `/api/call` | 执行 `{"name", "arguments"}` |

## 说明

- 基于官方 `mcp` Python SDK（新版 JSON-RPC 协议，2025-06-18 之后）。
- Web 后端用独立 `asyncio` 事件循环在同一进程内串行驱动 async 的 MCP 会话（`run_async`）。
