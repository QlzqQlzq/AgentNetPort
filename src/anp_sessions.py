"""信箱侧的"会话大脑"：决定一封信该复用哪个会话、还是开个新的。

分工（MAILBOX-PROTOCOL.md §2.2）：
    agent / 插件  只负责「发信 / 收信」，并在打招呼时把自己的会话名册报上来；
    **信箱**       负责「怎么发、发给谁、要不要开新对话」——也就是这里；
    收信通道       只负责「照章执行」：注入指定会话、或新建一个会话。

本模块不依赖任何 agent 实现，只吃三样东西：
    ① 收信通道上报的会话名册（每个会话的 cwd / 最近活跃时间 / 是否在跑）
    ② 信箱自己记的对话线绑定（context_id → 会话）
    ③ 寄件人在信封里写的诉求（session_policy / session / context_id / to_workspace）

存储：<home>/sessions.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import anp_core  # noqa: E402
from typing import Any

DEFAULT_HOME = Path(os.environ.get("AGENT_ANP_HOME") or (Path.home() / ".agentnetport"))

# 名册超过这个秒数没更新就当作过期（收信通道可能已经退出）
ROSTER_TTL = float(os.environ.get("AGENT_ANP_ROSTER_TTL") or 300)


def registry_path(home: Path = DEFAULT_HOME) -> Path:
    return Path(home) / "sessions.json"


def _norm(value: Any) -> str:
    """工作区路径归一化：统一分隔符、去尾部分隔符、忽略大小写。"""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("/", "\\").rstrip("\\")
    return (text or "\\").lower()


def core_read_text(path: Path) -> str:
    """借 core 的抗并发读：Windows 字节锁下读者会 PermissionError，需要重试。"""
    return anp_core.read_text_shared(path)


def load(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    p = registry_path(home)
    if not p.exists():
        return {"rosters": {}, "contexts": {}, "decisions": []}
    try:
        data = json.loads(core_read_text(p))
    except (json.JSONDecodeError, OSError):
        return {"rosters": {}, "contexts": {}, "decisions": []}
    if not isinstance(data, dict):
        return {"rosters": {}, "contexts": {}, "decisions": []}
    data.setdefault("rosters", {})
    data.setdefault("contexts", {})
    data.setdefault("decisions", [])
    return data


def save(data: dict[str, Any], home: Path = DEFAULT_HOME) -> None:
    p = registry_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 决策流水只留最近 200 条
    data["decisions"] = (data.get("decisions") or [])[-200:]
    data["savedAt"] = time.time()
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# 上报与查询
# --------------------------------------------------------------------------- #
def update_roster(adapter: str, sessions: list[dict[str, Any]], home: Path = DEFAULT_HOME,
                  workspace: str = "") -> dict[str, Any]:
    """收信通道上报"我这边有哪些会话"。这是信箱做决策的唯一信息源。"""
    rows = []
    for s in sessions or []:
        sid = str(s.get("sessionId") or s.get("session_id") or "").strip()
        if not sid:
            continue
        rows.append({
            "sessionId": sid,
            "cwd": s.get("cwd") or workspace or None,
            "updatedAt": s.get("updatedAt") or s.get("updated_at") or None,
            "lastPromptAt": s.get("lastPromptAt") or s.get("last_active_at") or None,
            "running": bool(s.get("running")),
            "title": s.get("title") or None,
        })
    data = load(home)
    data["rosters"][adapter] = {
        "at": time.time(),
        "workspace": workspace or None,
        "sessions": rows,
    }
    save(data, home)
    return {"ok": True, "adapter": adapter, "sessions": len(rows)}


def _live_sessions(data: dict[str, Any], adapter: str) -> list[dict[str, Any]]:
    roster = (data.get("rosters") or {}).get(adapter) or {}
    if time.time() - float(roster.get("at") or 0) > ROSTER_TTL:
        return []          # 名册过期：当作"看不到任何会话"
    return list(roster.get("sessions") or [])


def bind_context(context_id: str, session_id: str, adapter: str, home: Path = DEFAULT_HOME,
                 workspace: str = "") -> dict[str, Any]:
    """收信通道执行完新开会话之后回报：这条对话线落在哪个会话上。"""
    key = str(context_id or "").strip()
    if not key:
        return {"ok": False, "error": "context_id 不能为空"}
    data = load(home)
    data["contexts"][key] = {
        "sessionId": str(session_id or ""),
        "adapter": adapter,
        "workspace": workspace or None,
        "at": time.time(),
    }
    save(data, home)
    return {"ok": True, "context_id": key, "sessionId": str(session_id or "")}


def roster_view(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    data = load(home)
    out = {}
    for adapter, roster in (data.get("rosters") or {}).items():
        live = _live_sessions(data, adapter)
        out[adapter] = {
            "at": roster.get("at"),
            "stale": time.time() - float(roster.get("at") or 0) > ROSTER_TTL,
            "workspace": roster.get("workspace"),
            "sessions": [
                {"sessionId": s.get("sessionId"), "cwd": s.get("cwd"),
                 "lastPromptAt": s.get("lastPromptAt"), "running": s.get("running")}
                for s in live
            ],
        }
    return {"rosters": out, "contexts": data.get("contexts") or {},
            "decisions": (data.get("decisions") or [])[-10:]}


# --------------------------------------------------------------------------- #
# 决策
# --------------------------------------------------------------------------- #
def decide(to: str, meta: dict[str, Any] | None = None, home: Path = DEFAULT_HOME) -> dict[str, Any]:
    """信箱的会话决策：返回盖章给信封的 meta.session。

    信封可写的诉求（都可缺省）：
        session_policy : "new"    → 明确要求开新对话
                         "reuse"  → 明确要求复用（找不到就新建）
                         "auto"（默认）→ 按对话线绑定 / 工作区 / 活跃度自己判
        session        : 点名要落在哪个会话（可缺省）
        context_id     : 对话线（可缺省；有绑定时优先复用）
        to_workspace   : 期望在哪个工作区干活（可缺省）

    返回：
        {"mode": "reuse"|"new", "session_id": <str|None>, "policy": <生效策略>,
         "reason": <人话解释>, "context_id": <str|None>, "workspace": <str|None>}
    """
    meta = meta or {}
    policy = str(meta.get("session_policy") or "auto").strip().lower()
    if policy not in ("new", "reuse", "auto"):
        policy = "auto"
    context_id = str(meta.get("context_id") or "").strip()
    wanted_session = str(meta.get("session") or "").strip()
    workspace = str(meta.get("to_workspace") or meta.get("from_workspace") or "").strip()

    data = load(home)
    sessions = _live_sessions(data, to)
    by_id = {s["sessionId"]: s for s in sessions}
    binding = (data.get("contexts") or {}).get(context_id) or {}
    bound = str(binding.get("sessionId") or "")

    def result(mode: str, session_id: str | None, reason: str) -> dict[str, Any]:
        return {"mode": mode, "session_id": session_id, "policy": policy,
                "reason": reason, "context_id": context_id or None,
                "workspace": workspace or None}

    # ① 明确点名
    if wanted_session:
        if wanted_session in by_id:
            return result("reuse", wanted_session, f"寄件人点名会话 {wanted_session}")
        return result("new", None, f"寄件人点名的会话 {wanted_session} 不在名册里 → 开新对话")

    # ② 明确要求新对话
    if policy == "new":
        return result("new", None, "寄件人明确要求开新对话（session_policy=new）")

    # ③ 对话线绑定（回信默认走这条）
    if bound and bound in by_id:
        return result("reuse", bound, f"对话线 {context_id} 已绑定会话 {bound}")

    # ④ 按工作区找：精确 → 子目录 → 上级；同级按 running → lastPromptAt → updatedAt
    if workspace:
        key = _norm(workspace)
        tier: list[tuple[str, list[dict[str, Any]]]] = [("exact", []), ("child", []), ("parent", [])]
        for s in sessions:
            cwd = _norm(s.get("cwd"))
            if not cwd:
                continue
            if cwd == key:
                tier[0][1].append(s)
            elif cwd.startswith(key + "\\"):
                tier[1][1].append(s)
            elif key.startswith(cwd + "\\"):
                tier[2][1].append(s)
        for label, rows in tier:
            if not rows:
                continue
            rows.sort(key=lambda s: (not s.get("running"),
                                     -float(s.get("lastPromptAt") or s.get("updatedAt") or 0)))
            chosen = rows[0]
            note = {"exact": "精确匹配", "child": "子目录匹配", "parent": "上级工作区匹配"}[label]
            return result("reuse", chosen["sessionId"],
                          f"工作区 {workspace} {note}：{len(rows)} 个候选，取"
                          + ("正在运行的" if chosen.get("running") else "最近活跃的"))

    # ⑤ 没有路由信息（既没点名、没对话线、也没带工作区）时怎么办？
    #    老行为是"开新对话"——但实践里这会把**同一个人的连续来信**拆成一堆新对话
    #    （实测：codex 漏带 from_workspace，立刻新开一个会话；而它下一封还要汇报这件事，
    #    于是再开一个……）。agent 侧的工具（尤其桌面端的 MCP）不一定拿得到自己的
    #    工作区/会话，所以不能把"缺信息"一律当成"新话题"。
    #    改成：只有当**恰好一个**会话在跑时才认作"同一件事的后续"；多个在跑或没有在跑
    #    就维持老行为（开新对话），不猜。要退回老行为设 AGENT_ANP_NEW_WHEN_NO_ROUTE=1。
    if not workspace and not context_id and not wanted_session:
        strict = str(os.environ.get("AGENT_ANP_NEW_WHEN_NO_ROUTE") or "").strip()
        running = [s for s in sessions if s.get("running")]
        if strict not in ("1", "true", "yes") and len(running) == 1:
            return result("reuse", running[0]["sessionId"],
                          "信里没带路由信息，但恰好有 1 个会话在跑 → 认作同一件事的后续，继续用它"
                          "（要改成开新对话：设 AGENT_ANP_NEW_WHEN_NO_ROUTE=1 或在信里写 session_policy=new）")
        if sessions:
            return result("new", None,
                          (f"没有路由信息且 {len(running)} 个会话在跑（不够明确）→ 开新对话"
                           if running else "没有路由信息且没有会话在跑 → 开新对话"))

    # ⑥ 兜底：名册里只有一个会话就用它，否则开新对话
    if len(sessions) == 1:
        return result("reuse", sessions[0]["sessionId"], "名册里只有一个会话，直接用它")
    if policy == "reuse":
        if sessions:
            sessions.sort(key=lambda s: -float(s.get("lastPromptAt") or s.get("updatedAt") or 0))
            return result("reuse", sessions[0]["sessionId"], "要求复用且没有更好的匹配 → 取最近活跃的")
        return result("new", None, "要求复用但名册为空 → 只能开新对话")
    return result("new", None,
                  f"没有匹配的工作区会话（名册 {len(sessions)} 个）→ 开新对话")


def record_decision(to: str, mail_id: str, decision: dict[str, Any], delivered: bool,
                    home: Path = DEFAULT_HOME) -> None:
    """把决策与投递结果记成流水，便于事后追"这封信为什么落在那儿"。"""
    data = load(home)
    data["decisions"].append({
        "at": time.time(), "to": to, "mail": mail_id, "delivered": bool(delivered),
        "mode": decision.get("mode"), "session_id": decision.get("session_id"),
        "policy": decision.get("policy"), "reason": decision.get("reason"),
        "context_id": decision.get("context_id"),
    })
    save(data, home)
