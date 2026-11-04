"""协议错误的最小子集。

SDK 只做**分类**，不发明错误码。Protocol 层已有 `manifest.invalid` /
`endpoint.unreachable` / `permission.denied` 这些（给 Plugin Spec 用的）；
Adapter 侧没有自己的错误码表——这是规范 §12 里记着的 P1 缺口，
补之前 SDK 只用下面这几个**派生自 HTTP 语义**的分类。
"""
from __future__ import annotations


class AnpError(Exception):
    """一次与 Core 的交互失败了。

    kind 只有四种，全部从 HTTP 语义直接推导，不新增协议概念：
      transport   连不上（网络层失败，HTTP 状态码都没有）
      auth        401 —— 密钥缺失 / 不对 / 身份已被别人占（P0-2）
      policy      403 —— 策略层拒绝（register / announce / send_to）
      rejected    4xx/5xx 且 Core 给了 error 字段
    """

    def __init__(self, kind: str, message: str, *, status: int = 0,
                 payload: dict | None = None):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind
        self.status = status
        self.payload = payload or {}

    @property
    def hint(self) -> str:
        return str(self.payload.get("hint") or "")

    # ★曾有一个 `from_response(status, payload)` 类方法，已删除（2026-09-24）★：
    # 全仓库零调用方——`client.post/get` 只在**网络层**失败时手搓 AnpError，
    # 业务层的 4xx/5xx 走"调用方自己看响应体"的路。留着它会让第三方以为
    # "SDK 会帮我把 HTTP 错误转成异常"，于是写出混合两种错误处理的代码。
