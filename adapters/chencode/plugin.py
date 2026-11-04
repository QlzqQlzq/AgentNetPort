"""ChenCode Adapter Plugin（ANP 侧）。

★这一层不 import 任何 Core 模块★：与 ANP 之间只有 SDK + 公开 HTTP API。
ChenCode 平台语义全在 `platform.py`。

结构对应 Adapter Spec §1 四条职责：
  ① 托管身份 —— 代 `chencode` 这个外部 agent 注册，两枚令牌分开（P0-1）
  ② 实现唤醒 —— wake=http 指向本插件 /anp/wake；Core 打进来时接住
  ③ 语义翻译 —— to_native / from_native
  ④ 回收回信 —— 平台输出包成 ANP 信封走 POST /messages/send（B4 禁 stdout）

Errata 对齐：
  B1  wake 响应必须 ok:true + 合法 mode
  B4  回信只走 /messages/send，带 reply_to + correlation_id
  S1  只执行 Core 的 meta.session 章；/session/roster + /session/bind 为通道扩展面
  S2  ⑦a 凭证仍在则续约；⑦b 凭证全丢预期 401，不假装 bootstrap
  S3  签收前先持久化；异步失败必须补偿
  S4  need_reply===true 才义务回信；缺省=知会
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
    """★部署形态必须自带 SDK★：Core 从 `<home>/plugins/<id>/` 拉起插件。"""
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
    raise SystemExit(
        "找不到 ANP Adapter SDK：必须把 sdk/anp_adapter/ vendor 进插件目录，"
        "或装到 AGENT_ANP_HOME/sdk/ 下")
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from anp_adapter import (  # noqa: E402
    AnpClient,
    DEFAULT_RESPONSE_MODE,
    Envelope,
    PluginServer,
)
from anp_adapter.client import post  # noqa: E402

# ★CR-1★：禁止 `from platform import ...`——会遮蔽 stdlib platform。
import importlib.util as _ilu  # noqa: E402


def _load_chencode_platform():
    spec = _ilu.spec_from_file_location("chencode_platform", HERE / "platform.py")
    if spec is None or spec.loader is None:  # pragma: no cover
        raise SystemExit("platform.py (ChenCode platform layer) not found")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ChenCodePlatform = _load_chencode_platform().ChenCodePlatform

PLUGIN_ID = os.environ.get("ANP_PLUGIN_ID") or "anp.adapter.chencode"
MANAGED_AGENT = os.environ.get("ANP_CHENCODE_AGENT") or "chencode"


class ChenCodeAdapter:
    def __init__(self) -> None:
        self.platform = ChenCodePlatform(MANAGED_AGENT)
        self.plugin_client: AnpClient | None = None
        # ★S2-⑦a★：凭证仍在则续约；全丢则走 401，不假装 bootstrap
        self.agent_key = self.platform.agent_key()
        self.register_state: dict[str, Any] = {}
        self.tier: dict[str, Any] | None = None
        self._lock = threading.RLock()

    def tier_now(self) -> dict[str, Any]:
        if self.tier is None:
            self.tier = self.platform.detect_tier()
        return self.tier

    def _wake_declaration(self) -> dict[str, str]:
        """★代管的 agent 的 wake 指向本插件自己的 /anp/wake★。

        不用 exec：插件装在 <home>/plugins/，Core 的 install_roots 信任校验会拒。
        """
        port = os.environ.get("ANP_PLUGIN_PORT") or "0"
        return {"kind": "http", "url": f"http://127.0.0.1:{port}/anp/wake"}

    def _register_agent(self, client: AnpClient) -> dict[str, Any]:
        wake = self._wake_declaration()
        declaration = {
            "wake": wake,
            "caps": self.platform.caps_for(self.tier_now()["tier"]),
            "skills": ["coding", "review", "shell", "workspace", "desktop"],
            # 平台健康面作为可选 liveness 提示（不是插件自己的 /anp/health）。
            # WebUI 的绿色“在线”仍以最近 heartbeat lease 为准；平台探针失败
            # 不能抹掉新鲜心跳，只影响可唤醒/可达性提示。
            "process_api": self.platform.process_api_config(),
            "description": "ChenCode Desktop/CLI Adapter（ANP）",
            "workspace": self.platform.workspace,
            "client": "chencode",
        }
        if self.agent_key:
            # ★S2-⑦a★：有本地凭证 → 续约
            result = client.heartbeat(MANAGED_AGENT, token=self.agent_key, **declaration)
        else:
            # ★S2-⑦b★：凭证全丢时这里会走 bootstrap；若身份已存在 Core 应 401
            #   （P0-2），我们不重试换名、不静默换身份。
            result = client.bootstrap(MANAGED_AGENT, **declaration)
        key = str(result.get("key") or self.agent_key)
        if result.get("ok") and key:
            with self._lock:
                self.agent_key = key
                self.platform.save_credentials(key)
        self.register_state = {
            "ok": bool(result.get("ok")),
            "auth": result.get("auth"),
            "agent": MANAGED_AGENT,
            "error": result.get("error"),
        }
        if result.get("ok"):
            self._post_roster()
        else:
            self.platform.log(f"register failed: {result}")
        return result

    def on_register(self, result: Any) -> None:
        """Core → Plugin 握手。★H-2★：立刻回包，代管注册丢后台线程。"""
        self.plugin_client = result.client()
        threading.Thread(target=self._register_later, name="anp-chencode-register",
                         daemon=True).start()

    def _register_later(self) -> None:
        try:
            with self._lock:
                self._register_agent(self.plugin_client)
        except Exception as exc:  # noqa: BLE001
            self.register_state = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.platform.log(f"registration failed: {self.register_state['error']}")

    def _agent_client(self) -> AnpClient | None:
        if self.plugin_client is None or not self.agent_key:
            return None
        return self.plugin_client.as_identity(self.agent_key, MANAGED_AGENT)

    # ---- S1 Layer B：会话名册 / 绑定 ----
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

    def _bind_context(self, context_id: str, session_id: str,
                      workspace: str) -> dict[str, Any]:
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
        """P0-4：x-mailbox-key 必须是目标 Agent 的 token。"""
        supplied = str(side.get("agent_key") or "")
        with self._lock:
            expected = self.agent_key
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def route(self, envelope: Envelope) -> dict[str, Any]:
        return self.platform.route_from_stamp(envelope)

    def on_wake(self, envelope: Envelope, side: dict[str, Any]) -> dict[str, Any]:
        """签收门（B1 + S3）：先鉴权与持久化，再 ok:true；失败不得签收。"""
        if not self._authorized(side):
            return {"ok": False, "error": "wake key is not the managed Agent token",
                    "mode": DEFAULT_RESPONSE_MODE}
        if not envelope.is_complete:
            return {"ok": False, "error": f"incomplete envelope: {envelope.missing()}",
                    "mode": DEFAULT_RESPONSE_MODE}
        route = self.route(envelope)
        native = self.platform.to_native(envelope)
        # ★S3-A★：待办先落盘；写不进去就不能声称"已接受"
        try:
            pending_id = self.platform.enqueue_work(envelope, native, route)
        except Exception as exc:  # noqa: BLE001
            self.platform.log(f"enqueue failed: {type(exc).__name__}: {exc}")
            return {"ok": False,
                    "error": f"enqueue failed: {type(exc).__name__}: {exc}",
                    "mode": DEFAULT_RESPONSE_MODE}

        tier = self.tier_now()
        messages = self._peek_unread()
        try:
            self.platform.write_pending(messages, tier, envelope.raw)
        except Exception as exc:  # noqa: BLE001
            # M-6：摘要写失败不能假装完整接受——降级为不签收，让 Core 可重投
            self.platform.log(f"write_pending failed: {exc}")
            self.platform.finish_work(
                pending_id, ok=False, detail=f"write_pending failed: {exc}")
            return {"ok": False, "error": f"write_pending failed: {exc}",
                    "mode": DEFAULT_RESPONSE_MODE}

        # ★S4★：need_reply 缺省=知会，不默认烧完整 headless 会话
        must_reply = self.platform.should_reply(envelope)
        force_run = self.platform.flag_on("AGENT_ANP_CHEN_ALWAYS_RUN")
        # A 档只有桌面/健康探针可达；当前 Adapter 没有把信注入既有桌面会话，
        # 所以只能落 pending，交给会话侧后续处理。只有 B 档同时具备
        # bun + bootstrap-entry.ts 时，才允许在这里启动 headless 回合。
        headless_ready = (
            tier["tier"] == "B"
            and bool(tier.get("cli"))
            and bool(tier.get("entry"))
        )
        # 仅测试 stub 允许绕过真实平台档位，方便在无桌面会话的验收环境
        # 验证回信封装；真实平台仍严格遵守 A/B/C 档能力。
        stub_reply = bool(self.platform.flag("AGENT_ANP_CHEN_STUB_REPLY").strip())
        headless_ready = headless_ready or stub_reply
        if (must_reply or force_run) and headless_ready:
            thread = threading.Thread(
                target=self._run_one,
                args=(envelope, native, route, pending_id, must_reply or force_run),
                name=f"anp-chencode-{envelope.id}", daemon=True)
            thread.start()
            # M-5：B 档立即进入执行轨道用 immediate；否则 followup（挂到后续回合）
            mode = "immediate" if tier["tier"] == "B" else "followup"
            return {"ok": True, "mode": mode,
                    "envelope_id": envelope.id, "route": route,
                    "accepted": True, "pending_id": pending_id,
                    "tier": tier["tier"], "need_reply": must_reply,
                    "force_run": force_run}
        if must_reply or force_run:
            reason = (
                f"ChenCode {tier['tier']} 档没有可自动执行的 headless 会话；"
                "请在 ChenCode 会话侧处理待取信"
            )
            if must_reply or force_run:
                self._compensate_failure(envelope, route, "", reason)
            try:
                self.platform.finish_work(
                    pending_id, ok=False, detail="queued-for-session")
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True,
                    "mode": "followup" if tier["tier"] == "A" else "batched",
                    "envelope_id": envelope.id, "route": route,
                    "accepted": True, "pending_id": pending_id,
                    "tier": tier["tier"], "need_reply": must_reply,
                    "force_run": force_run, "deferred": True,
                    "reason": "platform session required"}
        # 知会：已入队 + 落摘要，等会话侧处理（batched：进本端批处理/待取信队列）
        try:
            self.platform.finish_work(pending_id, ok=True, detail="queued-as-notice")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "mode": "batched",
                "envelope_id": envelope.id, "route": route,
                "accepted": True, "pending_id": pending_id,
                "tier": tier["tier"], "need_reply": False}

    def _run_one(self, envelope: Envelope, native: dict[str, Any],
                 route: dict[str, Any], pending_id: str = "",
                 send_reply: bool = False) -> None:
        """异步执行（S3-C）：失败必须补偿，产出不得静默丢弃。

        send_reply：H-1——ALWAYS_RUN 强制跑时，即使 need_reply 缺省，
        只要有平台产出就必须回信（或明确记录 reply-suppressed）。
        """
        session_id = ""
        try:
            result = self.platform.run(native, route)
            session_id = str(result.get("session_id") or route.get("session_id") or "")
            if session_id:
                self._post_roster()
                context_id = str(native.get("context_id") or "")
                if context_id and route.get("mode") == "new":
                    self._bind_context(context_id, session_id, route.get("workspace") or "")
                    self._post_roster()
            reply = str(result.get("reply") or "").strip()
            ok_run = bool(result.get("ok"))
            # H-1：send_reply 覆盖「强制执行也必须回收产出」
            should_send = send_reply or self.platform.should_reply(envelope)
            if ok_run and reply and should_send:
                self._send_reply(envelope, reply, session_id, route)
                if pending_id:
                    self.platform.finish_work(pending_id, ok=True, detail="replied")
            elif ok_run and reply and not should_send:
                # 显式丢弃可观测（S3 精神：禁止静默）
                if pending_id:
                    self.platform.finish_work(
                        pending_id, ok=True, detail="reply-suppressed")
            elif pending_id:
                self.platform.finish_work(
                    pending_id, ok=ok_run,
                    detail=str(result.get("returncode") or result.get("subtype") or ""))
            if not ok_run:
                self._compensate_failure(
                    envelope, route, session_id,
                    str(result.get("error") or result.get("stderr")
                        or result.get("subtype") or "platform failed"))
        except Exception as exc:  # noqa: BLE001
            self.platform.log(f"worker failed for {envelope.id}: {type(exc).__name__}: {exc}")
            if pending_id:
                self.platform.finish_work(pending_id, ok=False,
                                          detail=f"{type(exc).__name__}: {exc}")
            self._compensate_failure(envelope, route, session_id,
                                     f"{type(exc).__name__}: {exc}")

    def _send_reply(self, envelope: Envelope, body: str, session_id: str,
                    route: dict[str, Any]) -> None:
        """B4：回信唯一合法出口是 POST /messages/send。"""
        client = self._agent_client()
        if client is None:
            raise RuntimeError("managed Agent client is not ready")
        response = client.send(
            envelope.from_, body, reply_to=envelope.id,
            correlation_id=envelope.correlation_id, kind="reply",
            meta=self.platform.reply_meta(envelope, session_id,
                                          route.get("workspace") or ""),
            wake=True)
        if not response.get("ok"):
            raise RuntimeError(f"reply persistence failed: {response}")

    def _compensate_failure(self, envelope: Envelope, route: dict[str, Any],
                            session_id: str, error: str) -> None:
        """S3-C：失败补偿——向发信方发失败回信（带对话线坐标）。"""
        if not envelope.from_:
            return
        note = (
            f"[ChenCode Adapter] 平台执行失败，本封未能完成。\n"
            f"error: {error}\n"
            f"envelope: {envelope.id}"
        )
        try:
            self._send_reply(envelope, note, session_id, route)
        except Exception as exc:  # noqa: BLE001
            self.platform.log(f"compensation reply failed: {type(exc).__name__}: {exc}")

    def _peek_unread(self) -> list[dict[str, Any]]:
        client = self._agent_client()
        if client is None:
            return []
        return list(client.receive(peek=True, unread_only=True, limit=20)
                    .get("messages") or [])

    def resolve(self, target: str, **kwargs: Any) -> dict[str, Any]:
        """可选模糊寻址（Spec §8.5）：只用公开名册 + 本地提示。"""
        client = self._agent_client()
        if client is None:
            return {"status": "DIRECTORY_UNAVAILABLE", "query": target,
                    "reason": "managed Agent is not registered"}
        return client.resolve(target, **kwargs)


def main() -> int:
    if not os.environ.get("ANP_PLUGIN_PORT"):
        raise SystemExit("没有 ANP_PLUGIN_PORT：本插件必须由 Core 拉起，不要手工跑")
    adapter = ChenCodeAdapter()
    # ★先绑端口，再做任何慢活★（Core 的 10s register 预算）
    PluginServer(on_register=adapter.on_register,
                 on_wake=adapter.on_wake).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
