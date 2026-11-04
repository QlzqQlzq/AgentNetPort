"""统一权限策略：谁能铸造身份 / 谁能广播 / 谁能发给谁。

设计约束（主人 2026-09-22：「一身份一密钥没必要，统一 + 权限就行」）：
    * **密钥表保持一个** `keys.json`，不拆成"一身份一文件"。本机所有 agent 跑在同一个
      OS 账户下，拆文件只是把同一把钥匙换了个抽屉，挡不住读取；
    * 真正该管的是**行为权限**，所以在这一层之上放一份**统一的、人可编辑的**策略文件，
      在唯一的几个决策点上执行。

三条策略项（都可单独收紧，互不影响）：

    register  谁能"铸造新身份"。`POST /heartbeat` 对**从没出现过的名字**会自动发密钥
              ——本机任何进程都能凭空造出一个身份、拿一个收件箱、然后开始发信。
              真实账本里 `fixprobe` / `regressprobe` / `root` / `freshprobe` 就是这么来的。
    announce  谁能发公告广播。广播会**推送全员、可能拉起不在线的 agent**，是花钱的动作。
    send_to   谁能发给谁（拓扑控制）。形如 `{"dsh": ["codex", "chencode", "*"]}`。

默认全部 `open`（与加上这一层之前的行为**完全一致**）：这是接入层，收紧必须是显式动作，
默默改变行为会打断正在跑的 agent。每一项要收紧，改 `permissions.json` 一个字段即可，
不需要改代码、不需要重启任何 agent（hub 每次决策都重读，改完即生效）。

★读不到/解析失败怎么办★：只影响**显式配置过收紧项**的人（默认 open 的人无感），
  所以这里选择**收紧项一律拒绝** + loud log，而不是静默放开。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import anp_core as core  # noqa: E402

DEFAULT_HOME = core.DEFAULT_HOME

# 策略文件路径：默认 <home>/permissions.json，可用 AGENT_ANP_POLICY 覆盖
# （与 AGENT_ANP_AGENTS 的接线表同一个风格）。
def policy_path(home: Path = DEFAULT_HOME) -> Path:
    env = str(os.environ.get("AGENT_ANP_POLICY") or "").strip()
    return Path(env).expanduser() if env else Path(home) / "permissions.json"


# 三项的取值：open = 不限制（默认）；allowlist = 只放行名单里的；closed = 谁都不行
MODES = ("open", "allowlist", "closed")

DEFAULT_POLICY: dict[str, Any] = {
    "version": 1,
    "register": {"mode": "open", "allow": []},
    "announce": {"mode": "open", "allow": []},
    "send_to": {"mode": "open", "allow": {}},
}


class PolicyUnreadable(Exception):
    """策略文件存在但读不动/解析失败 —— 收紧项一律拒绝，不静默放开。"""


def default_policy() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_POLICY))


def _norm_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in MODES else "open"


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def load(home: Path = DEFAULT_HOME) -> tuple[dict[str, Any], str]:
    """读策略。返回 `(策略, 状态)`，状态供 `/health` 展示：ok / default / unreadable。"""
    p = policy_path(home)
    if not p.exists():
        return default_policy(), "default"
    try:
        data = json.loads(core.read_text_shared(p))
    except (json.JSONDecodeError, OSError) as e:
        raise PolicyUnreadable(f"{p} 读取失败（{type(e).__name__}: {e}）") from e
    if not isinstance(data, dict):
        raise PolicyUnreadable(f"{p} 内容不是 JSON 对象")
    out = default_policy()
    for key in ("register", "announce", "send_to"):
        item = data.get(key)
        if not isinstance(item, dict):
            continue
        out[key] = {"mode": _norm_mode(item.get("mode")),
                    "allow": (item.get("allow") if key == "send_to"
                              else _as_list(item.get("allow")))}
    return out, "ok"


def load_safe(home: Path = DEFAULT_HOME) -> tuple[dict[str, Any], str]:
    """读策略，异常时**收紧项一律拒绝**（默认 open 的人不受影响，见模块说明）。"""
    try:
        return load(home)
    except PolicyUnreadable as e:
        sys.stderr.write(f"[policy] 策略读不动，收紧项一律拒绝：{e}\n")
        sys.stderr.flush()
        strict = default_policy()
        for key in ("register", "announce", "send_to"):
            strict[key]["mode"] = "closed"
        return strict, f"unreadable: {e}"


# --------------------------------------------------------------------------- #
# 三项判定（hub 与 direct 降级路径共用同一份，别在两处各写一遍）
# --------------------------------------------------------------------------- #
def may_register(name: str, is_admin: bool = False, home: Path = DEFAULT_HOME) -> tuple[bool, str]:
    """这个名字可以被铸造吗？（`POST /heartbeat` 的首次引导路径）

    持管理员令牌的人永远放行——否则"上锁"会把接线脚本自己关在门外。
    """
    who = str(name or "").strip()
    if is_admin:
        return True, "admin"
    if not who:
        return False, "身份名不能为空"
    policy, state = load_safe(home)
    item = policy["register"]
    mode = str(item.get("mode") or "open")
    if mode == "open":
        return True, "open"
    if mode == "allowlist" and who in _as_list(item.get("allow")):
        return True, "allowlisted"
    return False, (f"register={mode}" + (f"（{who} 不在 {_as_list(item.get('allow'))} 里）"
                                        if mode == "allowlist" else "")
                   + f"；策略状态={state}")


def may_announce(sender: str, home: Path = DEFAULT_HOME) -> tuple[bool, str]:
    """这个身份可以发公告广播吗？（广播=推送全员、可能拉起不在线的人）"""
    who = str(sender or "").strip() or "unknown"
    policy, state = load_safe(home)
    item = policy["announce"]
    mode = str(item.get("mode") or "open")
    if mode == "open":
        return True, "open"
    if mode == "allowlist" and who in _as_list(item.get("allow")):
        return True, "allowlisted"
    return False, (f"announce={mode}" + (f"（{who} 不在 {_as_list(item.get('allow'))} 里）"
                                        if mode == "allowlist" else "")
                   + f"；策略状态={state}")


def may_send_to(sender: str, target: str, home: Path = DEFAULT_HOME) -> tuple[bool, str]:
    """sender 可以发给 target 吗？`send_to.mode=open` 时恒真（默认）。"""
    who = str(sender or "").strip() or "unknown"
    to = str(target or "").strip()
    policy, state = load_safe(home)
    item = policy["send_to"]
    mode = str(item.get("mode") or "open")
    if mode == "open" or not to:
        return True, "open"
    allow = item.get("allow") if isinstance(item.get("allow"), dict) else {}
    allowed = _as_list(allow.get(who))
    if mode == "allowlist" and ("*" in allowed or to in allowed):
        return True, "allowlisted"
    return False, f"send_to={mode}（{who} 不允许发给 {to}）；策略状态={state}"


def summary(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    """策略现状，给 `/health` 与看板用（"谁能干什么"必须可直接读出来）。"""
    try:
        policy, state = load(home)
    except PolicyUnreadable as e:
        return {"path": str(policy_path(home)), "state": f"unreadable: {e}",
                "register": "closed(读不动)", "announce": "closed(读不动)", "send_to": "closed(读不动)"}
    return {
        "path": str(policy_path(home)),
        "state": state,
        "register": f"{policy['register']['mode']}"
                    + (f" → {_as_list(policy['register']['allow'])}"
                       if policy['register']['mode'] == 'allowlist' else ""),
        "announce": f"{policy['announce']['mode']}"
                    + (f" → {_as_list(policy['announce']['allow'])}"
                       if policy['announce']['mode'] == 'allowlist' else ""),
        "send_to": f"{policy['send_to']['mode']}"
                   + (f" → {json.dumps(policy['send_to']['allow'], ensure_ascii=False)}"
                      if policy['send_to']['mode'] == 'allowlist' else ""),
        "note": "默认三项全 open（与未加权限层时一致）；要收紧改这个文件，hub 每次决策重读，改完即生效",
    }
