"""
MCP Web 服务器: 通过 Flask 暴露 REST API 和前端页面。

API:
    GET  /                  前端页面 (static/index.html)
    GET  /api/status        连接状态
    POST /api/connect       建立 MCP 连接 (JSON: 同 McpServerConfig)
    POST /api/tools         列出所有 tools
    POST /api/call          执行 tool (JSON: {"name":..., "arguments": {...}})

运行:
    python web_server.py --command npx -- -y @modelcontextprotocol/server-filesystem .
    python web_server.py --url http://localhost:8080/mcp --transport streamable_http

连接配置可通过命令行在启动时指定 (启动即连接), 也可启动后通过 POST /api/connect 动态配置。
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import sys
import threading
import webbrowser
from typing import Any, Optional

from flask import Flask, jsonify, request, send_from_directory

from mcp_client import McpClient, McpServerConfig


def _frozen_base_dir() -> str:
    """打包成 exe 后 (sys.frozen), 返回 exe 所在目录 (用于存放用户配置)。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _static_dir() -> str:
    """静态资源目录: 打包后位于 PyInstaller 的解压目录 (sys._MEIPASS)。"""
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS"), "static")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


app = Flask(__name__, static_folder=_static_dir(), static_url_path="/static")
# 关键: 关闭 jsonify 的 key 排序, 否则 profiles 的顺序会被按字母重排,
# 导致前端"排序"功能失效
app.json.sort_keys = False

# 全局单例 client: 所有协程调用都投递到唯一的后台事件循环线程串行执行
_client: Optional[McpClient] = None
_client_config: Optional[McpServerConfig] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_loop_lock = threading.Lock()

# 保存的连接配置 (含 headers 等凭据), 下次打开页面自动加载
# 打包后存到 exe 所在目录, 便于用户持久化配置
CONFIG_FILE = os.path.join(_frozen_base_dir(), "saved_config.json")

# 连接超时 (秒): 服务器不可达时快速失败并提示, 避免前端长时间无反馈
CONNECT_TIMEOUT = 20


def _get_loop() -> asyncio.AbstractEventLoop:
    """惰性启动一个常驻后台事件循环线程 (线程安全, 供多请求线程共享)。"""
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(
                target=_loop.run_forever, name="mcp-async-loop", daemon=True
            )
            _loop_thread.start()
        return _loop


def run_async(coro):
    """把协程投递到后台事件循环线程并同步等待结果 (Flask 是同步的)。"""
    return asyncio.run_coroutine_threadsafe(coro, _get_loop()).result()


def _safe_close(client: McpClient) -> None:
    """关闭客户端, 吞掉所有异常 (连接失败时 close 可能因异步清理再次抛错)。"""
    try:
        run_async(client.close())
    except Exception:  # noqa: BLE001
        pass


def _mark_disconnected() -> None:
    """连接已失效时清理全局状态 (避免 status 假报已连接)。"""
    global _client, _client_config
    if _client is not None:
        _safe_close(_client)
    _client = None
    _client_config = None


@app.before_request
def csrf_protect():
    """对状态修改请求做 CSRF 防护: 校验 Origin/Referer 来自本机。

    跨站请求 (恶意网页) 必然携带 Origin/Referer, 且与本机 host 不同;
    而 curl 等非浏览器客户端不带这两个头, 直接放行。
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")
    if origin is None and referer is None:
        return None
    allowed = (f"http://{request.host}", f"https://{request.host}")
    if origin and origin not in allowed:
        return jsonify({"ok": False, "error": "跨站请求被拒绝 (CSRF)"}), 403
    if referer and not any(referer.startswith(prefix) for prefix in allowed):
        return jsonify({"ok": False, "error": "跨站请求被拒绝 (CSRF)"}), 403
    return None


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def status():
    if _client is None or _client.session is None:
        return jsonify({"connected": False})
    try:
        alive = run_async(_client.ping(timeout=2.0))
    except Exception:  # noqa: BLE001
        alive = False
    if not alive:
        _mark_disconnected()
    return jsonify({"connected": alive})


@app.get("/api/current-config")
def current_config():
    """返回当前实际连接的配置 (用于页面刷新时恢复表单)。"""
    global _client_config
    if _client_config is None:
        return jsonify({"ok": False})
    cfg = {
        "command": _client_config.command,
        "args": _client_config.args,
        "url": _client_config.url,
        "transport": _client_config.transport,
        "headers": _client_config.headers,
    }
    cfg = {k: v for k, v in cfg.items() if v is not None}
    if _client_config.command:
        cfg["mode"] = "stdio"
    elif _client_config.url:
        cfg["mode"] = "http"
    return jsonify({"ok": True, "config": cfg})


def _load_profiles() -> dict:
    """读取配置存储, 兼容旧版单配置格式。返回 {active, profiles:{name: config}}。"""
    if not os.path.exists(CONFIG_FILE):
        return {"active": None, "profiles": {}}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return {"active": None, "profiles": {}}
    # 新版: {"active": ..., "profiles": {...}}
    if isinstance(data, dict) and "profiles" in data:
        return {
            "active": data.get("active"),
            "profiles": data.get("profiles") or {},
        }
    # 旧版: 直接是单个 config 对象, 迁移为 "default" profile
    return {"active": "default", "profiles": {"default": data}}


def _save_profiles(active: Optional[str], profiles: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"active": active, "profiles": profiles}, f, ensure_ascii=False, indent=2)


@app.get("/api/profiles")
def get_profiles():
    """返回所有已保存的 profile 及当前激活项。"""
    try:
        store = _load_profiles()
        return jsonify({"ok": True, "active": store["active"], "profiles": store["profiles"]})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)})


@app.post("/api/profiles")
def save_profile():
    """保存/更新一个 profile 并设为激活项。body: {name, config}。"""
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    config = data.get("config")
    if not name:
        return jsonify({"ok": False, "error": "缺少 profile 名称"}), 400
    if not isinstance(config, dict):
        return jsonify({"ok": False, "error": "config 必须是对象"}), 400
    try:
        store = _load_profiles()
        store["profiles"][name] = config
        _save_profiles(name, store["profiles"])
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.delete("/api/profiles/<name>")
def delete_profile(name: str):
    """删除指定 profile, 替补为删除项的下一个 (否则前一个)。"""
    try:
        store = _load_profiles()
        profiles = store["profiles"]
        if name not in profiles:
            return jsonify({"ok": False, "error": f"profile '{name}' 不存在"}), 404
        keys = list(profiles.keys())
        idx = keys.index(name)
        del profiles[name]
        active = store["active"]
        if active == name:
            remaining = list(profiles.keys())
            if remaining:
                # 删除项的下一个 (若删除的是末尾, 则取前一个)
                next_name = keys[idx + 1] if idx + 1 < len(keys) else (keys[idx - 1] if idx > 0 else None)
                active = next_name if next_name in profiles else remaining[0]
            else:
                active = None
        _save_profiles(active, profiles)
        return jsonify({"ok": True, "active": active})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/profiles/activate")
def activate_profile():
    """把指定 profile 设为激活项 (不修改配置内容)。body: {name}。"""
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "缺少 profile 名称"}), 400
    try:
        store = _load_profiles()
        if name not in store["profiles"]:
            return jsonify({"ok": False, "error": f"profile '{name}' 不存在"}), 404
        _save_profiles(name, store["profiles"])
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/profiles/rename")
def rename_profile():
    """重命名 profile, 保持顺序。body: {old, new}。"""
    data = request.get_json(force=True) or {}
    old = (data.get("old") or "").strip()
    new = (data.get("new") or "").strip()
    if not old or not new:
        return jsonify({"ok": False, "error": "缺少名称"}), 400
    try:
        store = _load_profiles()
        profiles = store["profiles"]
        if old not in profiles:
            return jsonify({"ok": False, "error": f"profile '{old}' 不存在"}), 404
        if new in profiles and new != old:
            return jsonify({"ok": False, "error": f"名称 '{new}' 已存在"}), 400
        new_profiles = {new if k == old else k: v for k, v in profiles.items()}
        active = new if store["active"] == old else store["active"]
        _save_profiles(active, new_profiles)
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/profiles/reorder")
def reorder_profiles():
    """按给定顺序重排 profile。body: {names: [...]}。"""
    data = request.get_json(force=True) or {}
    names = data.get("names") or []
    if not isinstance(names, list):
        return jsonify({"ok": False, "error": "names 必须是列表"}), 400
    try:
        store = _load_profiles()
        profiles = store["profiles"]
        new_profiles = {}
        for n in names:
            if n in profiles:
                new_profiles[n] = profiles[n]
        for k, v in profiles.items():
            if k not in new_profiles:
                new_profiles[k] = v
        _save_profiles(store["active"], new_profiles)
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/connect")
def connect():
    global _client, _client_config
    data = request.get_json(force=True) or {}

    if _client is not None:
        _safe_close(_client)
        _client = None
    _client_config = None  # 先清空, 连接成功后再设置, 避免失败后残留旧配置

    config = McpServerConfig(
        command=data.get("command"),
        args=data.get("args") or [],
        env=data.get("env") or {},
        url=data.get("url"),
        transport=data.get("transport") or "streamable_http",
        headers=data.get("headers") or {},
    )

    client = McpClient(config)
    try:
        run_async(asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT))
    except asyncio.TimeoutError:
        _safe_close(client)
        return jsonify({"ok": False, "error": f"连接超时 (>{CONNECT_TIMEOUT}s)，请检查服务器地址或网络"}), 400
    except concurrent.futures.CancelledError:
        # SDK 连接失败可能抛 CancelledError; run_coroutine_threadsafe 会将其
        # 转为 concurrent.futures.CancelledError (Exception 子类)
        _safe_close(client)
        return jsonify({"ok": False, "error": "连接失败 (服务器不可达或连接被中断)"}), 400
    except Exception as e:  # noqa: BLE001
        _safe_close(client)
        return jsonify({"ok": False, "error": str(e)}), 400

    _client = client
    _client_config = config
    return jsonify({"ok": True})


@app.post("/api/tools")
def list_tools():
    if _client is None or _client.session is None:
        return jsonify({"ok": False, "error": "未连接, 请先调用 /api/connect"}), 400
    try:
        tools = run_async(_client.list_tools())
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "tools": tools})


@app.post("/api/call")
def call_tool():
    if _client is None or _client.session is None:
        return jsonify({"ok": False, "error": "未连接"}), 400
    data = request.get_json(force=True) or {}
    name = data.get("name")
    arguments = data.get("arguments") or {}
    if not name:
        return jsonify({"ok": False, "error": "缺少 tool name"}), 400
    if not isinstance(arguments, dict):
        return jsonify({"ok": False, "error": "arguments 必须是对象"}), 400
    try:
        result = run_async(_client.call_tool(name, arguments))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "result": result})


def build_config_from_args(args: argparse.Namespace) -> Optional[McpServerConfig]:
    # argparse.REMAINDER 会把 `--` 分隔符本身也收进 args.args, 需剥掉再传给 server
    server_args = args.args[1:] if args.args and args.args[0] == "--" else args.args
    if args.command:
        return McpServerConfig(command=args.command, args=server_args, env=dict(args.env or []))
    if args.url:
        return McpServerConfig(url=args.url, transport=args.transport, headers=dict(args.headers or []))
    return None


def main() -> None:
    global _client, _client_config
    parser = argparse.ArgumentParser(description="MCP Web 服务器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--command", help="stdio: MCP server 启动命令")
    parser.add_argument("--url", help="HTTP/SSE: MCP endpoint URL")
    parser.add_argument("--transport", default="streamable_http", choices=["streamable_http", "sse"])
    parser.add_argument("--env", action="append", nargs=2, metavar=("KEY", "VALUE"))
    parser.add_argument("--header", action="append", nargs=2, metavar=("KEY", "VALUE"), dest="headers")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="-- 之后的 MCP server 启动参数")

    args = parser.parse_args()
    config = build_config_from_args(args)
    if config is not None:
        client = McpClient(config)
        try:
            run_async(asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT))
        except concurrent.futures.CancelledError:
            _safe_close(client)
            print("启动连接失败 (服务器不可达或连接被中断), 以未连接状态运行", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            _safe_close(client)
            print(f"启动连接失败 (以未连接状态运行): {e}", file=sys.stderr)
        else:
            _client = client
            _client_config = config
            print("启动时已连接 MCP server")
    else:
        print("启动时未指定 MCP 连接, 可通过页面或 POST /api/connect 配置")

    url = f"http://{args.host}:{args.port}"
    print(f"Web 前端: {url}")

    # 打包成 exe 时自动打开浏览器 (延迟 1.5s 等服务器就绪)
    if getattr(sys, "frozen", False):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
