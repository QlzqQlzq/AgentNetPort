"""最小 Adapter Plugin 原型（#6 验证用，现基于 ANP Adapter SDK）。

要验证的那条路：

    Core 拉起本插件
      → Core POST /anp/register 送来身份 + 令牌
        → 本插件用这对凭证，通过**公开** POST /heartbeat 注册 demo-01
          → demo-01 的 wake 指向本插件自己的 /anp/wake
            → 别的 Agent 给 demo-01 发信
              → Core 推到 /anp/wake
                → 本插件收到完整 Envelope

★不 import Core、不复用 anp.bulletin★。协议面全部来自 `sdk/anp_adapter/`：
HTTP 客户端、6 个 Core API、Envelope、wake 解析、PluginServer 骨架。

它刻意什么都不额外做：不注册工具、不订阅事件、不加面板——只回答
「Adapter Plugin 到底能不能代管一个 Agent 身份」这一个问题。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _find_sdk() -> Path | None:
    """定位 ANP Adapter SDK。

    ★这暴露了一个真实的部署后果★：Adapter Plugin 由 Core 从
    `<home>/plugins/<id>/` 拉起，那时"仓库根的 ../sdk"根本不在。
    所以 SDK 必须**随插件一起交付**（vendor 进插件目录，或装进站点目录）。

    按优先级找：
      1. `<插件目录>/anp_adapter`         —— vendor 进来的（部署形态）
      2. `<AGENT_ANP_HOME>/sdk/anp_adapter` —— 装在 ANP home 下
      3. `<仓库>/sdk/anp_adapter`          —— 在仓库里原地跑（开发形态）
    """
    home = os.environ.get("AGENT_ANP_HOME") or ""
    for cand in (_HERE / "anp_adapter",
                 Path(home) / "sdk" / "anp_adapter" if home else None,
                 _HERE.parent.parent / "sdk" / "anp_adapter"):
        if cand is not None and (cand / "__init__.py").is_file():
            return cand.parent
    return None


_sdk = _find_sdk()
if _sdk is not None and str(_sdk) not in sys.path:
    sys.path.insert(0, str(_sdk))
if _sdk is None:
    raise SystemExit(
        "找不到 ANP Adapter SDK。Adapter Plugin 依赖它，必须 vendor 进插件目录"
        "（把 sdk/anp_adapter/ 复制到本目录）或装到 AGENT_ANP_HOME/sdk/ 下。")

from anp_adapter import (AnpClient, Envelope, PluginServer,        # noqa: E402
                        DEFAULT_RESPONSE_MODE)

MANAGED_AGENT = "demo-01"
DATA = _HERE / "data"
RECV = DATA / "received.jsonl"


def fingerprint(token: str) -> str:
    """令牌的短指纹（sha256 前 16 位）。只用于比对，不可反推。"""
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] if token else ""


def record(env: Envelope, side: dict | None = None) -> None:
    """把收到的信封落到自己的目录，供外部核验（不碰 <home> 下任何信箱文件）。

    ★P0-4★：`side['agent_key']` 是 Core 投递时捎过来的那枚令牌。这里**只存它的指纹**，
    不落明文——核验方拿 `key_for(<agent>)` 算同一个指纹来比对，就能锁死"投递头里
    就是目标 Agent 那枚"，而不必把令牌写进任何文件。（G1，2026-09-23）
    """
    DATA.mkdir(parents=True, exist_ok=True)
    row: dict = {"at": time.time(), "envelope": env.raw}
    if side:
        row["delivered_key_fp"] = fingerprint(str(side.get("agent_key") or ""))
    with RECV.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


class DemoAdapterPlugin:
    """代管 demo-01 的 Adapter Plugin。

    三段式，对应 Spec §1 的四条职责：
      托管身份   —— on_register 拿自己的凭证
      实现唤醒   —— 代 demo-01 注册，wake 指向本插件
      语义翻译   —— to_native / from_native
      回收回信   —— on_wake 里顺手把回信发回去
    """

    def __init__(self) -> None:
        self.client: AnpClient | None = None     # 插件自己那枚
        self.agent_key = ""                      # 代管的 agent 那枚
        self.register_state: dict = {}
        self.platform_inbox: list[dict] = []

    # ---- Spec §3：用公开 /heartbeat 代 demo-01 注册 ----
    def register_agent(self, client: AnpClient, retry: int = 20) -> dict:
        """★wake 指向本插件自己的 /anp/wake★——这才是"Adapter"的关键：
        外部平台没有可寻址端点，本插件替它占一个。

        demo-01 是**新名字**，走的是 register 策略门（不是"已有身份必须带对令牌"）；
        带的令牌只在它是管理员令牌时影响 is_admin。
        token 必须带上，否则第二次注册被 P0-2 的 401 挡掉。
        """
        wake = {"kind": "http",
                "url": f"http://127.0.0.1:{os.environ.get('ANP_PLUGIN_PORT') or 0}"
                       f"/anp/wake"}
        last: dict = {}
        for i in range(retry):
            r = client.heartbeat(MANAGED_AGENT, token=client.token, wake=wake,
                                 description="由 Adapter Plugin 代管的外部 agent 身份",
                                 client="adapter-plugin")
            last = r
            if r.get("ok"):
                self.agent_key = str(r.get("key") or "")
                self.register_state = {"ok": True, "attempt": i + 1,
                                       "agent_key": self.agent_key}
                return r
            time.sleep(0.5)
        self.register_state = {"ok": False, "last": last}
        return last

    # ---- Core → 插件的握手 ----
    def on_register(self, res) -> None:
        """res 是 RegisterResult：Core 送来的身份 + 令牌 + 配置。"""
        self.client = res.client()          # 以插件自己的身份
        self.register_agent(self.client)

    # ---- 唤醒入口：翻译 → 递平台 → 回收回信 ----
    def on_wake(self, env: Envelope, side: dict) -> dict:
        record(env, side)
        # side['agent_key']（x-mailbox-key）是**目标 Agent** 的令牌，
        # 不是本插件的（P0-4）
        native = {"text": env.body, "sender": env.from_,
                  "correlation_id": env.correlation_id,
                  "session_hint": str((env.meta or {}).get("context_id") or "")}
        self.platform_inbox.append(native)
        # 回收回信：真实平台是异步的，这里为演示同步做掉
        if self.client is not None and env.from_:
            self.client.as_identity(self.agent_key, MANAGED_AGENT).send(
                to=env.from_, body=f"平台已处理：{native['text']}",
                reply_to=env.id,                     # ★必须带回★
                correlation_id=native["correlation_id"] or env.correlation_id,
                kind="reply", wake=False)
        # ★必须带合法 mode，否则 Core 不签收★（v0.1.1 errata B1）
        return {"ok": True, "mode": DEFAULT_RESPONSE_MODE,
                "envelope_id": env.id,
                "key_is_agent_not_plugin": bool(side.get("agent_key"))}


def main() -> int:
    if not os.environ.get("ANP_PLUGIN_PORT"):
        raise SystemExit("没有 ANP_PLUGIN_PORT：本插件必须由 Core 拉起，不要手工跑")
    plugin = DemoAdapterPlugin()
    srv = PluginServer(on_register=plugin.on_register, on_wake=plugin.on_wake)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
