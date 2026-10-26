"""
MCP CLI: 交互式 + 命令行参数两种方式。

用法示例:

    # 交互式
    python cli.py --command npx -- -y @modelcontextprotocol/server-filesystem .
    python cli.py --url https://example.com/mcp --transport streamable_http

    # 命令行直接调用 (注意: --list/--call 是本脚本参数, 必须放在 -- 之前)
    python cli.py --list --command python -- run_server.py
    python cli.py --call my_tool --args-json '{"k": 1}' --command python -- run_server.py

说明: 命令后面用 "--" 分隔 MCP server 的启动参数; --list/--call/--args-json 是本脚本参数, 要写在 "--" 之前。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Optional

from mcp_client import McpClient, McpServerConfig, parse_args_json


def build_config(args: argparse.Namespace) -> McpServerConfig:
    # argparse.REMAINDER 会把 `--` 分隔符本身也收进 args.args, 需剥掉再传给 server
    server_args = args.args[1:] if args.args and args.args[0] == "--" else args.args
    if args.command:
        return McpServerConfig(command=args.command, args=server_args, env=dict(args.env or []))
    if args.url:
        return McpServerConfig(url=args.url, transport=args.transport, headers=dict(args.headers or []))
    raise SystemExit("错误: 必须提供 --command 或 --url. 详见 --help")


async def do_list(client: McpClient) -> int:
    tools = await client.list_tools()
    if not tools:
        print("没有发现任何 tools")
        return 0
    print(f"共 {len(tools)} 个 tools:\n")
    for t in tools:
        print(client.describe_tool(t))
        print()
    return 0


async def do_call(client: McpClient, name: str, args_json: Optional[str]) -> int:
    try:
        arguments = parse_args_json(args_json or "")
    except ValueError as e:
        print(f"参数解析失败: {e}", file=sys.stderr)
        return 1
    result = await client.call_tool(name, arguments)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["isError"] else 0


async def interactive(client: McpClient) -> int:
    print("已连接 MCP。输入 help 查看帮助, 输入 quit 退出。\n")
    while True:
        try:
            line = input("mcp> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            break
        elif cmd in ("help", "?"):
            print("命令:")
            print("  tools / list          列出所有 tools")
            print("  call <name> [json]    执行 tool, 参数为 JSON 或 key=value")
            print("  desc <name>           查看单个 tool 的说明")
            print("  quit                  退出")
        elif cmd in ("tools", "list"):
            tools = await client.list_tools()
            if not tools:
                print("没有发现任何 tools")
            else:
                print(f"共 {len(tools)} 个 tools:")
                for t in tools:
                    print(f"  - {t['name']}: {(t['description'] or '').splitlines()[0] if t['description'] else '(无描述)'}")
        elif cmd == "desc":
            if len(parts) < 2:
                print("用法: desc <name>")
                continue
            tools = await client.list_tools()
            found = next((t for t in tools if t["name"] == parts[1]), None)
            if not found:
                print(f"未找到 tool: {parts[1]}")
            else:
                print(client.describe_tool(found))
        elif cmd == "call":
            if len(parts) < 2:
                print("用法: call <name> [json 参数]")
                continue
            rest = parts[1].split(maxsplit=1)
            name = rest[0]
            args_json = rest[1] if len(rest) > 1 else None
            try:
                arguments = parse_args_json(args_json or "")
            except ValueError as e:
                print(f"参数解析失败: {e}")
                continue
            try:
                result = await client.call_tool(name, arguments)
            except Exception as e:  # noqa: BLE001
                print(f"执行失败: {e}")
                continue
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"未知命令: {cmd}, 输入 help 查看帮助")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="MCP 客户端: 连接 MCP server, 解析并执行 tools",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # 连接配置
    parser.add_argument("--command", help="stdio: MCP server 启动命令 (如 npx/uvx/python)")
    parser.add_argument("--url", help="HTTP/SSE: MCP endpoint URL")
    parser.add_argument("--transport", default="streamable_http",
                        choices=["streamable_http", "sse"],
                        help="--url 的连接类型")
    parser.add_argument("--env", action="append", nargs=2, metavar=("KEY", "VALUE"),
                        help="stdio 环境变量, 可多次使用")
    parser.add_argument("--header", action="append", nargs=2, metavar=("KEY", "VALUE"),
                        dest="headers", help="HTTP 请求头, 可多次使用")
    # 动作
    parser.add_argument("--list", action="store_true", help="列出所有 tools 后退出")
    parser.add_argument("--call", metavar="NAME", help="执行指定 tool 后退出")
    parser.add_argument("--args-json", metavar="JSON", help="配合 --call 传入的参数 JSON")
    # MCP server 启动参数 (跟在 -- 之后)
    parser.add_argument("args", nargs=argparse.REMAINDER, help="-- 之后的 MCP server 启动参数")

    args = parser.parse_args()

    config = build_config(args)
    client = McpClient(config)

    await client.connect()
    try:
        if args.list:
            return await do_list(client)
        if args.call:
            return await do_call(client, args.call, args.args_json)
        return await interactive(client)
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
