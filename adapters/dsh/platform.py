"""★DSH 平台语义★——ANP 一概不知道这一层。

按 Adapter Spec §1 与 SKILL 第 ⑤ 步，平台知识**只应出现在两个翻译函数里**：
`to_native(envelope)` 与 `from_native(platform_msg)`。本模块就是那两个函数，外加
DSH 自己的会话账本（Agent ↔ Session 的映射）。

**边界（越界即 bug）**：
  ✗ 不做投递决策（重试 / 退避 / 谁在线）—— 那是 Core 的事
  ✗ 不读别人的收件箱，不读 keys.json / ledger.jsonl
  ✗ 不 import Core 的 src/
  ✓ 只持有 DSH 侧状态；状态全丢后必须能从 Core 恢复

## DSH 的对象映射（Spec §8.5.8：Workspace / Session 不注册成 Agent）

```
ANP Agent「dsh」（一个身份，Core 只看得见它）
   └── DSH 侧 N 个 session / workspace     ← 本模块维护，Core 看不见
```

DSH 侧的一个"工作区"不是一个 ANP 身份，而是本 Adapter 自己的**投递坐标**。
Core 从来不理解 DSH 的 workspace，也不该理解。

## 平台侧状态放哪

放**插件自己的 `data/`**（`<plugin_dir>/data/`），**绝不写进信箱 `<home>` 根下**——
否则两套系统共管一个目录（这是 Spec §12 登记的缺口之一，已在 SKILL 踩坑第 14 条写明）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# 路径：平台状态只住插件自己的 data/
# --------------------------------------------------------------------------- #
_HERE = Path(__file__).resolve().parent
DATA = _HERE / "data"

# DSH 侧的待取信文件。**这是 DSH 平台的原生格式，不是 ANP 的信封格式**——
# 转换发生在 to_native / from_native 里。
PENDING_JSONL = DATA / "dsh-pending.jsonl"
PENDING_MD = DATA / "dsh-pending.md"
SESSIONS = DATA / "dsh-sessions.json"
OUTBOX = DATA / "dsh-outbox.jsonl"
WORK_STATE = DATA / "dsh-work.json"
_work_lock = threading.RLock()


def _ensure_dir() -> None:
    DATA.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# ① to_native：ANP 信封 → DSH 侧载荷
# --------------------------------------------------------------------------- #
def to_native(env) -> dict:
    """把一个 ANP 信封翻译成 DSH 侧能消费的载荷。

    这是**平台知识的唯一入口之一**。DSH 不是 HTTP Agent、也没有可寻址的会话端点，
    所以"递进平台"在真实部署里退化成**落一件 DSH 能看见的待办**：
    DSH 侧（本 harness 的插件/工具）读 `dsh-pending.*` 消费。

    ★注意这里刻意**没有**做会话归属的最终决定★——归属由 `route()` 决定，
    本函数只管把信封翻成平台字段。

    `meta` 里的坐标是 ANP 的公开约定（Protocol §2）：
        context_id / from_workspace / to_workspace
    """
    meta = dict(env.meta or {})
    return {
        # ---- DSH 侧字段（平台知识） ----
        "kind": "dsh.inbox_item",
        "text": env.body,                          # 会话里真正显示的那段字
        "sender": env.from_,
        "subject": env.subject,
        "urgent": env.kind == "message" and not env.reply_to,
        # ---- 回信必需的坐标：原样带着，**不许在这一层丢失** ----
        "anp_id": env.id,
        "anp_from": env.from_,
        "anp_to": env.to,
        "correlation_id": env.correlation_id,
        "reply_to": env.reply_to,
        "anp_kind": env.kind,
        # ---- 路由坐标（对话线是钥匙，工作区是坐标） ----
        "context_id": str(meta.get("context_id") or ""),
        "from_workspace": str(meta.get("from_workspace") or ""),
        "to_workspace": str(meta.get("to_workspace") or ""),
        "meta": meta,
        "received_at": time.time(),
    }


# --------------------------------------------------------------------------- #
# ② from_native：DSH 侧产出 → ANP 信封字段
# --------------------------------------------------------------------------- #
def from_native(platform_msg: dict) -> dict:
    """把 DSH 侧的产出翻译回 ANP 信封字段。

    ★`reply_to` 与 `correlation_id` 是对话线的全部依据★（SKILL 5.4）：
    不填 = 对方那封往来当场断线。`from_native` 的职责就是**把它们原样带回**，
    而不是让上层每次重新猜。

    入参是 DSH 侧的回复对象（至少要有 `text`，可选 `anp_id` / `correlation_id`）。
    """
    if not isinstance(platform_msg, dict):
        platform_msg = {"text": str(platform_msg)}
    text = str(platform_msg.get("text") or "")
    return {
        "body": text,
        "reply_to": str(platform_msg.get("anp_id") or
                        platform_msg.get("reply_to") or ""),
        "correlation_id": str(platform_msg.get("correlation_id") or ""),
        "kind": "reply",
        "subject": str(platform_msg.get("subject") or ""),
    }


# --------------------------------------------------------------------------- #
# ③ 会话 / 工作区映射（Adapter 内部对象，Core 看不见）
# --------------------------------------------------------------------------- #
STICKY_TTL = 3600.0        # 同一对话线多久之内算"同一个活跃会话"


class SessionBook:
    """ANP 对话线 → DSH 会话的映射账本。

    ★硬规则（SKILL 第 ③ 步）★：**Agent ↔ Workspace/Session 的映射由 Adapter 自己维护**，
    Core 不需要理解 DSH 的原生 ID。这里就是那份映射。

    持久化到 `data/dsh-sessions.json`。★丢了必须能恢复★：映射丢了只是退化成
    "每个对话线开一个新会话"，不会丢信——信封本身始终在 Core 里。
    """

    def __init__(self, path: Path = SESSIONS):
        self.path = path
        self.rows: dict[str, dict] = {}
        # ★并发重投/多线程 wake★：route 是读-改-写，必须串行，否则丢映射
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.rows = {str(k): dict(v) for k, v in raw.items()
                             if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError, ValueError):
            # 状态坏了不是错误——Core 才是唯一真相源，这里退化成空表即可。
            self.rows = {}

    def _save(self) -> None:
        _ensure_dir()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.rows, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def route(self, native: dict) -> dict:
        """执行 Core 盖的 `meta.session` 章；无章时才做本地映射回退。

        ★判定权在 Core（SKILL ⑤⑥ / Errata S1）★：
        Hub 发信时会写 `meta.session = {mode: reuse|new, session_id, reason, ...}`
        （字段名是 `mode`，不是 action）。本方法**只执行**，不再推导
        workspace 匹配 / session_policy / 多候选。

        - `mode=reuse` → 用章上的 `session_id`（没有则用本地对话线映射）并 `reused=True`
        - `mode=new`   → 开新 DSH 会话 id，`reused=False`
        - 无章         → 仅作执行回退：对话线粘性 / 开新；reason 标明 `no-stamp-*`

        返回 `{"session", "reused", "workspace", "reason"}`。
        """
        with self._lock:
            return self._route_locked(native)

    def _route_locked(self, native: dict) -> dict:
        """执行 Core 盖的 `meta.session` 章；无章时才做本地映射回退。

        ★判定权在 Core（SKILL ⑤⑥ / Errata S1）★：
        Hub 发信时会写 `meta.session = {mode: reuse|new, session_id, reason, ...}`
        （字段名是 `mode`，不是 action）。本方法**只执行**，不再推导
        workspace 匹配 / session_policy / 多候选。

        - `mode=reuse` → 用章上的 `session_id`（没有则用本地对话线映射）并 `reused=True`
        - `mode=new`   → 开新 DSH 会话 id，`reused=False`
        - 无章         → 仅作执行回退：对话线粘性 / 开新；reason 标明 `no-stamp-*`

        返回 `{"session", "reused", "workspace", "reason"}`。
        """
        meta = native.get("meta") if isinstance(native.get("meta"), dict) else {}
        stamp = meta.get("session") if isinstance(meta.get("session"), dict) else {}
        mode = str(stamp.get("mode") or stamp.get("action") or "").strip().lower()
        stamped_sid = str(stamp.get("session_id") or "").strip()
        line = native.get("correlation_id") or native.get("context_id") or ""
        ws = native.get("to_workspace") or native.get("from_workspace") or ""
        now = time.time()

        def _remember(sess: str, reused: bool) -> None:
            if line:
                row = self.rows.get(line) or {}
                row.update({"session": sess, "ts": now,
                            "hits": int(row.get("hits") or 0) + 1})
                if ws:
                    row["workspace"] = ws
                self.rows[line] = row
                self._save()

        def _fresh_session_id() -> str:
            return (f"dsh-sess-{int(now * 1000) % 10 ** 10}-"
                    f"{abs(hash(line or ws)) % 9973}")

        # ---- 照章执行：reuse ------------------------------------------------
        if mode == "reuse":
            sess = stamped_sid or (self.rows.get(line) or {}).get("session") or ""
            if sess:
                _remember(sess, True)
                return {"session": sess, "reused": True,
                        "workspace": (self.rows.get(line) or {}).get("workspace") or ws,
                        "reason": ("meta.session-reuse" if stamped_sid
                                   else "meta.session-reuse-local")}
            # 章要求 reuse 但给不出可复用 id：不猜，按执行回退开新并标明
            sess = _fresh_session_id()
            _remember(sess, False)
            return {"session": sess, "reused": False, "workspace": ws,
                    "reason": "meta.session-reuse-missing-id"}

        # ---- 照章执行：new --------------------------------------------------
        if mode == "new":
            sess = _fresh_session_id()
            _remember(sess, False)
            return {"session": sess, "reused": False, "workspace": ws,
                    "reason": "meta.session-new"}

        # ---- 无章：本地映射只作执行回退（仍不做策略推导） --------------------
        if line and line in self.rows:
            row = self.rows[line]
            fresh = (now - float(row.get("ts") or 0)) <= STICKY_TTL
            row.update({"ts": now, "hits": int(row.get("hits") or 0) + 1})
            if ws:
                row["workspace"] = ws
            self._save()
            return {"session": row.get("session") or "", "reused": True,
                    "workspace": row.get("workspace") or ws,
                    "reason": ("no-stamp-correlation-sticky" if fresh
                               else "no-stamp-correlation-stale-reuse")}

        sess = _fresh_session_id()
        if line:
            self.rows[line] = {"session": sess, "ts": now, "hits": 1,
                               "workspace": ws}
            self._save()
        return {"session": sess, "reused": False, "workspace": ws,
                "reason": "no-stamp-new-session" if line else "no-stamp-no-line"}

    def last_used(self) -> dict[str, float]:
        """给寻址用的 `last_used` 提示（★平台记录，不是名册的 last_seen★，Spec §8.6）。

        DSH 侧"最近真的被用过"的工作区时间戳。用来在真正并列时破并列，
        **绝不用它冒充心跳活跃度**。
        """
        out: dict[str, float] = {}
        for row in self.rows.values():
            ws = str(row.get("workspace") or "")
            if ws:
                out[ws] = max(out.get(ws, 0.0), float(row.get("ts") or 0))
        return out


# --------------------------------------------------------------------------- #
# ③b 按 env.id 幂等：同一信封只处理一次（H-6 / 并发重投）
# --------------------------------------------------------------------------- #
SEEN = DATA / "dsh-seen.jsonl"
_seen_lock = threading.RLock()


def already_seen(envelope_id: str) -> bool:
    eid = str(envelope_id or "").strip()
    if not eid:
        return False
    with _seen_lock:
        if not SEEN.is_file():
            return False
        try:
            for line in SEEN.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and str(row.get("envelope_id") or "") == eid:
                    return True
        except OSError:
            return False
    return False


def mark_seen(envelope_id: str) -> bool:
    """认领 env.id。返回 False = 此前已处理/已认领（真幂等，不得双跑）。

    ★失败要 unmark_seen★：Core 在 ok:false 时不会签收、会重投；
    若失败仍留在 seen 里，重投会被当成重复而永远丢掉。
    """
    eid = str(envelope_id or "").strip()
    if not eid:
        return True
    with _seen_lock:
        if already_seen(eid):
            return False
        _ensure_dir()
        with SEEN.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps({"envelope_id": eid, "at": time.time()},
                                ensure_ascii=False) + "\n")
    return True


def unmark_seen(envelope_id: str) -> None:
    """处理失败时释放认领，允许 Core 重投后再次处理。"""
    eid = str(envelope_id or "").strip()
    if not eid or not SEEN.is_file():
        return
    with _seen_lock:
        try:
            rows = []
            for line in SEEN.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and str(row.get("envelope_id") or "") == eid:
                    continue
                rows.append(line)
            tmp = SEEN.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(rows) + ("\n" if rows else ""),
                           encoding="utf-8")
            os.replace(tmp, SEEN)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# ④ 递平台：落一件 DSH 能看见的待办
# --------------------------------------------------------------------------- #
def deliver_to_platform(native: dict, route: dict) -> dict:
    """把翻译好的载荷真正递进 DSH 侧。

    真实部署里 DSH 的会话注入有两档：
      A 档 —— DSH 侧有常驻消费者（本 harness 的插件/工具）读 `dsh-pending.jsonl`
      B 档 —— 没有消费者时至少留下人能看见的 `dsh-pending.md`

    本函数**两档都写**：jsonl 给机器、md 给人。这不是"没接好"——
    它对应 Spec §4 里"平台怎么被叫醒"这一段，由 Adapter 自己定义。

    ★它不做投递决策★：写没写成功是这里的事实，重投与否是 Core 的事。
    """
    _ensure_dir()
    row = dict(native)
    row["session"] = route.get("session") or ""
    row["workspace"] = route.get("workspace") or ""
    row["route_reason"] = route.get("reason") or ""

    with PENDING_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    lines = [
        f"## ANP → DSH　{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['received_at']))}",
        "",
        f"- 来自：`{row['sender']}`　→　`{row['anp_to']}`",
        f"- 信封 id：`{row['anp_id']}`",
        f"- 对话线：`{row['correlation_id'] or '(无)'}`",
        f"- 会话：`{row['session']}`　工作区：`{row['workspace'] or '(未声明)'}`",
        f"- 路由：{row['route_reason']}",
        "",
        row["text"],
        "",
        "---",
        "",
    ]
    with PENDING_MD.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return {"ok": True, "session": row["session"], "jsonl": str(PENDING_JSONL),
            "md": str(PENDING_MD)}


def read_pending(limit: int = 200, peek: bool = True) -> list[dict]:
    """读 DSH 侧的待办（供 DSH 侧消费 / 验收用）。

    `peek=False` 会把已读的行搬走——**但这只是 DSH 侧的本地游标**，
    它**不代替 Core 签收**（Adapter 禁止代 Core 签收，SKILL ⓪）。
    """
    if not PENDING_JSONL.is_file():
        return []
    rows: list[dict] = []
    for line in PENDING_JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows = rows[-limit:]
    if not peek:
        _ensure_dir()
        PENDING_JSONL.write_text("", encoding="utf-8")
    return rows


def _load_work_state_locked() -> dict[str, dict]:
    try:
        raw = json.loads(WORK_STATE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): dict(v) for k, v in raw.items()
                    if isinstance(v, dict)}
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def _save_work_state_locked(state: dict[str, dict]) -> None:
    _ensure_dir()
    tmp = WORK_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, WORK_STATE)


def work_state() -> dict[str, dict]:
    """返回 Adapter 自己的 DSH 消费账本快照。"""
    with _work_lock:
        return _load_work_state_locked()


def queue_work(native: dict, route: dict) -> dict:
    """为一封已持久化的待办建立幂等消费记录。"""
    eid = str(native.get("anp_id") or "").strip()
    if not eid:
        return {}
    with _work_lock:
        state = _load_work_state_locked()
        row = state.get(eid)
        if row and row.get("status") in {"running", "replied", "failed", "notice"}:
            return dict(row)
        now = time.time()
        row = {
            "envelope_id": eid,
            "status": "queued",
            "attempts": int((row or {}).get("attempts") or 0),
            "from": str(native.get("anp_from") or ""),
            "to": str(native.get("anp_to") or ""),
            "correlation_id": str(native.get("correlation_id") or ""),
            "need_reply": (native.get("meta") or {}).get("need_reply") is True,
            "received_at": float(native.get("received_at") or now),
            "queued_at": now,
            "updated_at": now,
        }
        state[eid] = row
        _save_work_state_locked(state)
        return dict(row)


def claim_work(envelope_id: str, *, current_after: float,
               stale_after: float = 600.0) -> dict | None:
    """原子认领 queued/超时 running/可重试 failed 任务。"""
    eid = str(envelope_id or "").strip()
    if not eid:
        return None
    now = time.time()
    with _work_lock:
        state = _load_work_state_locked()
        row = state.get(eid)
        if not row:
            return None
        status = str(row.get("status") or "")
        received_at = float(row.get("received_at") or 0)
        updated_at = float(row.get("updated_at") or 0)
        current = received_at >= current_after
        recoverable = (
            status == "running" and now - updated_at >= stale_after
        )
        # 失败已经发过补偿回信，不在同一进程里自动重复回信。
        # 只有崩溃留下的 running 才由消费者自动恢复；失败任务由人工/运维重置。
        retryable = False
        if status == "queued" and not current:
            return None
        if status == "running" and not recoverable:
            return None
        if status == "failed" and not retryable:
            return None
        if status in {"replied", "notice"}:
            return None
        row = dict(row)
        row.update({
            "status": "running",
            "attempts": int(row.get("attempts") or 0) + 1,
            "started_at": now,
            "updated_at": now,
        })
        state[eid] = row
        _save_work_state_locked(state)
        return row


def finish_work(envelope_id: str, status: str, **fields) -> dict:
    """写入 queued/running/replied/failed/notice 等消费结果。"""
    eid = str(envelope_id or "").strip()
    if not eid:
        return {}
    with _work_lock:
        state = _load_work_state_locked()
        row = dict(state.get(eid) or {"envelope_id": eid})
        row.update(fields)
        row["status"] = str(status)
        row["updated_at"] = time.time()
        state[eid] = row
        _save_work_state_locked(state)
        return row


def record_outbox(entry: dict) -> None:
    """DSH 侧产出的回复在发回 Core 之前先落一笔本地账（便于验收与排障）。"""
    _ensure_dir()
    with OUTBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# ⑤ 凭证箱：Adapter 自己的状态，必须能扛住进程重启
# --------------------------------------------------------------------------- #
CREDENTIALS = DATA / "dsh-credentials.json"
STATUS = DATA / "dsh-status.json"


def write_status(body: dict) -> None:
    """把 Adapter 的运行状态落盘（验收与排障读它）。

    ★这里只写 Adapter 自己的 `data/`，不碰信箱 `<home>` 下任何文件★
    （Spec §12 登记的缺口：平台状态不该与 Core 共管一个目录）。
    """
    _ensure_dir()
    tmp = STATUS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, STATUS)


def read_status() -> dict:
    try:
        raw = json.loads(STATUS.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def load_credentials() -> dict:
    """读回本 Adapter 代管身份持有的令牌。

    ★为什么这是 Adapter 的合法状态★：Spec 明文写着 **"Adapter 持有它管理的所有
    agent 的 agent token"**（§2 令牌由谁持有）。而 Core 每次拉起插件都分配
    **随机新端口**，插件重启后必须用**旧令牌**去 `/heartbeat` 续约才能把 wake
    端点更新到新端口；否则它会以"新身份"去 bootstrap，撞上 P0-2 的
    「已有 name + 无密钥 → 401」，**wake 就永远停在死掉的旧端口上**——
    表现为"注册成功但信永远投不进来"（实测踩到）。

    ★边界★：这里**只**存本 Adapter 代管身份自己的令牌，
    绝不是 hub signing secret，也不是别人的令牌（Spec §6.3 硬边界）。
    文件权限按本机用户目录处理，与 adapter 令牌的信任假设一致。
    """
    try:
        raw = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {
                "agent_key": str(raw.get("agent_key") or ""),
                "plugin_id": str(raw.get("plugin_id") or ""),
                "manage_agent": str(raw.get("manage_agent") or ""),
                "saved_at": float(raw.get("saved_at") or 0),
            }
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return {}


def save_credentials(*, agent_key: str, plugin_id: str,
                     manage_agent: str) -> None:
    """把代管身份的令牌落盘，供重启后恢复（只存自己代管的这一枚）。"""
    if not agent_key:
        return
    _ensure_dir()
    tmp = CREDENTIALS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "agent_key": agent_key,
        "plugin_id": plugin_id,
        "manage_agent": manage_agent,
        "saved_at": time.time(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CREDENTIALS)
