"""
MCP 客户端核心库。

支持:
- stdio 传输: 本地启动一个 MCP 服务进程 (npx / uvx / python ...)
- SSE/HTTP 传输: 连接到一个已经运行的 HTTP/SSE 端点 (streamable http 或 sse)

基于官方 mcp Python SDK 的新版协议 (ClientSession, 2025-06-18 之后的 JSON-RPC)。

依赖: pip install mcp
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class McpServerConfig:
    """MCP 服务器连接配置。三种方式任选其一。"""

    # --- stdio 方式 ---
    command: Optional[str] = None      # 例如 "npx" / "uvx" / "python"
    args: list[str] = field(default_factory=list)  # 例如 ["-y", "@modelcontextprotocol/server-filesystem", "."]
    env: dict[str, str] = field(default_factory=dict)

    # --- SSE / HTTP 方式 ---
    url: Optional[str] = None          # 例如 "https://example.com/mcp" 或 "http://localhost:8080/sse"
    # 连接类型: "sse" 或 "streamable_http" (新版 streamable HTTP transport)
    transport: str = "streamable_http"
    headers: dict[str, str] = field(default_factory=dict)  # 可选, 认证头等


# ---------------------------------------------------------------------------
# 客户端封装
# ---------------------------------------------------------------------------

class McpClient:
    """封装 MCP 连接与 tools 的解析/执行。"""

    def __init__(self, config: McpServerConfig):
        self.config = config
        self.session: Optional[ClientSession] = None
        self._read = None
        self._write = None
        self._exit_stack = None

    # ---- 连接管理 ----

    async def connect(self) -> None:
        """根据配置建立连接并初始化会话, 拉取 tools。"""
        from contextlib import AsyncExitStack

        self._exit_stack = AsyncExitStack()

        if self.config.command:
            # stdio 方式 (Windows 上 .cmd/.bat/.exe 的解析由 SDK 的
            # get_windows_executable_command 处理, 无需自定义包装)
            params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env={**self.config.env} if self.config.env else None,
            )
            self._read, self._write = await self._exit_stack.enter_async_context(
                stdio_client(params)
            )
        elif self.config.url:
            if self.config.transport == "sse":
                self._read, self._write = await self._exit_stack.enter_async_context(
                    sse_client(self.config.url, headers=self.config.headers or None)
                )
            else:
                # streamable_http 客户端本身不接受 headers, 通过带 headers 的
                # httpx.AsyncClient 传入 (create_mcp_http_client 保留推荐的超时配置)
                http_client = create_mcp_http_client(
                    headers=self.config.headers or None
                )
                self._read, self._write = await self._exit_stack.enter_async_context(
                    streamable_http_client(self.config.url, http_client=http_client)
                )
        else:
            raise ValueError("必须配置 command (stdio) 或 url (http/sse) 之一")

        self.session = await self._exit_stack.enter_async_context(
            ClientSession(self._read, self._write)
        )
        await self.session.initialize()

    async def close(self) -> None:
        """关闭连接。"""
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self.session = None

    # ---- tools 解析 ----

    async def list_tools(self) -> list[dict[str, Any]]:
        """列出并解析所有 tools, 返回便于展示的字典列表。"""
        self._ensure_connected()
        result = await self.session.list_tools()
        tools = []
        for t in result.tools:
            tools.append({
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.input_schema or {},
            })
        return tools

    def _schema_properties(self, tool: dict[str, Any]) -> dict[str, Any]:
        schema = tool.get("input_schema") or {}
        return schema.get("properties") or {}

    def _schema_required(self, tool: dict[str, Any]) -> list[str]:
        schema = tool.get("input_schema") or {}
        return schema.get("required") or []

    def describe_tool(self, tool: dict[str, Any]) -> str:
        """生成单条 tool 的可读描述文本。"""
        lines = [f"## {tool['name']}"]
        if tool.get("description"):
            lines.append(tool["description"].strip())
        props = self._schema_properties(tool)
        required = set(self._schema_required(tool))
        if props:
            lines.append("参数:")
            for pname, pschema in props.items():
                ptype = pschema.get("type", "any")
                req = "必填" if pname in required else "可选"
                desc = (pschema.get("description") or "").strip()
                lines.append(f"  - {pname} ({ptype}, {req}){': ' + desc if desc else ''}")
        else:
            lines.append("参数: 无")
        return "\n".join(lines)

    # ---- tools 执行 ----

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """执行指定 tool, 返回结构化结果。"""
        self._ensure_connected()
        result = await self.session.call_tool(name, arguments)

        content_list = []
        for item in result.content:
            if item.type == "text":
                content_list.append({"type": "text", "text": item.text})
            elif item.type == "image":
                content_list.append({
                    "type": "image",
                    "mimeType": getattr(item, "mime_type", "image/png"),
                    "data": getattr(item, "data", ""),
                })
            elif item.type == "resource":
                content_list.append({"type": "resource", "resource": str(getattr(item, "resource", ""))})
            else:
                content_list.append({"type": item.type, "data": str(item)})

        return {
            "isError": bool(result.is_error),
            "content": content_list,
        }

    async def ping(self, timeout: float = 5.0) -> bool:
        """轻量存活检测: 发送 MCP ping, 超时或失败返回 False。"""
        try:
            self._ensure_connected()
            await asyncio.wait_for(self.session.send_ping(), timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _ensure_connected(self) -> None:
        if self.session is None:
            raise RuntimeError("未连接, 请先调用 await connect()")


# ---------------------------------------------------------------------------
# 便捷: 解析 arguments 字符串为 dict
# ---------------------------------------------------------------------------

def parse_args_json(text: str) -> dict[str, Any]:
    """把用户输入解析成 dict。依次尝试: JSON -> Python dict 字面量 -> key=value。

    容忍 shell 场景下双引号被剥掉的情况 (如 PowerShell), 例如:
        {"url":"https://example.com","max_length":200}
        {url:https://example.com,max_length:200}
        url=https://example.com max_length=200
    """
    text = text.strip()
    if not text:
        return {}
    # 1. 标准 JSON
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError("参数必须是 JSON 对象 (dict)")
    except json.JSONDecodeError:
        pass
    # 2. Python dict 字面量 (容忍无引号的 key, ast 对无引号 key 也支持)
    try:
        import ast
        parsed = ast.literal_eval(text)
        if isinstance(parsed, dict):
            return {str(k): v for k, v in parsed.items()}
    except Exception:
        pass
    # 3. 剥离外层大括号后按 key:value / key=value 解析
    body = text.strip()
    if body.startswith("{") and body.endswith("}"):
        body = body[1:-1].strip()
    # 先按顶层逗号切分; 无逗号时按空白切分 (如 "a=1 b=2"), 保护引号/括号内的空白
    parts = _split_top_level(body)
    if len(parts) == 1:
        space_parts = _split_top_level(parts[0], " ")
        if len(space_parts) > 1:
            parts = space_parts
    parts = [p for p in parts if p.strip()]
    if not parts:
        raise ValueError(f"无法解析参数: {text!r} (需 JSON 对象或 key=value)")
    data = {}
    for part in parts:
        if "=" in part:
            k, v = part.split("=", 1)
        elif ":" in part:
            k, v = part.split(":", 1)
        else:
            raise ValueError(f"无法解析参数片段: {part!r}")
        data[k.strip().strip("{}'\" ")] = _coerce(v.strip())
    return data


def _split_top_level(s: str, sep: str = ",") -> list[str]:
    """按 sep 切分, 忽略括号/引号内部的 sep (sep 为 " " 时按任意空白切分)。"""
    parts = []
    depth = 0
    in_str = None
    cur = []
    for ch in s:
        if in_str:
            cur.append(ch)
            if ch == in_str:
                in_str = None
            continue
        if ch in "\"'":
            in_str = ch
            cur.append(ch)
        elif ch in "[{(":
            depth += 1
            cur.append(ch)
        elif ch in "]})":
            depth -= 1
            cur.append(ch)
        elif depth == 0 and (ch == sep or (sep == " " and ch.isspace())):
            if cur:
                parts.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _coerce(v: str) -> Any:
    v = v.strip()
    # 去掉包裹的引号
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v == "true":
        return True
    if v == "false":
        return False
    if v == "null" or v == "None":
        return None
    try:
        return int(v)
    except ValueError:  # pragma: no cover - 只为可读性
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v
