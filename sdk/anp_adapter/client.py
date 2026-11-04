"""Core HTTP 客户端 + 那 6 个 Core API 的薄封装。

这层**只有便利，没有语义**。它不缓存、不重试决策、不解释协议——
重试与退避是 Core 的事（Adapter Spec §1「禁止做」第一条）。

★不 import 任何 Core 模块★：与 Core 之间只有 HTTP。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .errors import AnpError

# Spec §6.1 的 canonical 面。SDK 只认这几个，别的地方一个都不打。
HEARTBEAT = "/heartbeat"
SEND = "/messages/send"
RECEIVE = "/messages/receive"
QUERY = "/messages/query"
AGENTS = "/agents"
PLUGIN_REGISTER = "/plugin/register"

_KEY_HEADER = "x-mailbox-key"      # Spec §5.1：这个头捎的是**目标 Agent** 的令牌


def post(base: str, path: str, body: dict, key: str = "",
         timeout: float = 30.0) -> tuple[int, dict]:
    """POST 一个 JSON，返回 (状态码, 响应体)。

    key 非空时带 `x-mailbox-key`。★响应头必须纯 ASCII★——http.server 的
    send_header 用 latin-1 编码，非 ASCII 值会让服务端 handler 半路死掉。
    """
    base = str(base).rstrip("/")
    headers = {"content-type": "application/json; charset=utf-8"}
    if key:
        headers[_KEY_HEADER] = str(key)
    req = urllib.request.Request(
        base + path, method="POST",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            try:
                return r.status, json.loads(raw or "{}")
            except json.JSONDecodeError:
                return r.status, {"_raw": raw}
    except urllib.error.HTTPError as e:
        # 4xx/5xx 的响应体里通常有 error + hint，别丢掉
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            return e.code, {}
    except urllib.error.URLError as e:
        # 网络层失败：没有状态码。用一个 AnpError 承载，避免上层各写一套
        raise AnpError("transport", f"连不上 {base}{path}：{getattr(e, 'reason', e)}") from e


def get(base: str, path: str, timeout: float = 15.0) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(str(base).rstrip("/") + path,
                                    timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            return e.code, {}
    except urllib.error.URLError as e:
        raise AnpError("transport", f"连不上 {base}{path}：{getattr(e, 'reason', e)}") from e


class AnpClient:
    """一个身份对 Core 的一组调用。

    一个 client = 一枚令牌 + 一个身份名。★要换身份就换一个 client★，
    不要复用同一个 client 改 token——两个身份两枚令牌本来就不该混（P0-1）。
    """

    def __init__(self, hub_base: str = "http://127.0.0.1:8900",
                 token: str = "", who: str = ""):
        self.base = str(hub_base).rstrip("/")
        self.token = str(token or "")
        self.who = str(who or "")

    # ---- 让 client 可以被复制着换身份 ----
    def as_identity(self, token: str, who: str) -> "AnpClient":
        c = AnpClient(self.base, token, who)
        return c

    # ---- Spec §6.1 ① POST /heartbeat ----
    def heartbeat(self, name: str, token: str | None = None,
                  **declaration) -> dict:
        """注册 / 续约。

        ★已有身份 + 不带 key → 401，绝不重新发令牌★（P0-2）。
        ★首次不带 key = bootstrap，后续必须带 key = renewal★（P0-10）。

        `token=None` 表示"用我这枚"；传 "" 表示"显式不带"（首次注册用）。
        """
        key = self.token if token is None else token
        _, r = post(self.base, HEARTBEAT, {"name": name, **declaration}, key)
        return r

    def bootstrap(self, name: str, **declaration) -> dict:
        """首次注册：显式不带 key，从响应里拿令牌（Spec §3）。"""
        return self.heartbeat(name, token="", **declaration)

    # ---- Spec §6.1 ② POST /messages/send ----
    def send(self, to: str, body: str, *, reply_to: str = "",
             correlation_id: str = "", kind: str = "message",
             subject: str = "", meta: dict | None = None,
             wake: bool = True) -> dict:
        """发信。

        ★返回的 ok=true 只表示「消息已持久化」，不表示对端已收到★（Spec §9）。
        投递结果看 `wake` 字段；消息记录里的状态字段看 `/messages/receive` 的 messages。
        """
        payload: dict = {"from": self.who, "to": to, "body": body,
                         "kind": kind, "wake": bool(wake)}
        if reply_to:
            payload["reply_to"] = reply_to
        if correlation_id:
            payload["correlation_id"] = correlation_id
        if subject:
            payload["subject"] = subject
        if meta:
            payload["meta"] = meta
        _, r = post(self.base, SEND, payload, self.token)
        return r

    # ---- Spec §6.1 ③ POST /messages/receive ----
    def receive(self, *, peek: bool = False, unread_only: bool = True,
                limit: int = 20, order: str = "oldest",
                kind: str = "", from_: str = "",
                correlation_id: str = "") -> dict:
        """读信（与 `/tool` 的 mail_inbox 是**同一份实现**，P0-9）。

        返回的 messages 带完整状态字段（attempts / next_retry_at /
        wake_skipped / last_error / read_by / reply_to…），所以 Adapter
        不需要去翻 Core 的任何文件就能判断状态。
        """
        body = {"from": from_ or self.who, "peek": bool(peek),
                "unread_only": bool(unread_only), "limit": int(limit),
                "order": order}
        if kind:
            body["kind"] = kind
        if correlation_id:
            body["correlation_id"] = correlation_id
        _, r = post(self.base, RECEIVE, body, self.token)
        return r

    # ---- Spec §6.1 ④ POST /messages/query ----
    def query(self, correlation_id: str) -> dict:
        """按对话线查。验回信是否落回原线就用它。"""
        _, r = post(self.base, QUERY, {"from": self.who,
                                        "correlation_id": correlation_id},
                    self.token)
        return r

    # ---- Spec §6.1 ⑤ GET /agents ----
    def agents(self) -> dict:
        """名册（免令牌，不含正文）。"""
        _, r = get(self.base, AGENTS)
        return r

    # ---- 不做协议、只做便利：模糊目标 → canonical Agent ID（Spec §13） ----
    def resolve(self, target, *, hints: dict | None = None, **kw):
        """把一个模糊目标解析成 canonical Agent ID。

        ★这不是第 7 个 Core API★：它**只调 /agents**（上面那个），加上
        Adapter 自己持有的平台提示（alias / tags / workspace 标题），
        用普通字符串算法在本地算。Core 完全不知道有人在做模糊解析。

        返回 `Resolution`（dict 子类，带 status / target / candidates / reason /
        decided_by）。`status` 取值见 `anp_adapter.resolve`：

            RESOLVED   → target 就是可以发信的 canonical Agent ID
            AMBIGUOUS  → 多个相近候选，交给上层/用户再指一次
            NOT_FOUND  → 名册里没有匹配
            DIRECTORY_UNAVAILABLE / INVALID_TARGET / …

        `hints` 按 agent_id 索引，放**只有 Adapter 知道**的东西：

            {"demo-live2d-a": {"aliases": ["立绘"], "handle": "dsh-11451",
                               "workspace": "Live2D 研究", "tags": ["art"]}}

        第三方 Adapter 完全可以不调这个方法——模糊解析是可选能力，不是协议要求。
        """
        from .resolve import (build_dir, resolve as _resolve,   # noqa: PLC0415
                              DIRECTORY_UNAVAILABLE, Resolution)
        if not isinstance(target, str) or not target.strip():
            # 目标本身不合法就**不必去打网络**——"没有可解析的对象"跟名册无关。
            return _resolve(target, [])
        try:
            roster = list(self.agents().get("agents") or [])
        except AnpError as e:
            # 名册拿不到 = 解析阶段就失败。★这与"投递失败"是两件事★（Spec §13）。
            return Resolution(
                {"status": DIRECTORY_UNAVAILABLE, "query": str(target),
                 "target": "", "candidates": [], "decided_by": "",
                 "resolve_stage": "resolve", "detail": f"{e.kind}: {e}",
                 "reason": f"拿不到 /agents 名册：{e}"})
        return _resolve(target, build_dir(roster, hints), **kw)

    # ---- Spec §6.1 ⑥ POST /plugin/register ----
    def plugin_register(self, plugin_id: str, plugin_token: str,
                        contributions: dict | None = None) -> dict:
        """插件报到：交贡献，换**自己的**凭证。

        ★C1 方案 P（2026-09-23）★：鉴的是**插件自己的 agent token**，不是 admin令牌。
        那一枚是 Core 在 `/anp/register` 时发给你的（响应体 `token` 字段，请求头也带了一份），
        插件重启后原样持着——所以这里不需要、也不接受 admin。

        `plugin_id`：你要报到的插件 id。令牌必须**正好是这个插件的**：
        拿 A 插件的令牌报到 B 插件会被 401 挡掉。
        `contributions`：可选。tools / events / panel；不传则沿用已加载的声明。
        """
        payload: dict = {"id": plugin_id}
        if contributions:
            # 只做形状归一化，★不解释语义★（SDK 的硬约束②）。
            # Core 侧仍然会用自己的口径再规一遍，这里只为让 JSON 好看点。
            payload["tools"] = list(contributions.get("tools") or [])
            payload["events"] = [str(e) for e in (contributions.get("events") or [])]
            payload["panel"] = str(contributions.get("panel") or "")
        _, r = post(self.base, PLUGIN_REGISTER, payload, plugin_token)
        return r

    # ---- 便捷：以另一个身份发信 ----
    def send_as(self, token: str, who: str, to: str, body: str, **kw) -> dict:
        """用另一枚令牌发信。★适配器"代 agent 发信"的标准写法★——
        临时换身份，不动自己那枚。"""
        return self.as_identity(token, who).send(to, body, **kw)
