"""MiMo Adapter Plugin（ANP 侧）。

★这一层不 import 任何 Core 模块★：与 ANP 之间只有 SDK + 公开 HTTP API。
MiMo 平台语义全在 `platform.py`。

结构对应当初定下的三问三答：

    ANP 信封 ──to_native()──▶ MiMo 原生格式 ──▶ 平台（serve / CLI / 落盘）
    MiMo 回信 ──from_native()──▶ ANP 发信字段 ──▶ /messages/send

职责（Adapter Spec §1 那四条）：
  ① 托管身份 —— 代 `mimo` 这个外部 agent 注册，两枚令牌分开
  ② 实现唤醒 —— 按档位决定 wake 声明；Core 打 /anp/wake 时接住
  ③ 语义翻译 —— to_native / from_native
  ④ 回收回信 —— 把平台产出包成信封发回
"""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


def _find_sdk() -> Path | None:
    """★部署形态必须自带 SDK★（#3 抓到的坑）：Core 从 `<home>/plugins/<id>/` 拉起插件，
    仓库根的 ../sdk 不在路径里。按 vendor → ANP home → 仓库三级找。"""
    home = os.environ.get("AGENT_ANP_HOME") or ""
    for cand in (_HERE / "anp_adapter",
                 Path(home) / "sdk" / "anp_adapter" if home else None,
                 _HERE.parents[2] / "sdk" / "anp_adapter"):
        if cand is not None and (cand / "__init__.py").is_file():
            return cand.parent
    return None


_sdk = _find_sdk()
if _sdk is None:
    raise SystemExit("找不到 ANP Adapter SDK：必须把 sdk/anp_adapter/ vendor 进插件目录，"
                     "或装到 AGENT_ANP_HOME/sdk/ 下")
if str(_sdk) not in sys.path:
    sys.path.insert(0, str(_sdk))

from anp_adapter import (AnpClient, Envelope, PluginServer,        # noqa: E402
                        DEFAULT_RESPONSE_MODE)
from anp_adapter.errors import AnpError                     # noqa: E402

# ★CR-1★：禁止 `from platform import ...`——会遮蔽 stdlib platform。
import importlib.util as _ilu  # noqa: E402


def _load_mimo_platform():
    spec = _ilu.spec_from_file_location("mimo_platform", _HERE / "platform.py")
    if spec is None or spec.loader is None:  # pragma: no cover
        raise SystemExit("platform.py (MiMo platform layer) not found")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MiMoPlatform = _load_mimo_platform().MiMoPlatform

MANAGED_AGENT = "mimo"        # 被代管的外部 agent 身份（旧账本里就叫 mimo）
ADAPTER_ID = "anp.adapter.mimo"


class MimoAdapter:
    def __init__(self) -> None:
        self.platform = MiMoPlatform(MANAGED_AGENT)
        self.client: AnpClient | None = None     # 插件自己那枚
        self.agent_key = self.platform.agent_key()  # 代管的 mimo 那枚（S2-⑦a：从 data/ 恢复）
        # ★档位延迟探测★：`detect_tier()` 要打网络（probe_serve 三条路径、各 1.2s 超时），
        # 在构造时跑会把启动拖过 Core 的 10 秒 register 预算（#4.1 实测踩到：
        # load_error=endpoint.unreachable）。所以构造只做廉价的事，
        # 档位等 /anp/register 来了再探——那时端口已经绑好，Core 不会超时。
        self.tier: dict | None = None
        self.register_state: dict = {}

    def tier_now(self) -> dict:
        if self.tier is None:
            self.tier = self.platform.detect_tier()
        return self.tier

    # ---- ① 托管身份：用公开 /heartbeat 代 mimo 注册 ----
    def register_managed(self, client: AnpClient, retry: int = 20) -> dict:
        """★P0-1/P0-10★：首次 bootstrap（不带 key）拿 agent 令牌；续约必须带 agent 自己那枚。

        以前误传 `token=client.token`（插件令牌）：首次碰巧能 bootstrap，
        但插件重载后 "mimo" 已知 → 拿插件令牌续约被 P0-2 的 401 挡死。
        """
        wake = self._wake_declaration()   # 内部会触发 tier_now()
        last: dict = {}
        for i in range(retry):
            try:
                if self.agent_key:
                    r = client.heartbeat(
                        MANAGED_AGENT, token=self.agent_key, wake=wake,
                        process_api=self.platform.process_api_config(),
                        description="MiMo Desktop（ANP；无常驻端点/无 CLI 时走 C 档 queue+poll）",
                        client="mimo", caps=self.platform.caps_for(self.tier_now()["tier"]),
                        tier=self.tier_now(), workspace=self.platform.workspace)
                else:
                    r = client.bootstrap(
                        MANAGED_AGENT, wake=wake,
                        process_api=self.platform.process_api_config(),
                        description="MiMo Desktop（ANP；无常驻端点/无 CLI 时走 C 档 queue+poll）",
                        client="mimo", caps=self.platform.caps_for(self.tier_now()["tier"]),
                        tier=self.tier_now(), workspace=self.platform.workspace)
            except AnpError as e:
                last = {"ok": False, "error": str(e), "kind": e.kind}
                self.platform.log(f"注册 mimo 失败（{e.kind}）：{e}")
                time_sleep(0.5)
                continue
            last = r
            if r.get("ok"):
                self.agent_key = str(r.get("key") or self.agent_key)
                self.platform.save_credentials(self.agent_key)
                self.register_state = {"ok": True, "attempt": i + 1,
                                       "agent_key": self.agent_key, "wake": wake}
                return r
            time_sleep(0.5)
        self.register_state = {"ok": False, "last": last}
        return last

    def _wake_declaration(self) -> dict:
        """代管的 agent 用什么端点被唤醒。

        ★#4.1 最有价值的一条修正★：我最初的写法照抄旧 Mimo 的
        `exec + 独立子进程`。但旧 Mimo 那么写是因为它**不是插件**——一个独立脚本
        没有可寻址端点，只能让 Core 拉起它。现在它是 Adapter Plugin，`PluginServer`
        本来就在 `ANP_PLUGIN_PORT` 上听着 `/anp/wake`，所以代管的 agent 直接指向
        **插件自己的 http 端点**就好。

        这么做同时绕开一个 Core 层的真问题：`check_wake_config` 要求 exec 脚本必须
        落在信箱**安装目录**内（`install_roots()`），而插件按设计装在
        `<home>/plugins/<id>/`，真实部署下永远在安装目录外 → exec 唤醒一律被拒
        （已作为 Core 缺口记在 verify.py 的 GAPS 里，不影响这条 http 路径）。

        档位决定的是"信封到了之后怎么进平台"，不是"Core 怎么找到我们"——两件事分开。
        """
        port = os.environ.get("ANP_PLUGIN_PORT") or "0"
        return {"kind": "http", "url": f"http://127.0.0.1:{port}/anp/wake"}

    # ---- ② Core → 插件的握手 ----
    def on_register(self, res) -> None:
        """★H-2★：立刻回包，代管注册丢后台线程（DSH/Codex 同款）。"""
        self.client = res.client()
        threading.Thread(target=self._register_later, name="anp-mimo-register",
                         daemon=True).start()

    def _register_later(self) -> None:
        try:
            self.register_managed(self.client)
        except Exception as exc:  # noqa: BLE001
            self.register_state = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.platform.log(f"registration failed: {self.register_state['error']}")

    # ---- ③④ 唤醒入口：翻译 → 递平台 → 回收回信 ----
    def on_wake(self, env: Envelope, side: dict) -> dict:
        """P0-4 + S3：鉴权默认拒绝；先持久化再签收；失败补偿。"""
        supplied = str(side.get("agent_key") or "")
        if not self.agent_key or supplied != self.agent_key:
            return {"ok": False, "error": "wake key is not the managed Agent token",
                    "mode": DEFAULT_RESPONSE_MODE}
        if not env.is_complete:
            return {"ok": False, "error": f"incomplete envelope: {env.missing()}",
                    "mode": DEFAULT_RESPONSE_MODE}
        native = self.to_native(env)
        try:
            pending_id, is_new = self.platform.enqueue_work(env, native)
        except Exception as exc:  # noqa: BLE001
            self.platform.log(f"enqueue failed: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"enqueue failed: {type(exc).__name__}: {exc}",
                    "mode": DEFAULT_RESPONSE_MODE}
        if not is_new:
            return {"ok": True, "mode": "batched",
                    "envelope_id": env.id, "pending_id": pending_id,
                    "deduped": True}
        route = self.platform.route_from_stamp(env)
        must_reply = self.platform.should_reply(env)
        try:
            messages = self._peek_unread()
            if must_reply:
                outcome = self._deliver_to_platform(native, messages, env.raw)
            else:
                outcome = {"ok": True, "tier": "C", "queued": len(messages),
                           "deferred": True, "reason": "notice-only"}
            if not outcome.get("ok"):
                raise RuntimeError(str(outcome.get("error") or "platform delivery failed"))
            self.platform.write_pending(messages, self.tier_now(), env.raw)
            if must_reply and outcome.get("deferred") and outcome.get("session_id"):
                # The HTTP wake only accepted an asynchronous turn.  Record
                # that non-terminal state before starting the watcher so a
                # very fast Desktop reply cannot be followed by a stale
                # ``watching`` row.
                self.platform.mark_work(
                    pending_id, state="watching", ok=True, detail="accepted")
                threading.Thread(
                    target=self._watch_desktop_reply,
                    args=(pending_id, messages, env, outcome),
                    name=f"anp-mimo-reply-{env.id}",
                    daemon=True,
                ).start()
                reply = {"ok": True, "deferred": True, "watching": True}
            else:
                reply = self._reap(messages, env, outcome) if must_reply else {
                    "ok": True, "deferred": True}
            work_ok = bool(outcome.get("ok")) and bool(reply.get("ok", True))
            if not (must_reply and outcome.get("deferred") and
                    outcome.get("session_id")):
                if work_ok and outcome.get("deferred") and must_reply:
                    # Queue-only delivery accepted the work but has no
                    # assistant reply to mark as complete yet.
                    self.platform.mark_work(
                        pending_id, state="accepted", ok=True,
                        detail="queued-for-session")
                else:
                    self.platform.finish_work(
                        pending_id, ok=work_ok,
                        detail=("replied" if reply.get("sent") else
                                "processed"))
        except Exception as exc:  # noqa: BLE001
            self.platform.finish_work(pending_id, ok=False, detail=f"{type(exc).__name__}: {exc}")
            self._compensate_failure(env, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "mode": DEFAULT_RESPONSE_MODE,
                    "error": f"{type(exc).__name__}: {exc}", "envelope_id": env.id}
        # ★必须带合法 mode，否则 Core 不签收★（v0.1.1 errata B1）
        tier_name = str(outcome.get("tier") or self.tier_now()["tier"])
        response_mode = ("immediate" if tier_name == "B" and reply.get("sent")
                         else "session" if tier_name == "A"
                         else "batched")
        return {"ok": True, "mode": response_mode,
                "envelope_id": env.id, "tier": self.tier_now()["tier"],
                "route": route,
                "unread": len(messages), "outcome": outcome, "reply": reply,
                "pending_id": pending_id}

    def _watch_desktop_reply(self, pending_id: str, messages: list[dict],
                             env: Envelope, outcome: dict[str, Any]) -> None:
        """Reap an asynchronous Desktop API turn and emit exactly one ANP reply."""
        session_id = str(outcome.get("session_id") or "")
        try:
            config = (self.platform._last_tier or {}).get("desktop_api")
            if not config or not session_id:
                raise RuntimeError("desktop reply watcher has no session/config")
            before_ids = {
                str(item) for item in (outcome.get("before_ids") or []) if str(item)
            }
            started = float(outcome.get("turn_started_ms") or 0)
            timeout = float(outcome.get("timeout_sec") or 120)
            deadline = time.time() + max(1.0, timeout)
            encoded = urllib.parse.quote(session_id, safe="")
            while time.time() < deadline:
                time.sleep(1.0)
                current = self.platform._desktop_api_request(
                    config, f"/v1/sessions/{encoded}/messages", timeout=20.0)
                if not isinstance(current, list):
                    continue
                candidates = []
                for item in current:
                    info = item.get("info") if isinstance(item, dict) else {}
                    if not isinstance(info, dict) or info.get("role") != "assistant":
                        continue
                    if str(info.get("id") or "") in before_ids:
                        continue
                    created = (info.get("time", {}).get("created")
                               if isinstance(info.get("time"), dict) else 0)
                    try:
                        if started and float(created or 0) < started - 1000:
                            continue
                    except (TypeError, ValueError):
                        pass
                    candidates.append(item)
                if not candidates:
                    continue
                latest = candidates[-1]
                text = self.platform._assistant_text(latest)
                finish = str((latest.get("info") or {}).get("finish") or
                             latest.get("finish") or "")
                parts = latest.get("parts") if isinstance(latest, dict) else []
                running_tool = any(
                    isinstance(part, dict)
                    and part.get("type") == "tool"
                    and str((part.get("state") or {}).get("status") or "")
                    in ("running", "pending")
                    for part in (parts if isinstance(parts, list) else [])
                )
                # Some Desktop API versions omit ``finish``. A text-only
                # assistant message is complete; a running tool part is not.
                if not text or running_tool or finish in ("tool-calls", "error"):
                    continue
                sent = self._reap(messages, env, {"reply": text})
                self.platform.finish_work(pending_id, ok=bool(sent.get("ok")),
                                          detail="replied" if sent.get("sent") else "reply-failed")
                self.platform.log(
                    f"MiMo Desktop API session={session_id} reply watcher finished "
                    f"finish={finish!r} sent={sent.get('sent')}"
                )
                return
            raise TimeoutError(f"desktop reply timeout after {timeout:g}s")
        except Exception as exc:  # noqa: BLE001
            self.platform.log(
                f"MiMo Desktop API reply watcher failed: {type(exc).__name__}: {exc}"
            )
            self.platform.finish_work(
                pending_id, ok=False, detail=f"{type(exc).__name__}: {exc}"
            )
            self._compensate_failure(
                env, f"{type(exc).__name__}: {exc}"
            )
        finally:
            if session_id:
                self.platform.release_desktop_turn(session_id)

    def _compensate_failure(self, env: Envelope, error: str) -> None:
        """S3-C：同步路径失败补偿——向发信方发失败回信。"""
        if not env.from_:
            return
        try:
            client = self.client.as_identity(self.agent_key, MANAGED_AGENT) if self.client and self.agent_key else None
            if client is None:
                self.platform.log(f"compensation skipped (no client): {error}")
                return
            body = f"[MiMo Adapter] 平台执行失败，本封未能完成。\nerror: {error}\nenvelope: {env.id}"
            response = client.send(
                env.from_, body, reply_to=env.id,
                correlation_id=env.correlation_id, kind="reply", wake=True)
            if not response.get("ok"):
                self.platform.log(f"compensation persistence failed: {response}")
        except Exception as exc:  # noqa: BLE001
            self.platform.log(f"compensation failed: {type(exc).__name__}: {exc}")

    # ---- ③ 语义翻译：信封 → 平台原生 ----
    def to_native(self, env: Envelope) -> dict:
        """★只取平台要的字段★，不把整个信封泄给平台。"""
        return {"text": env.body, "sender": env.from_, "subject": env.subject,
                "correlation_id": env.correlation_id,
                "context_id": str((env.meta or {}).get("context_id") or ""),
                "reply_to_session": str((env.meta or {}).get("reply_to_session") or "")}

    # ---- ③ 平台递送（按档位）----
    def _deliver_to_platform(self, native: dict, messages: list[dict],
                             wake_json: Any) -> dict:
        t = self.tier_now()["tier"]
        if t == "A" and self.tier_now().get("desktop_api"):
            return self.platform.send_desktop_turn(messages, wake_json)
        if t == "A" and self.tier_now().get("serve"):
            out = self.platform.announce_serve(str(self.tier_now()["serve"]),
                                             messages, wake_json)
            if out.get("ok"):
                return out
            # 降级 C（旧实现的降级路径）
            self.tier = {"tier": "C", "label": "queue-poll",
                         "detail": "serve 无 wake 路由，降级 C", "cli": None, "serve": ""}
            t = "C"
        if t == "B" and self.tier_now().get("cli"):
            if messages:
                self.platform.toast("MiMo Mailbox",
                                    f"B 档已拉起，处理 {len(messages)} 封未读")
            return self.platform.run_headless(str(self.tier_now()["cli"]), messages, wake_json)
        # C 档：只落摘要
        if messages:
            first = messages[0]
            self.platform.toast("MiMo Mailbox",
                                f"{len(messages)} 封未读，最近: "
                                f"{first.get('from')} · {str(first.get('subject') or '')[:40]}")
        return {"ok": True, "tier": "C", "queued": len(messages),
                "pending": str(self.platform.pending_md), "deferred": True,
                "reason": "queue-poll"}

    # ---- ④ 回收回信 ----
    def _reap(self, messages: list[dict], env: Envelope,
              outcome: dict[str, Any]) -> dict[str, Any]:
        """回收 B 档平台产出；A/C 档明确标记为延后由会话侧回信。"""
        text = str(outcome.get("reply") or outcome.get("text") or "").strip()
        if not text:
            return {"ok": True, "sent": False, "deferred": True,
                    "inbox_size": len(messages)}
        if self.client is None or not self.agent_key or not env.from_:
            raise RuntimeError("MiMo agent client is not ready for reply")
        response = self.client.as_identity(self.agent_key, MANAGED_AGENT).send(
            env.from_, text, reply_to=env.id,
            correlation_id=env.correlation_id, kind="reply", wake=True)
        if not response.get("ok"):
            raise RuntimeError(f"reply persistence failed: {response}")
        return {"ok": True, "sent": True, "response": response,
                "inbox_size": len(messages)}

    # ---- peek：只用公开 API ----
    def _peek_unread(self) -> list[dict]:
        """★只用 SDK 的 receive(peek=True)★。

        旧实现这里走 `core.read_records()`（Core 内部）+ `mailbox_mcp.call_backend()`
        ——#4.1 记下的 Spec §1「禁止做」违规。新实现只用公开 API。
        """
        if self.client is None:
            return []
        c = self.client.as_identity(self.agent_key, MANAGED_AGENT)
        return list(c.receive(peek=True, unread_only=True, limit=20).get("messages") or [])

    # ---- 手动取信（对应旧实现的 poll 子命令）----
    def poll(self) -> list[dict]:
        msgs = self._peek_unread()
        self.platform.write_pending(msgs, self.tier_now(), None)
        return msgs


def time_sleep(sec: float) -> None:
    import time
    time.sleep(sec)


def main() -> int:
    if not os.environ.get("ANP_PLUGIN_PORT"):
        raise SystemExit("没有 ANP_PLUGIN_PORT：本插件必须由 Core 拉起，不要手工跑")
    ad = MimoAdapter()
    srv = PluginServer(
        on_register=ad.on_register,
        on_wake=ad.on_wake,
        health_provider=ad.platform.health_payload,
        platform_health_path="/anp/platform-health",
    )
    # ★先绑端口，再做任何慢活★（#4.1 实测结论）
    #
    # Core 拉起插件后只等 10 秒（REGISTER_TIMEOUT）就来 POST /anp/register。
    # 而本插件的慢活不少：detect_tier() 要 probe_serve 三条路径（各 1.2s 超时）、
    # desktop_running() 要拉起 powershell、SDK import……全堆在构造里，
    # 曾让 Core 等到超时报 endpoint.unreachable，而服务其实再等两秒就起来了。
    #
    # 正确形状：start() 立刻 bind（让 /anp/register 打得进来），
    # 慢活（档位探测、代管注册）放到 on_register 回调里、或后台线程里。
    srv.start()
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
