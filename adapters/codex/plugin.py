"""Codex Adapter Plugin for AgentNetPort.

The plugin owns the Codex identity mapping and platform translation.  It does
not import ANP Core; all Core communication goes through the public SDK HTTP
surface.
"""
from __future__ import annotations

import hmac
import os
import sys
import threading
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _find_sdk() -> Path | None:
    home = os.environ.get("AGENT_ANP_HOME") or ""
    for candidate in (
        HERE / "anp_adapter",
        Path(home) / "sdk" / "anp_adapter" if home else None,
        HERE.parents[2] / "sdk" / "anp_adapter",
    ):
        if candidate is not None and (candidate / "__init__.py").is_file():
            return candidate.parent
    return None


SDK_ROOT = _find_sdk()
if SDK_ROOT is None:
    raise SystemExit("ANP Adapter SDK not found; vendor sdk/anp_adapter into this plugin")
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from anp_adapter import (  # noqa: E402
    AnpClient,
    DEFAULT_RESPONSE_MODE,
    Envelope,
    PluginServer,
)
from anp_adapter.client import post  # noqa: E402

# ★CR-1★：禁止 `from platform import ...`——会遮蔽 stdlib platform（DSH 同坑）。
import importlib.util as _ilu  # noqa: E402


def _load_codex_platform():
    spec = _ilu.spec_from_file_location("codex_platform", HERE / "platform.py")
    if spec is None or spec.loader is None:  # pragma: no cover
        raise SystemExit("platform.py (Codex platform layer) not found")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CodexPlatform = _load_codex_platform().CodexPlatform


PLUGIN_ID = os.environ.get("ANP_PLUGIN_ID") or "anp.adapter.codex"
MANAGED_AGENT = os.environ.get("ANP_CODEX_AGENT") or "codex"


class CodexAdapter:
    def __init__(self) -> None:
        self.platform = CodexPlatform(MANAGED_AGENT)
        self.plugin_client: AnpClient | None = None
        self.agent_key = self.platform.agent_key()
        self.register_state: dict[str, Any] = {}
        self._lock = threading.RLock()

    def _wake_declaration(self) -> dict[str, str]:
        port = os.environ.get("ANP_PLUGIN_PORT") or "0"
        return {"kind": "http", "url": f"http://127.0.0.1:{port}/anp/wake"}

    def _register_agent(self, client: AnpClient) -> dict[str, Any]:
        declaration = {
            "wake": self._wake_declaration(),
            "caps": {"inject_session": False, "spawn_cwd": True,
                      "resume_session": True, "queue": False},
            "skills": ["coding", "review", "shell", "workspace"],
            "process_api": {"kind": "http",
                             "url": f"http://127.0.0.1:{os.environ.get('ANP_PLUGIN_PORT') or '0'}/anp/health"},
            "description": "Codex CLI Adapter（ANP）",
            "workspace": self.platform.workspace,
            "client": "codex",
        }
        if self.agent_key:
            result = client.heartbeat(MANAGED_AGENT, token=self.agent_key, **declaration)
        else:
            result = client.bootstrap(MANAGED_AGENT, **declaration)
        key = str(result.get("key") or self.agent_key)
        if result.get("ok") and key:
            with self._lock:
                self.agent_key = key
                self.platform.save_credentials(key)
        self.register_state = {"ok": bool(result.get("ok")),
                               "auth": result.get("auth"),
                               "agent": MANAGED_AGENT}
        if result.get("ok"):
            self._post_roster()
        return result

    def on_register(self, result: Any) -> None:
        """Core → Plugin 握手。★H-2★：立刻回包，代管注册丢后台线程（DSH 同款）。

        同步调 /heartbeat + /session/roster 会拖穿 Core register_out 的 10s 预算。
        """
        self.plugin_client = result.client()
        threading.Thread(target=self._register_later, name="anp-codex-register",
                         daemon=True).start()

    def _register_later(self) -> None:
        try:
            with self._lock:
                self._register_agent(self.plugin_client)
        except Exception as exc:  # PluginServer exposes the error via /anp/health.
            self.register_state = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.platform.log(f"registration failed: {self.register_state['error']}")

    def _agent_client(self) -> AnpClient | None:
        if self.plugin_client is None or not self.agent_key:
            return None
        return self.plugin_client.as_identity(self.agent_key, MANAGED_AGENT)

    def _post_roster(self) -> dict[str, Any]:
        client = self._agent_client()
        if client is None:
            return {"ok": False, "error": "managed agent is not registered"}
        _, response = post(client.base, "/session/roster", {
            "adapter": MANAGED_AGENT,
            "workspace": self.platform.workspace,
            "sessions": self.platform.session_roster(),
        }, client.token)
        return response

    def _bind_context(self, context_id: str, session_id: str, workspace: str) -> dict[str, Any]:
        client = self._agent_client()
        if client is None or not context_id or not session_id:
            return {"ok": False, "error": "missing binding information"}
        _, response = post(client.base, "/session/bind", {
            "adapter": MANAGED_AGENT,
            "context_id": context_id,
            "session_id": session_id,
            "workspace": workspace,
        }, client.token)
        return response

    def _authorized(self, side: dict[str, Any]) -> bool:
        supplied = str(side.get("agent_key") or "")
        with self._lock:
            expected = self.agent_key
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def route(self, envelope: Envelope) -> dict[str, Any]:
        return self.platform.route_from_stamp(envelope)

    def on_wake(self, envelope: Envelope, side: dict[str, Any]) -> dict[str, Any]:
        """签收门（Errata S3）：先鉴权与持久化，再 ok:true；失败不得签收。"""
        if not self._authorized(side):
            return {"ok": False, "error": "wake key is not the managed Agent token",
                    "mode": DEFAULT_RESPONSE_MODE}
        if not envelope.is_complete:
            return {"ok": False, "error": f"incomplete envelope: {envelope.missing()}",
                    "mode": DEFAULT_RESPONSE_MODE}
        route = self.route(envelope)
        native = self.platform.to_native(envelope)
        # ★S3-A★：待办先落盘，可追；写不进去就不能声称"已接受"
        try:
            pending_id, is_new = self.platform.enqueue_work(envelope, native, route)
        except Exception as exc:  # noqa: BLE001
            self.platform.log(f"enqueue failed for {envelope.id}: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"enqueue failed: {type(exc).__name__}: {exc}",
                    "mode": DEFAULT_RESPONSE_MODE}
        # ★H-6 真幂等★：已在队/已在跑 → 不二次拉起平台，直接承认本次 wake
        if not is_new:
            return {"ok": True, "mode": DEFAULT_RESPONSE_MODE,
                    "envelope_id": envelope.id, "route": route,
                    "accepted": True, "pending_id": pending_id, "deduped": True}
        if not self.platform.should_reply(envelope):
            self.platform.finish_work(pending_id, ok=True, detail="queued-as-notice")
            return {"ok": True, "mode": "batched",
                    "envelope_id": envelope.id, "route": route,
                    "accepted": True, "pending_id": pending_id,
                    "need_reply": False}
        thread = threading.Thread(target=self._run_one,
                                   args=(envelope, native, route, pending_id),
                                   name=f"anp-codex-{envelope.id}", daemon=True)
        thread.start()
        return {"ok": True, "mode": DEFAULT_RESPONSE_MODE,
                "envelope_id": envelope.id, "route": route,
                "accepted": True, "pending_id": pending_id}

    def _run_one(self, envelope: Envelope, native: dict[str, Any],
                 route: dict[str, Any], pending_id: str = "") -> None:
        """异步执行（S3-C）：失败必须补偿，禁止静默丢弃。"""
        session_id = ""
        try:
            result = self.platform.run(native, route)
            session_id = str(result.get("session_id") or route.get("session_id") or "")
            if session_id:
                self._post_roster()
                context_id = str(native.get("context_id") or "")
                if context_id and route.get("mode") == "new":
                    self._bind_context(context_id, session_id, route["workspace"])
                    self._post_roster()
            reply_raw = str(result.get("reply") or "").strip()
            reply = str(self.platform.from_native({"text": reply_raw}).get("body") or "").strip()
            ok_run = bool(result.get("ok"))
            must_reply = self.platform.should_reply(envelope)
            # ★S4★：need_reply=true 才要求义务回信；缺省 = 知会
            if ok_run and reply and must_reply:
                self._send_reply(envelope, reply, session_id, route)
            elif ok_run and must_reply and not reply:
                # ★P1 空产出补偿★：平台成功但无 agent_message → 不得静默断线
                self._compensate_failure(
                    envelope, route, session_id,
                    "platform produced empty agent_message")
                if pending_id:
                    self.platform.finish_work(
                        pending_id, ok=False, detail="done_empty")
                return
            if pending_id:
                self.platform.finish_work(pending_id, ok=ok_run,
                                          detail=str(result.get("returncode") or ""))
            if not ok_run:
                # ★S3-C 补偿★：平台失败 → 失败回信（need_reply 或已签收需告知）
                self._compensate_failure(envelope, route, session_id,
                                         str(result.get("stderr") or result.get("error") or "platform failed"))
        except Exception as exc:  # noqa: BLE001
            self.platform.log(f"worker failed for {envelope.id}: {type(exc).__name__}: {exc}")
            if pending_id:
                self.platform.finish_work(pending_id, ok=False,
                                          detail=f"{type(exc).__name__}: {exc}")
            self._compensate_failure(envelope, route, session_id,
                                     f"{type(exc).__name__}: {exc}")

    def _send_reply(self, envelope: Envelope, body: str, session_id: str,
                    route: dict[str, Any]) -> None:
        client = self._agent_client()
        if client is None:
            raise RuntimeError("managed Agent client is not ready")
        response = client.send(
            envelope.from_, body, reply_to=envelope.id,
            correlation_id=envelope.correlation_id, kind="reply",
            meta=self.platform.reply_meta(envelope, session_id,
                                          route.get("workspace") or ""), wake=True)
        if not response.get("ok"):
            raise RuntimeError(f"reply persistence failed: {response}")

    def _compensate_failure(self, envelope: Envelope, route: dict[str, Any],
                            session_id: str, error: str) -> None:
        """S3-C：失败补偿——向发信方发失败回信（带对话线坐标）。"""
        if not envelope.from_:
            return
        note = (
            f"[Codex Adapter] 平台执行失败，本封未能完成。\n"
            f"error: {error}\n"
            f"envelope: {envelope.id}"
        )
        try:
            self._send_reply(envelope, note, session_id, route)
        except Exception as exc:  # noqa: BLE001
            self.platform.log(f"compensation reply failed: {type(exc).__name__}: {exc}")

    def resolve(self, target: str, **kwargs: Any) -> dict[str, Any]:
        client = self._agent_client()
        if client is None:
            return {"status": "DIRECTORY_UNAVAILABLE", "query": target,
                    "reason": "managed Agent is not registered"}
        return client.resolve(target, **kwargs)


def main() -> int:
    if not os.environ.get("ANP_PLUGIN_PORT"):
        raise SystemExit("ANP_PLUGIN_PORT is required; run this plugin through ANP Core")
    adapter = CodexAdapter()
    PluginServer(on_register=adapter.on_register,
                 on_wake=adapter.on_wake).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
