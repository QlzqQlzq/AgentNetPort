"""Envelope 的读取与唤醒载荷解析。

这层只做**字段访问与基本校验**，不解释业务含义。
信封格式的权威定义在 `ANP-PROTOCOL.md`；这里只是让 Adapter 不必满篇 `dict.get`。
"""
from __future__ import annotations

# Envelope 的必备字段。缺了任何一个都不算一封能回的信。
REQUIRED = ("id", "from", "to", "body", "kind")

# ★v0.1.1（errata B1）★：/anp/wake 的 HTTP **响应**必须带合法 mode，
# 否则 Core 不当成签收。合法值是闭集，写在这里让所有实现共用一份。
RESPONSE_MODES = ("immediate", "batched", "session", "inject", "followup")
DEFAULT_RESPONSE_MODE = "session"      # SDK 默认：信封进了平台的会话/队列


class Envelope:
    """一封 ANP 信封的只读视图。

    刻意不做 dict 的子类：Adapter 拿到的不该是一个可以随便改的内部结构。
    要原始 dict 用 `.raw`。
    """

    __slots__ = ("raw",)

    def __init__(self, raw: dict | None):
        self.raw = dict(raw or {})

    # ---- 字段访问 ----
    @property
    def id(self) -> str:
        return str(self.raw.get("id") or "")

    @property
    def from_(self) -> str:
        """发信方。`from` 是 Python 关键字，所以叫 `from_`。"""
        return str(self.raw.get("from") or "")

    @property
    def to(self) -> str:
        return str(self.raw.get("to") or "")

    @property
    def body(self) -> str:
        return str(self.raw.get("body") or "")

    @property
    def kind(self) -> str:
        return str(self.raw.get("kind") or "message")

    @property
    def subject(self) -> str:
        return str(self.raw.get("subject") or "")

    @property
    def reply_to(self) -> str:
        return str(self.raw.get("reply_to") or "")

    @property
    def correlation_id(self) -> str:
        return str(self.raw.get("correlation_id") or "")

    @property
    def meta(self) -> dict:
        return self.raw.get("meta") or {}

    @property
    def sig(self) -> str:
        return str(self.raw.get("sig") or "")

    # ---- 基本校验（只有"能不能回信"，没有别的） ----
    def missing(self) -> list[str]:
        """缺哪些必备字段。空 list = 完整。"""
        return [k for k in REQUIRED if not self.raw.get(k)]

    @property
    def is_complete(self) -> bool:
        return not self.missing()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"Envelope(id={self.id!r}, from={self.from_!r}, to={self.to!r}, "
                f"kind={self.kind!r}, body={self.body[:24]!r})")


def from_wake_payload(body: dict, headers: dict | None = None) -> tuple[Envelope, dict]:
    """解析 Core 打来的 `/anp/wake` 载荷。

    Core 发的是 `{"message": <完整信封>, "to": <Agent 身份名>}`（Spec §5.1），
    并在 `x-mailbox-key` 头捎上**目标 Agent** 的令牌。

    返回 `(信封, 旁证)`。旁证里有三样 Adapter 会用到的东西：
      agent_key   `x-mailbox-key` 的值（目标 Agent 的令牌，不是 adapter 的 ★P0-4★）
      said_to     body 里的 `to`
      raw_envelope  信封的原始 dict

    ★本函数不做鉴权决策★——"这个 key 对不对"由 Adapter 自己判断，
    SDK 只负责把东西取出来。
    """
    body = body or {}
    headers = headers or {}
    # ★HTTP 头大小写不敏感（RFC 7230）★：`x-mailbox-key` 与 `X-Mailbox-Key` 是同一个头。
    # 原来 `headers.get("x-mailbox-key")` 是**大小写敏感**查找——把名字规范化成 Title-Case
    # 的客户端（urllib、部分代理/网关）会让令牌静默变空串，adapter 侧永远拿到
    # agent_key="" 而且不报错。（G1 实测，2026-09-23）
    low = {str(k).lower(): v for k, v in dict(headers).items()}
    env = body.get("message")
    if not isinstance(env, dict):
        env = body if "id" in body else {}
    return Envelope(env), {
        "agent_key": str(low.get("x-mailbox-key") or ""),
        "said_to": str(body.get("to") or ""),
        "raw_envelope": env,
    }
