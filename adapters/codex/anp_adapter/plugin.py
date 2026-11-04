"""Plugin 接入共性：`/anp/register` 与 `/anp/wake` 的服务端骨架。

Core 拉起插件后（`command` + `ANP_PLUGIN_PORT` 环境变量）会 POST `/anp/register`，
送来**身份 + 令牌 + 配置**；之后要唤醒它就 POST `/anp/wake`
（Plugin Spec §4.0 的"方向二"）。

这层只提供骨架：把两个端点的收包/拆包/回包做掉，业务留给Adapter。
★它不 import Core★——插件进程与 Core 之间只有 HTTP。
"""
from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .client import AnpClient
from .envelope import Envelope, from_wake_payload, DEFAULT_RESPONSE_MODE
from .transport import HttpWake, free_port


class RegisterResult:
    """Core 在 `/anp/register` 时送来的东西。"""

    def __init__(self, body: dict):
        body = body or {}
        self.identity = str(body.get("identity") or "")
        self.token = str(body.get("token") or "")
        cfg = body.get("config") or {}
        self.config = cfg if isinstance(cfg, dict) else {}
        self.raw = body

    @property
    def hub_base(self) -> str:
        """从 config 拿 hub 端口。拿不到退回默认 8900——插件自己起在 hub 旁时才用得上。"""
        return (f"http://127.0.0.1:{self.config.get('hub_port') or '8900'}")

    def client(self) -> AnpClient:
        """以**插件自己**的身份建一个 client。"""
        return AnpClient(self.hub_base, self.token, self.identity)


class PluginServer:
    """一个 Adapter Plugin 的最小服务端。

    用法：

        def on_register(result):        # result: RegisterResult
            result.client().bootstrap("demo-01", wake={"kind": "queue"})

        def on_wake(env, side):         # env: Envelope
            ...翻译 → 递平台 → 回收回信...

        srv = PluginServer(on_register=on_register, on_wake=on_wake)
        srv.start()      # 绑 ANP_PLUGIN_PORT，等 Core 来握手

    贡献（tools / events / panel）由 `contributions` 回给 Core；默认空表——
    一个纯 Adapter 通常不需要注册工具。
    """

    def __init__(self, on_register=None, on_wake=None, contributions: dict | None = None,
                 host: str = "127.0.0.1", health_provider=None,
                 platform_health_path: str = "/anp/platform-health"):
        self.on_register = on_register
        self.on_wake = on_wake
        self.contributions = {"tools": [], "events": [], "panel": "",
                              **(contributions or {})}
        self.host = host
        self.port = int(os.environ.get("ANP_PLUGIN_PORT") or "0")
        self.health_provider = health_provider
        self.platform_health_path = platform_health_path.rstrip("/") or "/anp/platform-health"
        self.register_seen: RegisterResult | None = None
        self.wakes: list[tuple[Envelope, dict]] = []
        self._server: ThreadingHTTPServer | None = None

    # ---- 唤醒面：插件自己的 /anp/wake ----
    def handle_wake(self, payload: dict, headers: dict) -> dict:
        env, side = from_wake_payload(payload, headers)
        self.wakes.append((env, side))
        if self.on_wake is not None:
            out = self.on_wake(env, side)
            # 用户回调忘了给 mode 也别让信裸奔：补上默认值
            if isinstance(out, dict) and "mode" not in out:
                out = {"mode": DEFAULT_RESPONSE_MODE, **out}
            return out if isinstance(out, dict) else {
                "ok": True, "mode": DEFAULT_RESPONSE_MODE}
        # ★必须带合法 mode，否则 Core 不签收★（v0.1.1 errata B1）
        return {"ok": True, "mode": DEFAULT_RESPONSE_MODE,
                "envelope_id": env.id}

    def start(self) -> int:
        if not self.port:
            raise RuntimeError("没有 ANP_PLUGIN_PORT：PluginServer 必须由 Core 拉起")
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                # ★响应头必须纯 ASCII★（中文箭头会让 send_header 抛
                # UnicodeEncodeError，handler 半路死掉）
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = {}
                path = self.path.rstrip("/")

                if path == "/anp/register":
                    res = RegisterResult(body)
                    server.register_seen = res
                    if server.on_register is not None:
                        try:
                            server.on_register(res)
                        except Exception as e:  # noqa: BLE001 - 回调炸了也要回包
                            # ★但必须留下痕迹★：原来只存进 _register_error 而从不对暴露，
                            # 结果"adapter 启动正常但代管的 agent 不存在"最难查的那种
                            # 半身不遂（#4.1 实测踩到，调了半小时）。
                            server._register_error = f"{type(e).__name__}: {e}"
                            server._register_error_at = time.time()
                    self._send(200, {"ok": True, **server.contributions})
                    return

                if path == "/anp/wake":
                    self._send(200, server.handle_wake(body, dict(self.headers)))
                    return

                self._send(404, {"ok": False, "error": f"未知端点 {path}"})

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.rstrip("/")
                if path in ("/anp/health", server.platform_health_path):
                    payload = {
                        "ok": True,
                        "identity": (server.register_seen.identity
                                     if server.register_seen else ""),
                        "wakes": len(server.wakes),
                        "port": server.port,
                        # ★把握手期留下的错误暴露出来★：adapter 自己起没起来是一回事，
                        # 代管的 agent 注册成功没有是另一回事，后者原来毫无线索。
                        "register_error": getattr(server, "_register_error", None),
                    }
                    if path == server.platform_health_path and server.health_provider:
                        try:
                            extra = server.health_provider()
                            if isinstance(extra, dict):
                                payload.update(extra)
                                payload["adapter_ok"] = True
                        except Exception as exc:  # noqa: BLE001
                            payload.update({
                                "ok": False,
                                "status": "platform_health_error",
                                "error": f"{type(exc).__name__}: {exc}",
                            })
                    self._send(200, payload)
                    return
                self._send(404, {"ok": False, "error": f"未知端点 {self.path}"})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        import threading
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    def serve_forever(self) -> None:
        """阻塞版：不起线程，直接占住当前线程。"""
        if not self.port:
            raise RuntimeError("没有 ANP_PLUGIN_PORT：PluginServer 必须由 Core 拉起")
        import threading   # noqa: F401 - 保持与 start() 一致的导入习惯
        self.start()
        try:
            while True:
                import time
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


__all__ = ["RegisterResult", "PluginServer", "HttpWake", "free_port"]
