"""AgentNetPort hub：常驻信箱中枢。

职责
- 收件：POST /send 落盘到收件人收件箱，然后按登记信息唤醒收件人。
- 唤醒端点登记：POST /heartbeat（DSH 桥接插件启动时把 /mail/wake 地址报上来）。
- 给 MCP 数据面用：POST /tool（mail_send / mail_inbox / mail_wait / ...）。
- 观测：GET /health、GET /agents。

零依赖，只用标准库。默认监听 127.0.0.1:8900。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anp_core as core  # noqa: E402
import anp_plugin as plugin  # noqa: E402
import anp_policy as policy  # noqa: E402
import anp_sessions as sessions  # noqa: E402

HOME = core.DEFAULT_HOME
# 日志走异步队列：写不进就丢，绝不让"日志写满管道"卡住整个服务（见 core.AsyncLog）
LOG = core.AsyncLog()
STORE_LOCK = threading.Lock()
# 请求体体积上限（字节）：挡住"用一个巨大的 content-length 把 hub 顶到 OOM"。
MAX_BODY_BYTES = int(os.environ.get("AGENT_ANP_MAX_BODY_BYTES") or 4 * 1024 * 1024)
# 请求体读取单独设短超时：合法但慢速的客户端不能无限占住一个线程。
BODY_READ_TIMEOUT = max(1.0, min(float(os.environ.get("AGENT_ANP_BODY_READ_TIMEOUT") or 10.0), 120.0))
# 在途请求计数（优雅停止时等它归零）
INFLIGHT_LOCK = threading.Lock()
INFLIGHT: dict[str, Any] = {"n": 0, "draining": False, "cv": threading.Condition(INFLIGHT_LOCK)}
# 长轮询名额（do_wait）：一个 wait 最长挂 900 秒，不限流就能把线程全钉住
WAIT_LOCK = threading.Lock()
WAIT_STATE: dict[str, Any] = {"n": 0, "waited": 0}
WAIT_MAX = max(1, int(os.environ.get("AGENT_ANP_WAIT_MAX") or 32))
# 身份令牌的使用统计（可观测性）：谁在带密钥、谁被拒过。`/health` 里可见。
AUTH_LOCK = threading.Lock()
AUTH_STATS: dict[str, Any] = {"at": None, "total": 0, "by_identity": {}}

# 插件注册表的内存镜像 + 操作互斥。
# ★为什么不直接每次读盘★：load/unload 要改进程状态（拉起/终止），读-改-写必须串行，
# 否则两次 reload 会各拉一个进程。磁盘是落盘真相，内存只是缓存。
# hub 自己的端口：register 时随 config 交给插件，插件才能拼出 "怎么回call Core"。
# 插件没有别的办法知道这件事——端口是 Core 分配的（spec §3.2）。
# ★默认 8900★（2026-09-26 审计后统一）：README / SDK / core.DEFAULT_HUB / 看板默认值
# 本来全是 8900，只有 hub 自己（这里 + argparse + spawn_hub 回落）写着 8899——
# "按 README 直接起、然后访问 8900"必然连不上。契约只能有一个默认值。
HUB_PORT = 8900
PLUGIN_LOCK = threading.RLock()
PLUGINS: dict[str, dict[str, Any]] = {}
# 插件事件总线（spec §6）。旁路：入队即返回，绝不阻塞消息路径。
EVENTS = plugin.EventBus(HOME)
# 上一次扫到的在线集合（用于 agent.online / agent.offline 跃迁检测）
_ONLINE_CACHE: set[str] = set()
# 自动重投状态（`/health` 可见）
AUTO_STATE: dict[str, Any] = {"enabled": False, "interval": None, "limit": None, "last": None}
# 插件预加载是否跑完（`/health.preload_done` 可见）。★P0-1（2026-09-23）★：
# serve 线程先于 preload 启动，所以"HTTP 通"不再蕴含"插件就绪"，
# 必须有个独立信号，否则启动初期 /tools、/admin/panel 会短暂返回空。
PRELOAD_STATE: dict[str, Any] = {"done": False, "attempted": 0, "loaded": 0}
# 数据维护状态（`/health` 可见）
MAINT_STATE: dict[str, Any] = {"enabled": False, "interval": None, "last": None}
# 密度上限日志节流：每多少秒最多打一条（见 rate_guard 里"管道写满会阻塞"的说明）
RATE_LOG_EVERY = 10.0
RATE_LOG: dict[str, Any] = {"at": 0.0, "suppressed": 0}
# 伴生看板进程（hub 起、hub 停）
WEBUI_PROC: dict[str, Any] = {"proc": None}


# --------------------------------------------------------------------------- #
# 业务
# --------------------------------------------------------------------------- #
def agent_view(home: Path | None = None) -> list[dict[str, Any]]:
    home = HOME if home is None else Path(home)
    table = core.agent_table(home)
    for name in core.known_inboxes(home):
        table.setdefault(name, {"name": name})
    rows = []
    for name, entry in sorted(table.items()):
        msgs = core.read_records(name, home)
        rows.append({
            "name": name,
            "wake": entry.get("wake"),
            # 进程健康 API（mailbox-register skill：WebUI 的进程灯以它为准）。
            # 只透传、不探测——探测是 WebUI 的事，hub 不替它做决定。
            "process_api": entry.get("process_api"),
            # ★平台能力★（能不能注入会话/起进程/续接）——一直存在账本里，直到 2026-09-22
            # 才在这里暴露出去（`agent_view` 原来只挑固定几个字段，caps 被丢在读取侧）。
            "caps": entry.get("caps"),
            # ★任务能力★：身份自报的自由词表（coding / analysis / vision…）。
            # Core 只存、只返回，**不解释、不匹配、不调度**。
            "skills": entry.get("skills") or [],
            "description": entry.get("description"),
            "last_seen": entry.get("ts"),
            "messages": len(msgs),
            "unread": sum(1 for m in msgs if not m.get("read")),
        })
    return rows


class BadBody(Exception):
    """请求体无法按 UTF-8 解码 —— 交给 handler 回 400（loud failure）。"""


def recipients_of(args: dict[str, Any], sender: str) -> tuple[list[str], str, bool]:
    """解析收件人：支持 `to="codex"` 与 `to=["codex","mimo"]` 两种写法。

    返回 `(收件人列表, 解析出的对话线, 是否多人信)`。
    ★多人信语义（主人 2026-09-21 定）★：**同一封信的 N 份副本**——共享 message id 与 context_id，
    每人各有一条收件箱记录，签收/投递/重投**各自独立**；回信靠 `reply_to=<这个 id>` 回到发件人，
    "谁回的"看回信信封的 from。

    收件人解析本体在 `core.recipients_of`（降级路径也要用同一份，别在这里再写一遍）。
    """
    out = core.recipients_of(args.get("to"), sender)
    given = args.get("meta") if isinstance(args.get("meta"), dict) else {}
    group = str(given.get("context_id") or "").strip()
    return out, group, len(out) > 1


def rate_guard(to: str, meta: dict[str, Any]) -> dict[str, Any] | None:
    """对话线消息密度上限：挡住了再落盘。返回 None = 放行，否则返回错误响应。

    ★为什么必须有★（2026-09-22）：`send()` 原本没有任何 hop / TTL / 循环判断，
    默认 `wake=true`，于是 A→B→A→B… 与 A→B→C→A… 可以无限互相唤醒，每跳都烧 token。
    现有的"单封重试 5 次"只管投递失败，管不了 agent 之间的来回。
    判据是"同一个收件箱在最近 N 秒里收到了多少信"——每一跳都会落进某个人的箱子，
    所以任何回路必然在至少一个箱里堆积，一定被兜住。规则与逃生阀见 `core.context_rate_exceeded`。
    """
    if meta.get("rate_bypass"):
        return None
    cid = str(meta.get("context_id") or "").strip()
    try:
        exceeded, info = core.context_rate_exceeded(to, cid, HOME)
    except Exception as e:  # noqa: BLE001 - 计数失败不能挡住正常发信：放行 + 记日志
        LOG.write(f"[hub] 密度上限计数失败（已放行）: {type(e).__name__}: {e}\n")
        return None
    if not exceeded:
        return None
    # ★日志要短、要节流★（2026-09-22 实测踩到）：以前这里每次拒绝都打一条四百字节的
    # 中文详情，而 hub 的 stderr 常常被调用方重定向进一个**没人读的管道**——Windows 匿名
    # 管道缓冲区只有几 KB，写满之后 `sys.stderr.write` 会**阻塞**，于是整个 hub 停止响应
    # （连 /health 都不答，看起来像死锁）。详情已经在返回给调用方的 error 里，这里只留一行短
    # 摘要、且每 RATE_LOG_EVERY 秒最多一条，被压缩的次数记在 suppressed 里。
    now = time.time()
    RATE_LOG["suppressed"] += 1
    if now - float(RATE_LOG.get("at") or 0) >= RATE_LOG_EVERY:
        LOG.write(f"[hub] 密度上限：拒绝 → {to}（对话线 {info['ctx_hits']}/{info['ctx_limit']}、"
                         f"全箱 {info['any_hits']}/{info['any_limit']}，{info['window_sec']:.0f}s 窗；"
                         f"另压缩 {RATE_LOG['suppressed']} 条）"
                         f"确认是正常长任务请在 meta 里带 rate_bypass=true\n")
        RATE_LOG["at"] = now
        RATE_LOG["suppressed"] = 0
    return {
        "ok": False,
        "error": (f"发往 {to} 被拒：同一对话线在最近 {info['window_sec']:.0f} 秒内已有 "
                  f"{info['ctx_hits']} 封（上限 {info['ctx_limit']}），"
                  f"或该收件箱同期共收到 {info['any_hits']} 封（上限 {info['any_limit']}）。"
                  "疑似 agent 之间无限互刷；确认是正常长任务请在 meta 里带 rate_bypass=true。"),
        "rate_limited": info,
    }


def send_permission_guard(sender: str, targets: list[str],
                          wake: bool) -> dict[str, Any] | None:
    """插件出站闸（spec §5：manifest 的权限在调用点查表，不在表里 → permission.denied）。

    ★为什么以前是空转★（2026-09-26 外部审计复现）：权限闭集只有 `webui.panel` 在
    `/admin/panel` 有实际检查，`wake.others` / `broadcast` / `agents.discover` /
    `tools.register` 一个执行点都没有——`permissions: []` 的插件照样能唤起别人、
    能广播、能把工具塞进 agent 可见的 API。声明了却不执行，比没有声明更坏：
    读 manifest 的人会以为边界存在。

    ★只对已加载插件身份生效★：插件是唯一在 manifest 里声明权限的一类，所以也只有
    它能被查表。各 Adapter 代管的 agent / MCP 客户端是普通身份，不受影响——
    否则"收紧插件权限"会顺手把整个接入生态打断（这是本函数刻意划的界）。

    两个判据（都是**意图**，与收件人是不是 queue 档无关——钱包在"想唤起"时就花了）：
      · `wake.others`：出站 `wake=true` —— 一个 wake=true 就是一次可能的进程拉起；
      · `broadcast`：`to` 给了多个收件人 —— spec 里"多收件人即广播"。
    """
    perms = plugin.permissions_of(sender, HOME)
    if perms is None:            # 不是已加载插件：不参与插件权限查表
        return None
    if wake and "wake.others" not in perms:
        return {"ok": False, "code": "permission.denied", "permission": "wake.others",
                "error": f"插件 {sender} 没有 wake.others 权限，不能以 wake=true 唤起别的 agent",
                "hint": f"要放行：在 {sender} 的 manifest.json 的 permissions 里加上 "
                        f"wake.others，然后 reload 这个插件"}
    if len(targets) > 1 and "broadcast" not in perms:
        return {"ok": False, "code": "permission.denied", "permission": "broadcast",
                "error": (f"插件 {sender} 没有 broadcast 权限，不能一次发给多个收件人"
                          f"（{len(targets)} 个：{targets}）"),
                "hint": f"要放行：在 {sender} 的 manifest.json 的 permissions 里加上 "
                        f"broadcast，然后 reload 这个插件"}
    return None


def do_send(sender: str, args: dict[str, Any]) -> dict[str, Any]:
    home = HOME   # ★home 一律取服务端的★：请求体指定 home 是已堵过的
    # `x-mailbox-home` 漏洞的另一个入口（2026-09-22 收紧）
    meta = dict(args.get("meta")) if isinstance(args.get("meta"), dict) else {}

    targets, context_id, is_group = recipients_of(args, sender)
    if not targets:
        return {"ok": False, "error": "收件人为空（或只写了发送方自己）：至少给一个别的身份"}
    # ★插件权限闸在任何副作用之前★（查表失败/拒绝都不该留下半封信）
    denied = send_permission_guard(sender, targets, bool(args.get("wake", True)))
    if denied:
        return denied
    if is_group:
        # 多人信：每次收件人各自决策/投递（会话可能完全不同），但共享同一条对话线。
        if context_id:
            meta["context_id"] = context_id
        return send_group(sender, targets, args, meta, home)

    # ★信箱作出会话决策★（协议 §2.2）：怎么发、发给谁、要不要开新对话由信箱说了算，
    # agent 只在 meta.session_policy 里表达诉求，收信通道照章执行。
    to = targets[0]
    decision = sessions.decide(to, meta, home)
    if decision:
        meta["session"] = decision          # 盖章给收信通道执行
        meta.setdefault("context_id", decision.get("context_id"))

    blocked = rate_guard(to, meta)
    if blocked:
        return blocked
    allowed, why = policy.may_send_to(sender, to, HOME)
    if not allowed:
        return {"ok": False, "error": f"不允许发给 {to}：{why}",
                "hint": "要放开：把 permissions.json 的 send_to.mode 设成 open，"
                        f"或给 {sender} 的名单里加上 {to}（或 \"*\"）",
                "policy": policy.summary(HOME)}

    rec = core.send(
        sender,
        to,
        args.get("body") or "",
        subject=args.get("subject"),
        reply_to=args.get("reply_to"),
        kind=str(args.get("kind") or "message"),
        correlation_id=args.get("correlation_id") or meta.get("correlation_id"),
        meta=meta,
        need_reply=bool(args.get("need_reply")),
        home=home,
    )
    result: dict[str, Any] = {"ok": True, "mode": "hub", "message": rec}
    if decision:
        result["session_decision"] = decision
    # 投递走**与多人信同一段代码**（deliver_one）：占位 → 唤醒/静默 → 记账 → 签收 → 记决策。
    # 曾经这里和群发各写一遍，结果两边的语义漂了（双投递、wake=false 被重投叫醒、签收漏记）。
    got = deliver_one(sender, rec, args, home, decision)
    result["wake"] = got["wake"]
    if got.get("cached"):
        result["cached"] = True
    if got.get("signed_off"):
        result["signed_off"] = got["signed_off"]
    if rec.get("reply_to"):
        # 原信躺在"原信的收件人"箱里 —— 回复方 = sender，所以原信 owner 就是 sender。
        core.mark_replied(sender, str(rec["reply_to"]), str(rec["id"]), home)
    return result


def deliver_one(sender: str, rec: dict[str, Any], args: dict[str, Any], home: Path,
                decision: dict[str, Any] | None = None) -> dict[str, Any]:
    """投递**一份副本**（单人信与多人信共用）：占位 → 唤醒/静默 → 记账 → 签收 → 记决策。

    单人路径与多人路径必须走同一段代码，否则两边的语义一定会漂——
    实测过的双投递、`wake=false` 被重投叫醒、签收漏记，全是"两条路径各写一遍"造成的。
    """
    outcome: dict[str, Any] = {"ok": False, "error": "未执行唤醒"}
    delivered_ids: list[str] = []
    skipped = False        # 发送方说不唤醒（wake=false）
    no_push = False        # 收件人是 queue 档，没有 push 目标（P0-0）
    result_extra_cache = False
    to = str(rec.get("to") or "")
    mid = str(rec.get("id") or "")
    entry = None           # ★必须在 try 外初始化★：wake=false 分支不会赋值它，
    #                      而 finally 要读它判 exec 档。晚了这个会让所有 wake=false
    #                      的发送方吃一个 UnboundLocalError 400（D1 测试实测抓到）。
    try:
        if args.get("wake", True):
            entry = core.agent_table(home).get(to)
            # ★P0-0★：收件人是 queue 档（没有 push 目标）时，消息落盘即为终态。
            # 这不是失败、不排重投、不触发 message.undelivered——把它记成失败会让
            # 自动重投白试 5 次再误报一次"信死了"。
            if entry and core.delivery_mode(entry) == "queue":
                no_push = True
                outcome = {"ok": False, "kind": "queue",
                           "error": "收件人是 queue 档（仅落盘，等待它主动来取）"}
            elif entry:
                # ★投递前先占位（租约）★：自动重投的扫描线程只看"未投递+到期"，
                # 看不见"正在飞"的信；不占位就会同一封信投两遍
                # （2026-09-20 实测复现：慢收信端 3 秒，扫描 0.8 秒插一脚 → 唤醒 2 次）。
                core.claim_lease(to, {mid}, home, by="send")
                # ★把收件人的身份令牌随投递一起捎上★（协议 §3.1）：
                # 信箱是唯一知道密钥的地方，收信端据此校验"这封信确实来自信箱"。
                outcome = core.wake(entry, rec, key=core.key_for(to, home), home=home)
                delivered_ids = wake_sign_off_ids(outcome, rec)
            else:
                outcome = {"ok": False, "kind": "none", "error": f"{to} 还没登记过唤醒端点"}
        else:
            # ★发送方明确说了不唤醒★（缓存信）：这不是投递失败，别排重投——否则自动重投到期后
            # 照样会把收件人叫醒，直接违背发信方意图（2026-09-20 审阅实测过）。
            skipped = True
            outcome = {"ok": False, "kind": "none", "error": "发送方指定不唤醒（缓存信）"}
    finally:
        # ★甲（D1 修复，2026-09-23）★：exec 档的 `ok` 只代表 Popen 成功，
        # 不等于信已送达。所以 exec 走 `spawned` 通道——记 spawned_at / wake_pid、
        # 给 ack 留重投窗口，但**不写 delivered_at**；delivered_at 留给唤醒器
        # 真的签收时写（sign_off）。
        # http 档不变：它的 ok 已经过 wake_sign_off_ids 的 mode 签收门（B1）。
        is_spawn = (core.delivery_mode(entry) == "exec") if entry else False
        core.record_delivery(
            to,
            {} if (skipped or no_push or is_spawn) else {mid: bool(outcome.get("ok"))},
            {} if (skipped or no_push or is_spawn) else
            {mid: str(outcome.get("error") or outcome.get("kind") or "唤醒失败")},
            home,
            skipped={mid} if skipped else None,
            no_push={mid} if no_push else None,
            spawned={mid: {"pid": outcome.get("pid")}} if is_spawn else None,
        )
        # 事件：这一份副本真的投出去了（spec §6）。旁路入队，失败不影响投递本身。
        if bool(outcome.get("ok")):
            EVENTS.emit("message.delivered", id=mid, to=to, from_=str(rec.get("from") or ""),
                        kind=str(rec.get("kind") or "message"),
                        correlation_id=rec.get("correlation_id"))
        # ★信箱自己签收★（协议 §4.2.2）：http 档"投进会话队列"就算这封信落地了。
        if delivered_ids:
            core.sign_off(to, set(delivered_ids), home, by="wake:http-acked")
        elif skipped:
            # ★缓存信也要签收★（主人 2026-09-21 指出："这个缓存信也没有自动已读功能"）：
            # 它是**送达到了的**（安静送达），只是没叫醒人。留着 read=false 会让它永远挂在"未读"里，
            # 而"未读"的语义是"有一封信在等着被推送/处理"——缓存信不是那种待办。
            # 标 read 但用 read_by 说明缘由，`wake_skipped` 保留"未被唤醒"这个事实。
            core.sign_off(to, {mid}, home, by="cache:silent-delivery")
            result_extra_cache = True
        if decision:
            sessions.record_decision(to, mid, decision, bool(outcome.get("ok")), home)
    return {"to": to, "id": mid, "ok": bool(outcome.get("ok")),
            "wake": outcome, "cached": skipped,
            "signed_off": len(delivered_ids) + (1 if result_extra_cache else 0),
            "session_decision": decision}


def send_group(sender: str, targets: list[str], args: dict[str, Any], meta: dict[str, Any],
               home: Path) -> dict[str, Any]:
    """多人信：**同一封信的 N 份副本**（同 id、同对话线、各自独立投递与签收）。

    实现要点：每份副本单独跑 `sessions.decide`（各人的会话可能完全不同），
    但共享 message id 与 context_id；`meta.bcc_group` 记下全体收件人，便于信封上显示"发给谁"。
    """
    context_id = str(meta.get("context_id") or "").strip()
    results: list[dict[str, Any]] = []
    first: dict[str, Any] | None = None
    # ★同一封信共用同一个 message id★（主人 2026-09-21 定）：先定 id，再给每人各落一份副本。
    shared_id = core.message_id()
    for name in targets:
        # 每份副本单独决策（各自的工作区/会话都可能不同）
        per_meta = {**meta, "to_all": list(targets)}
        if context_id:
            per_meta["context_id"] = context_id
        decision = sessions.decide(name, per_meta, home)
        per_meta["session"] = decision
        per_meta.setdefault("context_id", decision.get("context_id"))
        try:
            allowed, why = policy.may_send_to(sender, name, HOME)
            if not allowed:
                results.append({"to": name, "ok": False,
                                "error": f"不允许发给 {name}：{why}"})
                continue
            blocked = rate_guard(name, per_meta)
            if blocked:
                blocked["to"] = name
                results.append(blocked)
                continue
            rec = core.send(
                sender, name, args.get("body") or "",
                subject=args.get("subject"), reply_to=args.get("reply_to"),
                kind=str(args.get("kind") or "message"),
                correlation_id=args.get("correlation_id") or per_meta.get("correlation_id"),
                meta=per_meta,
                need_reply=bool(args.get("need_reply")), home=home,
                id=shared_id,
            )
        except Exception as e:  # noqa: BLE001
            results.append({"to": name, "ok": False, "error": f"{type(e).__name__}: {e}"})
            continue
        first = first or rec
        got = deliver_one(sender, rec, args, home, decision)
        got["reached"] = str(per_meta.get("context_id") or "")
        results.append(got)
    if first and first.get("reply_to"):
        core.mark_replied(sender, str(first["reply_to"]), str(first["id"]), home)
    delivered = [r for r in results if r.get("ok")]
    cached = [r for r in results if r.get("cached")]
    return {
        "ok": bool(delivered) or bool(cached),
        "mode": "hub",
        "group": True,                      # 多人信（同一封信的多份副本）
        "id": (first or {}).get("id"),      # ★共享的 message id★
        "message": first,                   # 向后兼容：给第一份副本
        "context_id": (first or {}).get("meta", {}).get("context_id"),
        "recipients": targets,
        "delivered": len(delivered),
        "cached": len(cached),
        "results": results,
        "note": "同一封信的多份副本：共享 message id 与 context_id；每人签收/投递/重投各自独立；"
                "回信带 reply_to=<这个 id>，谁回的看回信信封 from",
    }


def do_redeliver(sender: str, args: dict[str, Any]) -> dict[str, Any]:
    """把一封（或某收件人全部待投递的）信再投一次。"""
    home = HOME
    target_id = str(args.get("id") or "").strip()
    to = str(args.get("to") or "").strip()
    rows: list[tuple[str, dict[str, Any]]] = []
    if target_id:
        found = core.find_message(target_id, home)
        if not found:
            return {"ok": False, "error": f"找不到消息 {target_id}"}
        rec, owner = found
        rows.append((owner, rec))
    else:
        # 重投某收件人的全部待投递信：以"从未投递成功"为准，没排重投的历史信也算。
        for item in core.pending_messages(home, to, undelivered_only=True):
            owner = str(item.pop("inbox", ""))
            rows.append((owner, item))

    results = []
    for owner, rec in rows:
        # ★归属校验★（2026-09-22 收紧）：`find_message` / `pending_messages` 都是跨箱检索，
        # 以前任意 agent 都能拿别人的消息 id 反复重投——每投一次就把对方唤醒一次，
        # 是刷屏 + 白烧 token 的口子。现在只能重投自己收件箱里的信（hub 内部例外）。
        if owner != sender and sender != "hub":
            results.append({"id": rec.get("id"), "to": rec.get("to"), "ok": False,
                            "error": "只能重投自己收件箱里的信（跨箱重投已拒绝）"})
            continue
        entry = core.agent_table(home).get(rec.get("to") or owner)
        if not entry:
            core.record_delivery(owner, {str(rec.get("id")): False},
                                 {str(rec.get("id")): "收件人未登记唤醒端点"}, home)
            results.append({"id": rec.get("id"), "ok": False, "error": "收件人未登记唤醒端点"})
            continue
        # ★P0-0★：queue 档收件人没有 push 目标，重投它永远不可能成功。
        # 按终态处理（wake_skipped="no_push_target"），不重投、不算失败。
        if core.delivery_mode(entry) == "queue":
            core.record_delivery(owner, {}, {}, home,
                                 no_push={str(rec.get("id"))})
            results.append({"id": rec.get("id"), "to": rec.get("to"), "ok": False,
                            "wake": {"ok": False, "kind": "queue",
                                     "error": "收件人是 queue 档，仅落盘"},
                            "no_push": True})
            continue
        # 重投前也占位：避免"重投 A 还在飞，扫描线程又捡到 A"这种自撞
        core.claim_lease(owner, {str(rec.get("id"))}, home, by="redeliver")
        woke = core.wake(entry, rec, key=core.key_for(rec.get("to") or owner, home),
                         home=home)
        # ★甲（D1 修复）★：和 deliver_one 用同一套判据，不能只改一处——
        # exec 重投同样只算 "spawned"，否则第二投就把 delivered_at 写上，
        # 这封信立刻被 auto_redeliver 的 undelivered_only 排除，永远停在第 2 次。
        is_spawn = core.delivery_mode(entry) == "exec"
        if is_spawn:
            core.record_delivery(owner, {}, {}, home,
                                 spawned={str(rec.get("id")): {"pid": woke.get("pid")}})
        else:
            core.record_delivery(owner, {str(rec.get("id")): bool(woke.get("ok"))},
                                 {str(rec.get("id")): str(woke.get("error")
                                                          or woke.get("kind")
                                                          or "唤醒失败")}, home)
        results.append({"id": rec.get("id"), "to": rec.get("to"),
                        "ok": bool(woke.get("ok")), "wake": woke})
    # ★ok 要说实话★：以前这里恒为 True，于是"全部被归属校验挡下"或"唤醒全失败"
    # 也报 ok=True——调用方（含 hub 自己的自动重投统计）会把失败当成成功。
    # 语义改为"这一轮**有没有真的投出去至少一封**"。
    return {"ok": any(r.get("ok") for r in results) if results else False,
            "mode": "hub", "redelivered": len(results), "results": results}


def do_pending(sender: str, args: dict[str, Any]) -> dict[str, Any]:
    """列出还没被消费的信：未读 + 投递状态 + 是否到重投时间。

    ★只能看自己那一箱★（2026-09-22 收紧）：以前 `name=` 可以指到任意身份，
    于是任何 agent 都能读到别人的收发件人 / 主题 / 时间戳（通信频率与关系）。
    要"全信箱视角"请用看板（`/api/state`，已上管理员令牌）。
    """
    home = HOME
    name = str(args.get("name") or "").strip()
    if name and name != sender and sender != "hub":
        return {"ok": False, "mode": "hub",
                "error": f"只能查自己的收件箱（你要查的 {name} 不是你自己）；"
                         "要看全信箱请用看板 /api/state"}
    due_only = bool(args.get("due_only"))
    # 不传 name 时也只列自己：全局待办是管理员视角，不是每个 agent 都该有的
    items = core.pending_messages(home, name or sender, due_only=due_only)
    slim = [{
        "inbox": item.get("inbox"),
        "id": item.get("id"),
        "from": item.get("from"),
        "to": item.get("to"),
        "subject": item.get("subject"),
        "ts": item.get("ts"),
        "attempts": item.get("attempts", 0),
        "delivered_at": item.get("delivered_at"),
        "next_retry_at": item.get("next_retry_at"),
        "last_error": item.get("last_error"),
    } for item in items]
    return {"ok": True, "mode": "hub", "count": len(slim), "due_only": due_only, "messages": slim}


def do_inbox(sender: str, args: dict[str, Any]) -> dict[str, Any]:
    """读收件箱。★默认"读了就签收"★（协议 §4.2.2）。

    为什么要改默认值（主人 2026-09-20）：旧默认是"不显式 ack 就一直未读"，
    但 agent 在唤醒会话里最不可靠的就是"记得做收尾动作"，于是未读常年挂着、
    谁也不知道是真没处理还是处理完忘了标。现在：
      - `peek=false`（默认）＝真读，读到的当场签收（记 read_by=mail_inbox）；
      - `peek=true` ＝ 只看不改，供唤醒器/巡检用（正文仍然留在收件箱里）。

    ★过滤参数（2026-09-22 加）★：插件要"只收我这条游戏线里的投票"，以前只能整箱读回
    来自己筛。过滤器只让结果**更窄**，签收口径不变——签的就是你真正读到的那几封。
    """
    home = HOME   # ★home 一律取服务端的★：请求体指定 home 是已堵过的
    # `x-mailbox-home` 漏洞的另一个入口（2026-09-22 收紧）
    rows = core.read_records(sender, home)
    unread_only = args.get("unread_only", True)
    peek = bool(args.get("peek"))
    mark = (not peek) and args.get("mark_read", True)
    picked = [r for r in rows if (not unread_only or not r.get("read"))]
    want_from = str(args.get("from") or "").strip()
    if want_from:
        picked = [r for r in picked if r.get("from") == want_from]
    want_kind = str(args.get("kind") or "").strip()
    if want_kind:
        picked = [r for r in picked if str(r.get("kind") or "") == want_kind]
    want_corr = str(args.get("correlation_id") or "").strip()
    if want_corr:
        picked = [r for r in picked if str(r.get("correlation_id") or "") == want_corr]
    limit = int(args.get("limit") or 20)
    order = str(args.get("order") or "oldest").lower()
    if order in ("newest", "latest", "desc"):
        # ★"最新"意味着倒序返回，不是"取最新的几封但还按旧→新排"★（2026-09-26 审计复现：
        # 实测拿到 B,C 而 pending_messages() 给的是 C,B —— 同一个"最新"两种顺序，
        # 看板按它排"最近来信"就会错）。取尾切片再反转。
        picked = list(reversed(picked[-limit:]))
    else:
        picked = picked[:limit]           # 默认：最早的未读，先来先处理
    _flag_sig(picked)
    signed = 0
    if mark:
        signed = core.sign_off(sender, {str(r.get("id")) for r in picked}, home, by="mail_inbox")
        # ★甲（D1 修复）★：agent 真的把信读走了，"delivered"至此落地。
        # exec 档 spawn 时不写 delivered_at，等的就是这一下。
        core.stamp_delivered(sender, {str(r.get("id")) for r in picked}, home)
        # ★只在 mark 分支同步内存副本★：sign_off 落的是盘，`rows`/`picked` 里的
        # 还是旧值，而下面 `unread` 是从 `rows` 现算的——不同步就会把"刚签收 2 封"
        # 报成"还剩 2 封未读"。
        # ★peek 分支绝不能这么做★：`picked` 与 `rows` 是同一批 dict，peek 一改，
        # 未读统计跟着变 0（P0-9 的 4.1 曾经这么被打红，2026-09-23）。
        for r in picked:
            r["read"] = True
    return {
        "ok": True,
        "mode": "hub",
        "self": sender,
        "unread": sum(1 for r in rows if not r.get("read")),
        "signed_off": signed,
        "note": ("peek=true：只看不改，未读状态保持原样" if peek
                 else f"已签收 {signed} 封（read_by=mail_inbox）；要只看不改请用 peek=true"),
        "messages": picked,
    }


def do_wait(sender: str, args: dict[str, Any]) -> dict[str, Any]:
    """长轮询等信。★peek 语义：只返回，不消费未读状态★（协议 §5.1）

    wait 若顺手标已读，本端 agent 随后自己 mail_inbox() 会读到"未读余 0"，
    那封信的正文就再也看不到了。要标记已读请用 mail_ack / mail_inbox。

    ★并发上限★（2026-09-26 外部审计复现）：单个 wait 最长挂 900 秒，而
    ThreadingHTTPServer 是"一个连接一个线程、没有总数上限"——任意本机进程开 N 个
    长轮询就能把 N 个线程钉住 15 分钟（INFLIGHT 只用于退出时等收尾，不限流）。
    现在 wait 有名额：满了直接回 `wait.busy`，让调用方几秒后重试或改用 mail_inbox 轮询，
    而不是默默堆积线程。名额数 `AGENT_ANP_WAIT_MAX` 可调，当前占用会在 /health 里可见。
    """
    home = HOME   # ★home 一律取服务端的★：请求体指定 home 是已堵过的
    # `x-mailbox-home` 漏洞的另一个入口（2026-09-22 收紧）
    timeout = max(1.0, min(float(args.get("timeout_sec") or 120), 900.0))
    sender_filter = str(args.get("from") or "").strip()
    with WAIT_LOCK:
        if WAIT_STATE["n"] >= WAIT_MAX:
            return {"ok": False, "mode": "hub", "self": sender, "code": "wait.busy",
                    "wait": dict(WAIT_STATE, max=WAIT_MAX),
                    "error": (f"同时在等的长轮询已达上限 {WAIT_MAX} 个"
                              f"（当前 {WAIT_STATE['n']}，上限可用 AGENT_ANP_WAIT_MAX 调）："
                              "请几秒后重试，或改用 mail_inbox 轮询")}
        WAIT_STATE["n"] += 1
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            rows = core.read_records(sender, home)
            hits = [r for r in rows if not r.get("read") and (not sender_filter or r.get("from") == sender_filter)]
            if hits:
                return {"ok": True, "mode": "hub", "self": sender, "peek": True, "messages": hits,
                        "note": "wait 不消费未读状态；要标记已读请用 mail_ack 或 mail_inbox"}
            time.sleep(0.4)
        return {"ok": True, "mode": "hub", "self": sender, "peek": True, "timeout": True, "messages": []}
    finally:
        with WAIT_LOCK:
            WAIT_STATE["n"] -= 1
            WAIT_STATE["waited"] = int(WAIT_STATE.get("waited") or 0) + 1


def _flag_sig(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给要返回的信逐封标注验签结果（`sig_ok` / `sig_why`），原样保留 `sig`。

    ★为什么标注而不是拒收★：签名是**可观测性**，不是访问控制。因为一封签名不过就把信
    藏起来/不投，等于给了"改坏一封信就能让它消失"的 DoS 手段。所以照常返回、照常签收，
    但把结论显式摆在调用方与看板面前——篡改必须看得见，而不是被静默吞掉。
    """
    for rec in recs:
        ok, why = core.verify_envelope(rec, HOME)
        rec["sig_ok"] = ok
        rec["sig_why"] = why
    return recs


def query_journal(sender: str, args: dict[str, Any]) -> dict[str, Any]:
    """按关联线查消息记录（Plugin API 的地基：插件不必直读任何人的收件箱）。

    ★授权只有一条★：**只有关联线的创建者能读它**（hub 自己 / 管理员令牌除外）。
    创建者 = 第一个在这条线上发信的身份，记在 `journals/<id>.jsonl` 的头记录里。
    不建任务注册表、不做 KV——owner 就写在文件头，没有需要同步的第二份状态。

    `state=true` 时额外把每封信的**实时投递状态**从各收件箱解析出来（默认 false：
    多收件人的话那要读 N 个箱子，坐标上涨）。
    """
    cid = str(args.get("correlation_id") or "").strip()
    if not cid:
        return {"ok": False, "mode": "hub", "error": "correlation_id 必填（这是关联线，不是收件箱）"}
    if sender != "hub":
        owner = core.journal_owner(cid, HOME)
        if not owner:
            return {"ok": False, "mode": "hub",
                    "error": f"关联线 {cid} 不存在（还没有人用它发过信）"}
        if owner != sender:
            return {"ok": False, "mode": "hub",
                    "error": f"关联线 {cid} 属于 {owner}，只有创建者能读（管理员请用 /sessions 同款令牌）"}
    rows = core.read_journal(cid, HOME, limit=int(args.get("limit") or 500))
    out = []
    for rec in rows:
        item = dict(rec)
        if args.get("state"):
            item["delivery"] = _delivery_state_of(rec)
        out.append(item)
    _flag_sig(out)
    # 被改过的 id 收两路来源：journal 那份被改（sig_ok=False），
    # 或任一收件箱那份被改（state=true 时 delivery 里 sig_ok=False）。
    tampered = [m["id"] for m in out if m.get("sig_ok") is False]
    for m in out:
        for d in (m.get("delivery") or []):
            if d.get("sig_ok") is False and m["id"] not in tampered:
                tampered.append(m["id"])
    return {"ok": True, "mode": "hub", "correlation_id": cid, "count": len(out),
            "owner": core.journal_owner(cid, HOME), "messages": out,
            "tampered": tampered}


def _delivery_state_of(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """这封信在各收件箱里的实时投递/签收状态（只读，不改任何状态）。

    ★顺带做跨副本验签★：一封带关联线的信有两份副本——收件箱里的那份与 `journals/` 里的
    那份，各自带签名。只验 journal 那份的话，"篡改收件箱副本"在 `mail_query` 里看不出来。
    所以这里把**收件箱那份**的验签结果一并返回，两份对不上就当场暴露。
    """
    mid = str(rec.get("id") or "")
    targets = core.recipients_of(rec.get("to"), rec.get("from") or "")
    out = []
    for name in targets:
        row = next((r for r in core.read_records(name, HOME) if str(r.get("id")) == mid), None)
        sig_ok, sig_why = (None, None) if row is None else core.verify_envelope(row, HOME)
        out.append({
            "to": name,
            "delivered": bool((row or {}).get("delivered_at")),
            "delivered_at": (row or {}).get("delivered_at"),
            "attempts": (row or {}).get("attempts") or 0,
            "next_retry_at": (row or {}).get("next_retry_at"),
            "last_error": (row or {}).get("last_error"),
            "read": bool((row or {}).get("read")),
            "read_by": (row or {}).get("read_by"),
            "replied_at": (row or {}).get("replied_at"),
            "found": row is not None,
            # 收件箱那一份的验签结论：False = 这份副本被改过
            "sig_ok": sig_ok,
            "sig_why": sig_why,
        })
    return out


def do_ack(sender: str, args: dict[str, Any]) -> dict[str, Any]:
    """签收：标已读并记 `read_by`。支持单封（`id`，前缀即可）或批量（`ids=[...]`）。

    批量那条是为**收信端自签收**准备的（协议 §4.2.2）：桥接把 n 封信渲染进会话后，
    一次把这一批的 id 报回来，不必逐封调。
    """
    home = HOME   # ★home 一律取服务端的★：请求体指定 home 是已堵过的 
    # `x-mailbox-home` 漏洞的另一个入口（2026-09-22 收紧）
    by = str(args.get("by") or sender or "mail_ack")
    ids: list[str] = []
    if args.get("ids"):
        ids = [str(x).strip() for x in (args.get("ids") or []) if str(x).strip()]
    elif args.get("id"):
        ids = [str(args["id"]).strip()]
    if not ids:
        return {"ok": False, "error": "要签收哪些信？给 id（前缀即可）或 ids 数组"}
    done, missing, foreign = [], [], []
    for ident in ids:
        found = core.find_message(ident, home)
        if not found:
            missing.append(ident)
            continue
        rec, owner = found
        # ★归属校验★（2026-09-22 收紧）：`find_message` 是全箱搜索 + 前缀匹配，
        # 以前只要 `by` 以 hub:/wake:/test: 开头就能签收**别人**收件箱里的信——
        # 而 `by` 是调用方自填的参数，等于任意 agent 都能抹掉别人的未读（对方永远看不到那封信）。
        # 现在只有两种人能动一封信：**收件箱主人本人**，或信箱自己（`sender == "hub"`，
        # 自动重投 / 投递后签收都走这条，不靠字符串前缀自封）。
        if owner != sender and sender != "hub":
            foreign.append({"id": str(rec.get("id")), "inbox": owner})
            continue
        # 归属校验过了：这封信的主人本人（或信箱自己）在确认收到。
        # ★甲（D1 修复）★：agent 主动 ack 是"delivered"的落地证据，所以这里补写
        # delivered_at——exec 档 spawn 时故意不写，就等这一下。
        core.sign_off(owner, {str(rec.get("id"))}, home, by=by)
        core.stamp_delivered(owner, {str(rec.get("id"))}, home)
        done.append(str(rec.get("id")))
    return {
        "ok": bool(done),
        "mode": "hub",
        "self": sender,
        "acked": done[0] if len(done) == 1 else None,
        "acked_ids": done,
        "signed_off": len(done),
        "by": by,
        "missing": missing,
        "foreign": foreign,
        "note": ("只能签收自己收件箱里的信（或由信箱自签收）；跨箱签收会被挡下并记进 foreign"
                 + ("；本次被挡下 %d 封" % len(foreign) if foreign else "")),
    }


def wake_sign_off_ids(outcome: dict[str, Any], rec: dict[str, Any]) -> list[str]:
    """兼容壳：真正的实现在 `core.wake_sign_off_ids`（hub / direct 两条路径共用一份）。"""
    return core.wake_sign_off_ids(outcome, rec)


# --------------------------------------------------------------------------- #
# 插件系统（spec §3 生命周期：load / enable / disable / unload）
# --------------------------------------------------------------------------- #
def _plugins_reload() -> dict[str, dict[str, Any]]:
    """把 plugins.json 读进内存。注册表读坏时抛 RuntimeError（调用方转成错误响应）。"""
    global PLUGINS
    PLUGINS = plugin.read_registry(HOME)
    return PLUGINS


def plugin_view(pid: str) -> dict[str, Any] | None:
    """对外看的插件条目：不带 token（manager_hash 只取前 8 位，够辨别是同一枚）。"""
    with PLUGIN_LOCK:
        e = PLUGINS.get(pid)
        if not e:
            return None
        return {
            "id": e["id"], "name": e.get("name"), "version": e.get("version"),
            "api_level": e.get("api_level"), "state": e.get("state"),
            "description": e.get("description"), "author": e.get("author"),
            "permissions": e.get("permissions") or [],
            "requires": e.get("requires") or [],
            "launch": e.get("launch"), "core_spawned": bool(e.get("core_spawned")),
            "base_url": e.get("base_url"), "dir": e.get("dir"),
            "load_error": e.get("load_error"), "loaded_at": e.get("loaded_at"),
            "registered_at": e.get("registered_at"),
            "registered": bool(e.get("contributions") is not None),
            "token_hint": (str(e.get("token") or "")[:8] + "…") if e.get("token") else None,
            "tools": [x["name"] for x in (e.get("contributions") or {}).get("tools", [])],
            "events": (e.get("contributions") or {}).get("events", []),
        }


def plugin_load(pid: str, *, reason: str = "load") -> dict[str, Any]:
    """load：校验 manifest → 确认身份与令牌 → 拉起 → 双向握手。

    ★进程事实三分离★：这里不探活、不保活。插件起来了没、端点通不通，只由握手这一次回答；
    之后它自己退出 = "端点不可达" = 走既有重投，不算状态变化。
    """
    with PLUGIN_LOCK:
        _plugins_reload()
        good, bad = plugin.list_valid(HOME)
        found = good.get(pid)
        if not found:
            known = sorted(set(good) | {b["id"] for b in bad})
            return {"ok": False, "code": "manifest.invalid",
                    "error": f"插件目录里找不到 {pid}（扫 plugins/ 只看到 {known}）"}
        man = plugin.load_manifest(found)
        plugin.check_compat(man)

        entry = dict(PLUGINS.get(pid) or {})
        entry.update({k: man[k] for k in ("id", "name", "version", "api_level", "description",
                                         "author", "license", "repository", "homepage",
                                         "anp_min", "permissions", "requires", "launch",
                                         "command", "url", "cwd", "tags") if k in man})
        entry["dir"] = man.get("dir")
        # 身份与令牌：插件 id 就是它的 ANP 身份名。
        # ★永久架构约束★：这里只调用 core.issue_key()，发证漏斗会保证它不等于 hub.secret。
        entry["token"] = core.issue_key(pid, HOME)
        entry.setdefault("state", "enabled")
        entry["last_action"] = reason
        entry["loaded_at"] = time.time()

        # ★重复 load 不许泄漏进程★：之前 Core 拉起的那个要先终止，否则每次 load 都多一个
        # 常驻进程（startup 预加载 + 手动 load 一次就是两个，实测踩到）。
        if entry.get("core_spawned") and entry.get("pid") and entry.get("pid") != os.getpid():
            plugin._terminate(entry)
        try:
            entry = plugin.launch(man, entry, HOME)
            # ★把插件登记成一个正常的 ANP 身份★（spec §1：插件 = 一个身份 + 一个 manifest
            # + 若干端点）。少了这一步，插件拿得到令牌却在账本里没有行 —— `agent_table()`
            # 查不到它，`deliver_one` 就走"还没登记过唤醒端点"分支，**发给插件的信永远投不出去**，
            # 而 `message.delivered` 事件也就永远不发。工具能通、消息不通，最难查的那种半身不遂。
            wake_url = str(entry.get("base_url") or "").rstrip("/") + "/anp/wake"
            core.heartbeat(pid, HOME, wake={"kind": "http", "url": wake_url},
                           description=man.get("description") or man.get("name"),
                           client="anp-plugin")
            entry["wake_url"] = wake_url
            cfg = plugin.config_for(entry, HOME)
            cfg.setdefault("hub_port", str(HUB_PORT))
            entry["config"] = cfg
            # ★register_out 不在这里做★：见下面锁外那一步（P1-1）
        except plugin.ManifestError as e:
            entry["load_error"] = e.detail
            entry["load_error_code"] = e.code
            PLUGINS[pid] = entry
            plugin.write_registry(PLUGINS, HOME)
            return {"ok": False, "code": e.code, "error": e.detail, "plugin": plugin_view(pid)}

        # ★P1-1 收尾（2026-09-23 / 2026-09-24 修正）★：
        # `register_out` 是最长 REGISTER_TIMEOUT=10s 的**出站 HTTP**，绝不能在
        # PLUGIN_LOCK 内跑。此前缩进错误导致它仍在 `with PLUGIN_LOCK` 里，外层
        # HTTP handler 又包了一层锁 —— load 会堵死管理面，且异常冒泡成
        # RemoteDisconnected（客户端只看到连接被掐断，看不到 load_error）。
        # 现在：中间态落盘后**退出本层锁**，握手在锁外做；任何异常都收成结构化响应。
        entry["contributions"] = None            # 中间态：还没握手
        entry["registered_at"] = 0
        entry["core_terminated"] = False
        entry.pop("load_error", None)
        entry.pop("load_error_code", None)
        PLUGINS[pid] = entry
        plugin.write_registry(PLUGINS, HOME)
    # ── 以上为短锁；以下 register_out 在锁外 ──────────────────────────
    try:
        contrib = plugin.register_out(entry, HOME)      # ★方向二：Core → Plugin★
    except Exception as e:  # noqa: BLE001 - 必须收成 JSON，禁止冒泡成 RemoteDisconnected
        detail = e.detail if isinstance(e, plugin.ManifestError) else f"{type(e).__name__}: {e}"
        code = e.code if isinstance(e, plugin.ManifestError) else "endpoint.unreachable"
        entry["load_error"] = detail
        entry["load_error_code"] = code
        with PLUGIN_LOCK:
            PLUGINS[pid] = entry
            plugin.write_registry(PLUGINS, HOME)
            view = plugin_view(pid)
        return {"ok": False, "code": code, "error": detail, "plugin": view}

    entry["contributions"] = contrib
    entry["registered_at"] = time.time()
    tools = [t.get("name") for t in (contrib.get("tools") or []) if isinstance(t, dict)]
    with PLUGIN_LOCK:
        PLUGINS[pid] = entry
        plugin.write_registry(PLUGINS, HOME)
        view = plugin_view(pid)
    return {"ok": True, "plugin": view,
            "contrib": {k: v for k, v in contrib.items() if k != "tools"} | {"tools": tools}}


def plugin_reload_all(home: Path = None) -> dict[str, Any]:
    """hub 启动时扫一遍 plugins/ 并逐个 load。没有 plugins 目录就什么都不做。"""
    home = home or HOME
    out: list[dict[str, Any]] = []
    for d in plugin.scan_plugins(home):
        try:
            man = plugin.load_manifest(d)
        except plugin.ManifestError as e:
            out.append({"id": str(d), "ok": False, "code": e.code, "error": e.detail})
            continue
        r = plugin_load(man["id"], reason="startup")
        out.append({"id": man["id"], "ok": r.get("ok", False),
                    **({"code": r.get("code")} if r.get("code") else {})})
    return {"loaded": len([x for x in out if x.get("ok")]), "attempted": len(out), "items": out}


def plugin_set_state(pid: str, state: str) -> dict[str, Any]:
    """enable / disable。disable 会终止 **Core 自己拉起的**那个进程（spec §3）。

    ★state 只接受 `enabled` / `disabled`★（`PLUGIN_STATE`）。URL 上是动作动词
    enable/disable，到这一层必须映射成状态值——曾经直接把动词传进来，
    结果 state 被写成 "disable"（一个不存在状态），disable 也就没有终止进程。
    """
    if state not in plugin.PLUGIN_STATE:
        return {"ok": False, "code": "manifest.invalid",
                "error": f"插件状态只接受 {list(plugin.PLUGIN_STATE)}，收到 {state!r}"}
    with PLUGIN_LOCK:
        _plugins_reload()
        e = PLUGINS.get(pid)
        if not e:
            return {"ok": False, "code": "manifest.invalid", "error": f"插件未加载：{pid}"}
        killed = False
        relaunch = False
        if state == "disabled":
            # ★把"Core 自己终止了它"记成事实★（不是进程状态推断——Core 只知道自己做过什么）
            killed = plugin._terminate(e)
            e["core_terminated"] = True
        else:
            # disable 终止过的，enable 要重新拉起 + 重新握手（spec §3）。
            # 没被终止过就什么都不做：enable 不是"重启"。
            relaunch = bool(e.get("core_spawned")) and bool(e.get("core_terminated"))
        e["state"] = state
        e["last_action"] = state
        if relaunch:
            try:
                man = plugin.load_manifest(Path(e["dir"]))
                e = plugin.launch(man, e, HOME)
                e["contributions"] = plugin.register_out(e, HOME)
                e["core_terminated"] = False
            except plugin.ManifestError as ex:
                PLUGINS[pid] = e
                plugin.write_registry(PLUGINS, HOME)
                return {"ok": False, "code": ex.code, "error": ex.detail}
        PLUGINS[pid] = e
        plugin.write_registry(PLUGINS, HOME)
        return {"ok": True, "plugin": plugin_view(pid),
                "terminated_core_spawned": killed}


def plugin_unload(pid: str) -> dict[str, Any]:
    """unload：终止 Core 拉起的进程 → 从注册表移除（身份令牌留着，key_for 复用）。"""
    with PLUGIN_LOCK:
        _plugins_reload()
        e = PLUGINS.pop(pid, None)
        if not e:
            return {"ok": False, "code": "manifest.invalid", "error": f"插件未加载：{pid}"}
        killed = plugin._terminate(e)
        plugin.write_registry(PLUGINS, HOME)
        return {"ok": True, "unloaded": pid, "terminated_core_spawned": killed}


def dispatch_tool(name: str, sender: str, args: dict[str, Any]) -> dict[str, Any]:
    # 插件工具：Core 自己不认识的名字，先按「插件工具表」查一遍。
    # 查到了就转发到插件的 /anp/tool——Core 仍然不解释它在干什么。
    hit = plugin.tool_registry(HOME).get(name)
    if hit:
        entry = dict(plugin.read_registry(HOME).get(hit["plugin_id"]) or {})
        if not entry:
            return {"ok": False, "error": f"插件 {hit['plugin_id']} 的注册条目不见了",
                    "code": "plugin.error"}
        return plugin.call_plugin_tool(hit["plugin_id"], entry, hit["tool"].get("name", ""),
                                       args, sender, HOME)
    with STORE_LOCK:
        if name == "mail_send":
            return do_send(sender, args)
        if name == "mail_inbox":
            return do_inbox(sender, args)
        if name == "mail_ack":
            return do_ack(sender, args)
        if name == "mail_query":
            return query_journal(sender, args)
        if name == "mail_pending":
            return do_pending(sender, args)
        if name == "mail_redeliver":
            return do_redeliver(sender, args)
        if name == "mail_agents":
            # ★agents.discover 在这里执行★（spec §5：调用点查表）。
            # `GET /agents` 是另一回事：它免令牌、且被 SDK 的 resolve() 依赖
            # （每个 Adapter 都 vendored 了那一份客户端），给它上令牌会打断整个接入生态，
            # 所以在 Errata 里记着；这个 `/tool` 面的调用方是**带令牌自证身份**的，能查表。
            perms = plugin.permissions_of(sender, HOME)
            if perms is not None and "agents.discover" not in perms:
                return {"ok": False, "code": "permission.denied",
                        "permission": "agents.discover",
                        "error": f"插件 {sender} 没有 agents.discover 权限，不能枚举名册",
                        "hint": f"要放行：在 {sender} 的 manifest.json 的 permissions 里加上 "
                                f"agents.discover，然后 reload 这个插件"}
            rows = agent_view(HOME)   # ★home 由服务端说了算★，不接受调用方指定
            for r in rows:
                r["self"] = r["name"] == sender
            return {"ok": True, "mode": "hub", "self": sender, "agents": rows}
        if name == "mail_wait":
            pass  # 长轮询不能占着全局锁
        else:
            return {"ok": False, "error": f"未知工具 {name}"}
    if name == "mail_wait":
        return do_wait(sender, args)
    return {"ok": False, "error": "未处理"}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AgentNetPort-hub/1.0"

    # 访问日志里必须打码的查询参数：`?token=` 是看板（浏览器）的管理员令牌用法，
    # 堵不掉，但 hub.log 的读者面比令牌本身大得多（它会被 spawn_hub 重定向进去、
    # 会被贴进 issue、会被同步工具搬走）——原样落日志等于把钥匙贴在门外。
    _SECRET_QS = ("token", "key", "secret", "password")

    @classmethod
    def _redact_path(cls, path: str) -> str:
        if "?" not in path:
            return path
        base, _, qs = path.partition("?")
        pairs = []
        for pair in qs.split("&"):
            name, sep, value = pair.partition("=")
            # 查询参数名可以百分号编码（例如 `%74oken`），脱敏判定必须先解码。
            try:
                decoded_name = urllib.parse.unquote_plus(name).strip().lower()
            except Exception:  # noqa: BLE001
                decoded_name = name.strip().lower()
            if value and decoded_name in cls._SECRET_QS:
                pairs.append(f"{name}{sep}***")
            else:
                pairs.append(pair)
        return base + "?" + "&".join(pairs)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        LOG.write("[hub] " + self._redact_path(fmt % args) + "\n")

    # -- helpers ---------------------------------------------------------- #
    def _raw(self, code: int, body: str, content_type: str = "application/json") -> None:
        """原样转发一段文本（插件面板用）。★不清洗、不解析、不缓存★——
        清洗会改内容，解析会让 Core 开始理解插件的输出。"""
        raw = str(body or "").encode("utf-8", "replace")
        self.send_response(code)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(raw)))
        self.send_header("cache-control", "no-store")
        # 独立管理 SPA（webui）与 hub 不同源：浏览器预检/读 JSON 需要 CORS（仅本机管理面）
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers",
                         "content-type, x-anp-admin, x-mailbox-key")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, payload: dict[str, Any], code: int = 200,
              deprecation: str | None = None) -> None:
        # 正文可能来自对端文本：先清洗再编码，避免一个坏字符炸掉整条响应。
        body = json.dumps(core.safe(payload), ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers",
                         "content-type, x-anp-admin, x-mailbox-key")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        if deprecation:
            self.send_header("deprecation", deprecation)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        # ★content-length 必须是"非负整数"★（2026-09-26 外部审计复现）：
        # · 负数（`-1`）会让 `rfile.read(-1)` 变成"读到 EOF 为止"——连接不关，
        #   这个请求线程就一直挂住（实测：客户端等到超时也拿不到响应）；
        # · 非数字（`abc`）会让 `int()` 抛 ValueError，而调用点只接 BadBody，
        #   异常一路冒到 socketserver，客户端看到的是连接被重置而不是一个 400。
        raw_len = self.headers.get("content-length")
        length = 0
        if raw_len is not None and str(raw_len).strip():
            try:
                length = int(str(raw_len).strip())
            except ValueError as e:
                raise BadBody(f"content-length 不是整数：{raw_len!r}") from e
            if length < 0:
                raise BadBody(f"content-length 不能是负数（{length}）：拒绝按它读请求体")
        if not length:
            return {}
        # ★请求体体积上限★（2026-09-22）：以前按声明的 content-length 全读进内存，
        # 本机任意进程都能用一个巨大的 content-length 把 hub 顶到 OOM。
        # 一封信远用不到 4 MiB，上限可用 AGENT_ANP_MAX_BODY_BYTES 调。
        if length > MAX_BODY_BYTES:
            raise BadBody(f"请求体 {length} 字节超过上限 {MAX_BODY_BYTES}"
                          f"（AGENT_ANP_MAX_BODY_BYTES 可调）")
        old_timeout = None
        try:
            old_timeout = self.connection.gettimeout()
            self.connection.settimeout(BODY_READ_TIMEOUT)
            raw_bytes = self.rfile.read(length)
        except (OSError, TimeoutError) as e:
            raise BadBody(f"请求体读取超时或连接中断：{type(e).__name__}") from e
        finally:
            try:
                self.connection.settimeout(old_timeout)
            except OSError:
                pass
        if len(raw_bytes) != length:
            raise BadBody(f"请求体长度不足：声明 {length} 字节，实际收到 {len(raw_bytes)} 字节")
        try:
            # ★严格 UTF-8★：解不动就报错，绝不静默替换成 U+FFFD。
            # 以前用 errors="replace" 会把发送端的编码事故变成一封"看起来正常"的乱码信，
            # 谁也发现不了（chencode 2026-09-20 实测踩到）。
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            raise BadBody(f"请求体不是合法 UTF-8（{e.reason} @ byte {e.start}）：请让发送端显式用 UTF-8 编码") from e
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return core.safe(data) if isinstance(data, dict) else {}

    def _home(self, args: dict[str, Any]) -> Path:
        return HOME

    def _admin_ok(self) -> bool:
        """管理员令牌校验：看 `/health` 之外的敏感端点（`/sessions`）。

        令牌来源：`x-mailbox-key` 头、`x-anp-admin` 头、或 `?token=` 查询参数
        （看板是浏览器，带头不方便，所以留查询参数这条路）。
        """
        given = (self.headers.get("x-mailbox-key")
                 or self.headers.get("x-anp-admin")
                 or urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("token", [""])[0])
        return core.admin_token_ok(given, HOME)

    # -- routes ----------------------------------------------------------- #
    def do_OPTIONS(self) -> None:  # noqa: N802
        # 独立管理 SPA 的 CORS 预检
        self.send_response(204)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers",
                         "content-type, x-anp-admin, x-mailbox-key")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.send_header("content-length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/tools":
            # 插件工具目录（给 agent 侧的 tools/list 合并用）。
            # ★Core 自己的工具不在这个列表里★：MCP 自己那一份是静态真相，两边各写一份必然漂移。
            # ★Core 不解释插件工具★：schema 原样透传，调用时整包转发。
            self._json({"ok": True, "core_api_level": core.CORE_API_LEVEL,
                        "plugin_tools": [
                            {"name": mcp_name, "plugin": hit["plugin_id"],
                             "canonical": hit["canonical"],
                             "description": (hit["tool"].get("description")
                                             or f"插件 {hit['plugin_id']} 提供的工具"),
                             # 插件侧声明的是 input_schema，MCP 边界统一成 inputSchema
                             "inputSchema": hit["tool"].get("input_schema") or {"type": "object"}}
                            for mcp_name, hit in sorted(plugin.tool_registry(HOME).items())],
                        "clashes": plugin.tool_clashes(HOME)})
            return
        if path.startswith("/admin/panel"):
            # ★管理面：代理插件的面板内容★（spec §4 方向 C）
            # Core 是唯一管理边界：WebUI 不直读插件文件、不直连插件端口，
            # 只通过这一个口子拿内容。多一次 HTTP 跳换来"插件不绑定任何看板实现"。
            if not self._admin_ok():
                self._json({"ok": False, "error": "需要管理员令牌", "auth": "admin-required"}, 401)
                return
            parts = [x for x in path.split("/") if x]        # ['admin','panel'] 或 [...,'<id>']
            if len(parts) == 2:
                # ★P1-1（2026-09-23）★：列表**不占 PLUGIN_LOCK、不 reload**。
                # `plugin.panels()` 自己从盘上读注册表（只读），不碰内存里的 PLUGINS；
                # 而 plugin_load / enable / disable 会在**同一把锁**里做 register_out
                # （最长 REGISTER_TIMEOUT=10s）。列表一旦持锁，管理面或启动路径一 load，
                # 列表和详情就一起堵——与"列表、详情都超时"的现象吻合。
                self._json({"ok": True, "panels": plugin.panels(HOME)})
                return
            pid = urllib.parse.unquote(parts[2])
            with PLUGIN_LOCK:
                _plugins_reload()
                entry = PLUGINS.get(pid)
            if not entry or entry.get("state") != "enabled":
                self._json({"ok": False, "error": f"插件未加载或已停用：{pid}"}, 404)
                return
            if "webui.panel" not in (entry.get("permissions") or []):
                self._json({"ok": False, "error": f"插件 {pid} 没有 webui.panel 权限",
                            "code": "permission.denied"}, 403)
                return
            code, body, ctype = plugin.call_plugin_panel(pid, entry, HOME)
            self._raw(code, body, ctype)
            return
        if path == "/admin/messages":
            # ★全局只读管理面★（WebUI / 管理 App）：跨收件箱浏览正文。
            # 与 `/messages/query`（关联线 + owner）是两套能力，刻意不合并——见 docs/ANP-ADMIN-API.md。
            # 只读：不签收、不重投、不改任何状态字段。
            if not self._admin_ok():
                self._json({"ok": False, "error": "需要管理员令牌", "auth": "admin-required"}, 401)
                return
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

            def _q(name: str, default: str = "") -> str:
                return (query.get(name) or [default])[0]

            def _flag(name: str) -> bool:
                return str(_q(name, "")).strip().lower() in ("1", "true", "yes", "on")

            try:
                out = core.browse_messages(
                    HOME,
                    inbox=_q("inbox"),
                    sender=_q("from"),
                    to=_q("to"),
                    unread_only=_flag("unread"),
                    kind=_q("kind"),
                    correlation_id=_q("correlation_id"),
                    q=_q("q"),
                    limit=int(_q("limit", "200") or 200),
                    offset=int(_q("offset", "0") or 0),
                    verify_sig=_q("sig", "1").strip().lower() not in ("0", "false", "no"),
                )
                self._json({"ok": True, "mode": "admin", "service": "AgentNetPort-hub",
                            "home": str(HOME), **out})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return
        if path == "/plugins":
            # 列出插件（脱敏：不带 token，只给前 8 位供辨别是同一枚）
            with PLUGIN_LOCK:
                try:
                    _plugins_reload()
                    self._json({"ok": True, "plugins": [plugin_view(k) for k in sorted(PLUGINS)]})
                except Exception as e:  # noqa: BLE001
                    self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return
        if path == "/health":
            self._json({
                "ok": True,
                "service": "AgentNetPort-hub",
                # ★P0-1（2026-09-23）★：serve 线程现在先于插件预加载启动
                # （否则启动阶段 /health 连不上，插件回调 Hub 还会对等死锁）。
                # 代价是"HTTP 已 accept"不再等于"插件已就绪"，所以必须给个明确信号：
                # 调用方要等插件就绪，就轮询它，别用"/health 能通"当插件加载完成。
                "preload_done": bool(PRELOAD_STATE.get("done")),
                "home": str(HOME),
                "agents": [a["name"] for a in agent_view()],
                # 信箱体检：未读 / 未投递 / 到期待重投 / 已消费 / 已回信
                "mail": core.inbox_health(HOME),
                # 令牌使用情况：谁在带密钥、谁被拒（"密钥有没有在发挥作用"的可观测答案）
                "auth": dict(AUTH_STATS),
                # 自动重投：是否开启、上一轮扫到什么（"信会不会自己再投"的可观测答案）
                "auto_redeliver": dict(AUTO_STATE),
                # 数据维护：上一轮删了多少、账本压到多小（"数据到底有没有在长"的可观测答案）
                "maintenance": {k: v for k, v in MAINT_STATE.items() if k != "last"} | {"last": MAINT_STATE.get("last")},
                # 插件：装了哪些、起来没、贡献了什么（"插件系统活着吗"的可观测答案）
                "plugins": {"total": len(PLUGINS), "enabled": sum(1 for e in PLUGINS.values() if e.get("state") == "enabled"),
                            "registered": sum(1 for e in PLUGINS.values() if e.get("contributions") is not None),
                            # ★永久约束自检★：插件 token 不得与 hub.secret 相同
                            "secret_isolation": core.secret_isolation_ok(HOME)[1]},
                # 事件总线：入队/丢弃/送达/失败（"插件有没有收到事件"的可观测答案）
                "events": dict(EVENTS.stats),
                # 信封完整性：多少封验过、多少封被改过（"消息是否被篡改"的可观测答案）。
                # 只统计不拦截——签名是可观测性，不是访问控制。
                "integrity": core.integrity_report(HOME),
                # 日志队列：丢了多少条必须看得见，否则"日志少了"会被误判成"事情没发生"
                "log": dict(LOG.stats()),
                # 权限策略现状（"谁能铸造身份/谁能广播/谁能发给谁"必须可直接读出来）
                "policy": policy.summary(HOME),
                # 长轮询名额：有没有人在等、服务过多少轮（"长轮询会不会把服务钉住"的答案）
                "wait": {"inflight": WAIT_STATE["n"], "max": WAIT_MAX,
                         "served": WAIT_STATE.get("waited") or 0},
                "pid": os.getpid(),
                "time": time.time(),
            })
        elif path == "/agents":
            self._json({"ok": True, "agents": agent_view()})
        elif path == "/sessions":
            # 会话大脑的现状：各通道上报的名册 + 对话线绑定 + 最近决策流水。
            # ★要管理员令牌★（2026-09-22）：这里含各通道上报的**工作区绝对路径**与 session id，
            # 是"看一眼就知道这台机器在干什么"的那种泄露。`/health`、`/agents` 仍免令牌——
            # 它们被 ensure_hub / register_self / webui_alive 等内部探活依赖，且不含正文。
            if not self._admin_ok():
                self._json({"ok": False, "error": "需要管理员令牌：x-mailbox-key 头或 ?token= 参数",
                            "hint": "令牌在 ~/.agentnetport/admin.token 或 hub 启动日志里",
                            "auth": "admin-required"}, 401)
                return
            self._json({"ok": True, **sessions.roster_view(HOME)})
        else:
            self._json({"ok": False, "error": f"未知路径 {path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            body = self._body()
        except BadBody as e:
            # loud failure：非法编码直接 400，别让一封静默损坏的信流进收件箱。
            self._json({"ok": False, "error": str(e)}, 400)
            return
        if path == "/session/roster":
            # 收信通道上报"我这边有哪些会话"——信箱据此决定复用还是新开。
            adapter = str(body.get("adapter") or body.get("name") or "").strip()
            if not adapter:
                self._json({"ok": False, "error": "adapter 必填"}, 400)
                return
            auth = self._auth(adapter)
            if not auth["ok"]:
                self._json({"ok": False, "error": auth["error"], "auth": auth}, 401)
                return
            home = HOME                      # ★服务端自己的 home 说了算★，见 _auth 的说明
            with STORE_LOCK:
                out = sessions.update_roster(adapter, list(body.get("sessions") or []), home,
                                             workspace=str(body.get("workspace") or ""))
            out["auth"] = auth["state"]
            self._json(out)
            return
        if path == "/session/bind":
            # 收信通道执行完"开新对话"之后回报落点，信箱记住这条对话线。
            adapter = str(body.get("adapter") or "").strip()
            auth = self._auth(adapter)
            if not auth["ok"]:
                self._json({"ok": False, "error": auth["error"], "auth": auth}, 401)
                return
            home = HOME
            with STORE_LOCK:
                out = sessions.bind_context(str(body.get("context_id") or ""),
                                            str(body.get("session_id") or ""),
                                            adapter, home,
                                            workspace=str(body.get("workspace") or ""))
            out["auth"] = auth["state"]
            self._json(out)
            return
        if path == "/heartbeat":
            name = str(body.get("name") or "").strip()
            if not name:
                self._json({"ok": False, "error": "name 必填"}, 400)
                return
            # ★身份名必须能无损落盘★（2026-09-26 外部审计：注册接口原来没有任何约束）。
            # `core._safe()` 把身份名编码成收件箱文件名，若名字本身就带编码字符集之外的
            # 字符（`/` `:` 空格…），落盘名与身份名就不再一一对应：注册时看不出问题，
            # 之后"别人以什么名字给我写信、我以什么名字读信"全靠猜。编码已经保证
            # **不同名字不共用文件**（单射），这里再挡一道：名字最好是本来就可用的。
            if name != core._safe(name):
                self._json({"ok": False, "error":
                            f"身份名 {name!r} 含不允许的字符：只允许字母、数字、-、_、.、@",
                            "hint": "换成全字母数字的名字（例如把 'agent/one' 改成 'agent.one'）"},
                           400)
                return
            # Windows 收件箱文件名大小写不敏感；保留旧文件名不动，但拒绝新注册
            # 与既有身份仅大小写不同的别名，避免两套身份共享同一个落盘文件。
            clash = core.identity_casefold_conflict(name, HOME)
            if clash:
                self._json({"ok": False, "error":
                            f"身份名 {name!r} 与既有身份 {clash!r} 仅大小写不同，"
                            "在 Windows 上会冲突",
                            "hint": "请使用不同的完整身份名"}, 409)
                return
            # ★这个接口以前没有鉴权★（2026-09-20 审阅发现）：任何本机进程都能 POST 一条
            # `{"name":"dsh","wake":{"kind":"exec","command":[…]}}` 把真实身份的唤醒端点顶掉——
            # 而 agent_table 取账本最后一条，于是下一封发给该身份的信就会执行那条命令（任意命令执行）。
            # 现在的门：**身份没出现过 = 首次注册，放行并当场发密钥；已经存在 = 必须拿对密钥**。
            known = core.identity_known(name, HOME)
            if known:
                auth = self._auth(name)
                if not auth["ok"]:
                    self._json({"ok": False, "error": auth["error"], "auth": auth}, 401)
                    return
            else:
                # ★谁能"铸造新身份"由权限策略决定★（2026-09-22）：首次引导会自动发密钥，
                # 于是本机任何进程都能凭空造出一个身份、拿一个收件箱、然后开始发信
                # （真实账本里 fixprobe / regressprobe / root / freshprobe 就是这么来的）。
                # 默认 register=open（与加权限层前一致），要收紧改 permissions.json。
                # 持管理员令牌的人永远放行，否则"上锁"会把接线脚本自己关在门外。
                token = self.headers.get("x-mailbox-key") or ""
                ok, why = policy.may_register(name, core.admin_token_ok(token, HOME), HOME)
                if not ok:
                    LOG.write(f"[hub] 403 拒绝铸造身份 {name}: {why}\n")
                    self._json({"ok": False, "error": f"不允许铸造新身份 {name}：{why}",
                                "hint": "要放开：把 permissions.json 的 register.mode 设成 open，"
                                        "或把这个名字加进 register.allow",
                                "policy": policy.summary(HOME)}, 403)
                    return
                auth = {"ok": True, "state": "bootstrap"}
            extra = {k: v for k, v in body.items() if k not in ("name", "home", "home_header")}
            with STORE_LOCK:
                rec = core.heartbeat(name, HOME, **extra)   # home 固定服务端自己的
                key = core.issue_key(name, HOME)            # 幂等：首次发放
            # ★密钥在响应里发★：新身份不必去翻那个"所有人都能读"的 keys.json 就能拿到自己的令牌
            self._json({"ok": True, "registered": rec, "home": str(HOME),
                        "key": key, "auth": auth["state"]})
            return
        # ★P0-9★ canonical 是 /messages/send；/send 保留为 deprecated 别名。
        # 别名与正名走**同一段代码**（不是复制一份），所以不可能分叉。
        # 响应体一字不改（不破坏现有调用方），只多一个 Deprecation 响应头说明去向。
        if path in ("/send", "/messages/send"):
            sender = str(body.get("from") or "unknown").strip() or "unknown"
            auth = self._auth(sender)
            if not auth["ok"]:
                self._json({"ok": False, "error": auth["error"], "auth": auth}, 401)
                return
            args = dict(body)
            args["wake"] = body.get("wake", True)
            try:
                result = do_send(sender, args)
                result["auth"] = auth["state"]
                # ★响应头必须纯 ASCII★：http.server 的 send_header 用 latin-1 编码，
                # 放一个中文箭头进去会让它 UnicodeEncodeError，handler 半路死掉，
                # 客户端只读到截断的 body（2026-09-22 实测踩过）。
                self._json(result, deprecation=("use POST /messages/send"
                                                if path == "/send" else None))
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 400)
            return
        if path == "/messages/query":
            # Plugin API：按关联线查消息记录（只有创建者能读）
            sender = str(body.get("from") or body.get("server") or "unknown").strip() or "unknown"
            auth = self._auth(sender)
            if not auth["ok"]:
                self._json({"ok": False, "error": auth["error"], "auth": auth}, 401)
                return
            try:
                out = query_journal(sender, body)
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            out["auth"] = auth["state"]
            self._json(out)
            return
        if path == "/messages/receive":
            # ★P0-9★ canonical 的读信端点。★直接调 do_inbox★——
            # `/tool` 的 mail_inbox 也是调它，所以两个入口共享同一份实现，
            # 不可能出现"两个接口语义慢慢分叉"（主人 2026-09-22 的要求）。
            who = str(body.get("from") or body.get("server") or "").strip() or "unknown"
            auth = self._auth(who)
            if not auth["ok"]:
                self._json({"ok": False, "error": auth["error"], "auth": auth}, 401)
                return
            # 身份字段不透传给底层，其余（peek/unread_only/mark_read/from/kind/
            # correlation_id/limit/order）原样交给 do_inbox
            args = {k: v for k, v in body.items() if k not in ("from", "server")}
            try:
                result = do_inbox(who, args)
                result["auth"] = auth["state"]
                self._json(result)
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 400)
            return
        if path == "/plugin/register":
            # ★★★ 方向一：Plugin → Core（spec §4.0）★★★
            # 插件主动报到：交"贡献"，换"凭证"。
            # 极易与 /anp/register 混淆——那一个是 Core 主动、Core 给凭证，方向相反。
            pid = str(body.get("id") or "").strip()
            if not pid:
                self._json({"ok": False, "error": "缺少 id（你要报到的插件 id）"}, 400)
                return
            # ★C1 方案 P（2026-09-23）★：报到要证明「我是 pid 本人」，不是「我是管理员」。
            # 插件自己的 agent token 就够——Core 在 /anp/register 时已经发过那一枚
            # （body.token，请求头也带了一份），插件重启后原样持着。
            # 它比 admin 更精确：admin 令牌只证明"你是管理员"，答不出"你是哪个插件"。
            # ★admin 令牌在这里明确不接受，不留运维旁路★、也★不引入 register nonce★
            # （插件已有自己的 token，再发一枚是并存两个同样能力的秘密）。
            given = self.headers.get("x-mailbox-key") or ""
            if not given:
                self._json({"ok": False, "error": "插件报到要带插件自己的令牌（x-mailbox-key）",
                            "code": "auth.plugin-credential",
                            "hint": "这枚令牌是 Core 在 /anp/register 时发给你的"
                                    "（响应体 body.token，请求头也带了一份）；"
                                    "admin 令牌在此不被接受",
                            "auth": "plugin-required"}, 401)
                return
            ok, why = core.verify_key(pid, given, HOME)
            if not ok:
                # ★401 vs 403★：凭证缺失/不对 = 401（"你是谁都没证明"）；
                # 403 留给"身份已认证但没有权限"（见 /tool 的 permission.denied）。
                self._json({"ok": False, "error": f"插件报到令牌不对：{why}",
                            "code": "auth.plugin-credential",
                            "hint": "要用**这个插件自己**的那枚令牌；"
                                    "拿 A 插件的令牌报到 B 插件会被拒",
                            "auth": "plugin-rejected"}, 401)
                return
            with PLUGIN_LOCK:
                _plugins_reload()
                entry = PLUGINS.get(pid)
                if not entry:
                    # ★这个 404 实际上到不了★：未加载的插件从没发过 key，
                    # `verify_key` 在鉴权步就返回 rejected → 已经 401 了。
                    # 保留它只是纵深防御。若哪天把鉴权挪到存在性之后，
                    # 未加载插件会从 401 变 404——那是**泄露了"这个插件没被加载"**，
                    # 与 2026-09-23 定下的"401=凭证不对、403=无权限"分层相抵，别那么做。
                    self._json({"ok": False, "code": "manifest.invalid",
                                "error": f"插件未加载：{pid}（先 load 它）"}, 404)
                    return
                token = entry.get("token") or core.issue_key(pid, HOME)
                entry["token"] = token
                entry["contributions"] = plugin._norm_contributions(body)
                entry["registered_at"] = time.time()
                entry["last_action"] = "plugin-register"
                plugin.write_registry(PLUGINS, HOME)
                PLUGINS[pid] = entry
                # ★永久架构约束★：这里只发插件自己那一枚 agent token，绝不含 hub.secret
                self._json({"ok": True, "identity": pid, "token": token,
                            "config": entry.get("config") or {},
                            "plugin_dir": str(entry.get("dir") or ""),
                            "base_url": entry.get("base_url"),
                            "api_level": int(entry.get("api_level") or core.CORE_API_LEVEL),
                            "core_api_level": core.CORE_API_LEVEL,
                            "permissions": entry.get("permissions") or [],
                            # ★"admin" 是 2026-09-26 审计抓出来的假标签★：这个入口
                            # 验的是**插件自己的**凭证（admin 令牌在此明确不接受），
                            # 回一个 "admin" 会让读响应的人以为管理员令牌也能进。
                            "auth": "plugin-credential"})
            return
        if path == "/plugin/config":
            pid = str(body.get("id") or "").strip()
            if not self._admin_ok():
                self._json({"ok": False, "error": "需要管理员令牌", "auth": "admin-required"}, 401)
                return
            with PLUGIN_LOCK:
                _plugins_reload()
                entry = PLUGINS.get(pid)
                if not entry:
                    self._json({"ok": False, "error": f"插件未加载：{pid}"}, 404)
                    return
                self._json({"ok": True, "id": pid, "config": plugin.config_for(entry, HOME)})
            return
        if path.startswith("/plugins/"):
            # 管理面：/plugins/<id>/<load|enable|disable|unload|reload>
            if not self._admin_ok():
                self._json({"ok": False, "error": "需要管理员令牌", "auth": "admin-required"}, 401)
                return
            parts = [x for x in path.split("/") if x]        # ['plugins', '<id>', '<action>']
            if len(parts) != 3:
                self._json({"ok": False, "error": "路径形如 /plugins/<id>/<load|enable|disable|unload|reload>"}, 400)
                return
            pid, action = parts[1], parts[2]
            # ★不要在 HTTP 层包 PLUGIN_LOCK★：plugin_load/set_state/unload 自己管短锁；
            # 整段包住会把 register_out（最长 10s）再次拖进锁里，并让异常变成
            # RemoteDisconnected。动作分发保持无锁 + 结构化错误。
            try:
                if action == "load":
                    r = plugin_load(pid, reason="load")
                elif action == "reload":
                    plugin_unload(pid)
                    r = plugin_load(pid, reason="reload")
                elif action in ("enable", "disable"):
                    # ★URL 上是动词，状态只有 enabled/disabled★（别把动词当状态存下去）
                    r = plugin_set_state(pid, "enabled" if action == "enable" else "disabled")
                elif action == "unload":
                    r = plugin_unload(pid)
                else:
                    self._json({"ok": False, "error": f"未知动作 {action}"}, 400)
                    return
            except Exception as e:  # noqa: BLE001 - 禁止冒泡成连接中断
                r = {"ok": False, "code": "plugin.error",
                     "error": f"{type(e).__name__}: {e}"}
            self._json(r, 200 if r.get("ok") else 409)
            return
        if path == "/tool":
            name = str(body.get("name") or "")
            sender = str(body.get("server") or body.get("from") or "unknown").strip() or "unknown"
            args = body.get("args") if isinstance(body.get("args"), dict) else {}
            auth = self._auth(sender)
            if not auth["ok"]:
                self._json({"ok": False, "error": auth["error"], "auth": auth}, 401)
                return
            with INFLIGHT_LOCK:
                INFLIGHT["n"] += 1
            try:
                result = dispatch_tool(name, sender, args)
                if isinstance(result, dict):
                    result["auth"] = auth["state"]
                self._json(result)
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 400)
            finally:
                with INFLIGHT_LOCK:
                    INFLIGHT["n"] -= 1
                    INFLIGHT["cv"].notify_all()
            return
        self._json({"ok": False, "error": f"未知路径 {path}"}, 404)

    def _auth(self, name: str) -> dict[str, Any]:
        """校验身份令牌（协议 §3.1）。

        三条硬规则（2026-09-20 审阅后收紧）：
        1. **home 由服务端决定**：请求头 `x-mailbox-home` 不再影响校验或数据面。
           以前两者可以指向不同目录——伪造一个空目录的头就能让密钥表"查不到"从而放行，
           数据却仍然读写真 home，等于完整绕过鉴权（已实测复现）。
        2. **issued-pending 也要拒绝**：该身份在此 home 里没有密钥时，只有
           "这名字从没出现过"（`identity_known` 为假）才可能被当成首次引导，
           而引导走 `POST /heartbeat`（它会当场发密钥）。其余一律 401。
        3. **密钥表读不动就拒绝**（`KeysUnreadable`），绝不回落成"没有密钥表"。

        每次结果都记进 `AUTH_STATS`（`/health` 可见）："密钥到底有没有在发挥作用"必须可观测。
        """
        token = self.headers.get("x-mailbox-key") or ""
        home = HOME
        try:
            ok, state = core.verify_key(name, token, home)
        except core.KeysUnreadable as e:
            LOG.write(f"[hub] 401 {name}: keys-unreadable（{e}）\n")
            return {"ok": False, "state": "keys-unreadable",
                    "error": f"密钥表读不动，出于安全一律拒绝：{e}"}
        if state == "issued-pending" and not core.identity_known(name, home):
            state = "bootstrap-pending"      # 全新身份：只允许去 /heartbeat 领密钥
        with AUTH_LOCK:
            AUTH_STATS["at"] = time.time()
            AUTH_STATS["total"] = int(AUTH_STATS.get("total") or 0) + 1
            per = AUTH_STATS.setdefault("by_identity", {})
            row = per.setdefault(name, {"ok": 0, "rejected": 0, "missing": 0, "last_state": None, "last_at": None})
            row["last_state"] = state
            row["last_at"] = time.time()
            if ok and state in ("ok",):
                row["ok"] += 1
            elif state == "rejected":
                row["rejected"] += 1
            else:
                row["missing"] += 1
        if not ok:
            if state == "missing":
                msg = (f"{name} 没带密钥（x-mailbox-key 头）：身份名是请求方自己填的，"
                       f"不带密钥就等于可以随便冒充别人，所以现在一律拒绝。"
                       f"密钥由 POST /heartbeat 的响应发放（首次心跳），之后每次请求都要带上。")
            elif state == "bootstrap-pending":
                msg = (f"{name} 是全新身份：先 POST /heartbeat 领取密钥（响应里的 key 字段），"
                       f"再带着它调用数据面接口。")
            else:
                msg = f"{name} 的密钥不对（x-mailbox-key 头）"
            LOG.write(f"[hub] 401 {name}: {state}\n")
            return {"ok": False, "state": state, "error": msg}
        return {"ok": True, "state": state}


def auto_redeliver(home: Path | None = None, limit: int = 3,
                     max_attempts: int | None = None) -> dict[str, Any]:
    """自动重投：把"排了重投且已到期"的信再投一次（★信箱自己做的事★，不需要谁记得）。

    home = HOME if home is None else Path(home)
    为什么要它（主人 2026-09-20 定）：`record_delivery` 只把 `next_retry_at` 写进状态，
    过去必须有人/agent 手动 `mail_redeliver`——没人记得，信就烂在箱底，
    而"未投递"这个状态本身还一直在体检里报警。状态机既然会算退避时间，就该由信箱自己兑现。

    安全阀（不制造风暴）：
      - 每轮最多重投 `limit` 封（默认 3，`AGENT_ANP_REDELIVER_LIMIT`）；
      - 单封累计尝试超过 `max_attempts`（默认 5，`AGENT_ANP_REDELIVER_MAX`）就**停手**，
        标 `last_error` 说明"已放弃自动重投"，人可在体检里看到（不静默丢弃）；
      - 只重投 `next_retry_at` 已到期的、且从未投递成功（`delivered_at` 为空）的；
      - 已签收（read）的不动。

    返回本轮结果，供 `/health` 与日志查看。

    ★边界★：这里只治"**投递失败**"（信没送到收信端）。"送到了但被唤醒的 agent 没处理完"
    （`delivered_at` 有值、`read` 仍为 false）不在这里重投——那需要区分"它正在处理"与"它死了"，
    现在还没做（见 MAILBOX-LOGIC.md §13）。
    """
    home = Path(home)
    if max_attempts is None:
        max_attempts = int(os.environ.get("AGENT_ANP_REDELIVER_MAX") or 5)
    rows = core.pending_messages(home, "", due_only=True, undelivered_only=True)
    done, given_up, skipped = [], [], []
    for item in rows[:max(0, int(limit))]:
        mid = str(item.get("id") or "")
        owner = str(item.get("inbox") or "")
        attempts = int(item.get("attempts") or 0)
        if attempts >= max_attempts:
            # 已经放弃过的别再刷一遍（否则每轮都重复写同一个 last_error）
            if not str(item.get("last_error") or "").startswith("已放弃自动重投"):
                # 事件：这封信彻底放弃了（spec §6）。插件要知道哪封信死了，就等这个。
                # 只在**第一次**标记放弃时发，别让每轮扫描都重播同一条。
                EVENTS.emit("message.undelivered", id=item.get("id"),
                            to=owner, from_=item.get("from"),
                            attempts=attempts, max_attempts=max_attempts,
                            last_error=str(item.get("last_error") or ""))
                core.update_message(owner, mid, {
                    "next_retry_at": None,
                    "last_error": f"已放弃自动重投（尝试 {attempts} 次 ≥ 上限 {max_attempts}）；"
                                  f"要再试请手动 mail_redeliver",
                }, home)
                given_up.append(mid)
            continue
        try:
            out = do_redeliver("hub", {"id": mid, "home": str(home)})
            (done if out.get("ok") else skipped).append({"id": mid, "result": out})
        except Exception as e:  # noqa: BLE001
            skipped.append({"id": mid, "error": f"{type(e).__name__}: {e}"})
    return {"ok": True, "due": len(rows), "retried": len(done), "gave_up": len(given_up),
            "skipped": len(skipped), "max_attempts": max_attempts,
            "redelivered": done, "abandoned": given_up, "errors": skipped[:5]}


def start_event_scanner(home: Path | None = None) -> threading.Thread | None:
    """扫 agent.online / agent.offline 的跃迁。

    为什么必须有个线程：TTL 过期**不需要任何心跳也会发生**——一个 agent 就这么不再心跳了。
    只在有人订阅时才扫（没订阅者就纯睡觉，不白扫账本）。
    """
    home = HOME if home is None else Path(home)
    interval = float(os.environ.get("AGENT_ANP_EVENT_SCAN_SEC") or 15)

    def _want() -> bool:
        return bool(EVENTS.subscribers("agent.online") or EVENTS.subscribers("agent.offline"))

    def _run() -> None:
        global _ONLINE_CACHE
        if not _want():
            _ONLINE_CACHE = set(core.online(home=home))
        while True:
            time.sleep(max(5.0, interval))
            try:
                if not _want():
                    continue
                now_online = set(core.online(home=home))
                for name in sorted(now_online - _ONLINE_CACHE):
                    plugin.clear_offline_flag(name, home)
                    EVENTS.emit("agent.online", agent=name)
                for name in sorted(_ONLINE_CACHE - now_online):
                    EVENTS.emit("agent.offline", agent=name,
                                last_seen=(core.agent_table(home).get(name) or {}).get("ts"))
                _ONLINE_CACHE = now_online
            except Exception as e:  # noqa: BLE001 - 扫描线程的任何异常都不许外溢
                LOG.write(f"[hub] 事件扫描出错（忽略，继续跑）：{type(e).__name__}: {e}\n")

    th = threading.Thread(target=_run, name="anp-event-scan", daemon=True)
    th.start()
    return th


def start_maintenance(home: Path | None = None) -> threading.Thread | None:
    """后台线程：定期收箱裁剪 + 账本压缩（`core.prune()` / `core.compact_ledger()`）。

    ★为什么要它★（2026-09-22）：`prune()` 从写下起就**全仓没有调用点**，账本也没有
    压缩逻辑，于是收件箱和账本无限增长——实测账本 4.0 MB / 17045 行，其中 16952 行是
    dsh 一家的心跳（桥接每 30 秒一条），而 `agent_table()` 只看每身份最后一条。
    这不是"已知限制"，是数据生命周期一直没兑现。

    保留策略：未读永不删；账本每身份留最后一条 + 最近 `LEDGER_KEEP` 行。
    设 `AGENT_ANP_MAINTENANCE=0` 关掉；间隔 `AGENT_ANP_MAINTENANCE_SEC`（默认 3600）。
    """
    home = HOME if home is None else Path(home)
    if str(os.environ.get("AGENT_ANP_MAINTENANCE") or "1").strip().lower() in ("0", "false", "no"):
        return None
    interval = max(60.0, float(os.environ.get("AGENT_ANP_MAINTENANCE_SEC") or 3600))
    keep = int(os.environ.get("AGENT_ANP_PRUNE_KEEP") or 500)

    def loop() -> None:
        time.sleep(min(60.0, interval))       # 启动先等一会儿，别和"刚落盘的信"抢
        while True:
            try:
                out = core.maintenance(home, keep_inbox=keep)
                if out.get("inbox_removed") or (out.get("ledger") or {}).get("before"):
                    with STORE_LOCK:
                        MAINT_STATE["last"] = out
                    LOG.write("[hub] 数据维护：收件箱删 %d 封，账本 %s → %s 行\n"
                              % (out.get("inbox_removed") or 0,
                                 (out.get("ledger") or {}).get("before"),
                                 (out.get("ledger") or {}).get("after")))
            except Exception as e:  # noqa: BLE001 - 维护失败不能影响服务
                LOG.write(f"[hub] 数据维护出错（不影响服务）: {type(e).__name__}: {e}\n")
            time.sleep(interval)

    th = threading.Thread(target=loop, name="AgentNetPort-maintenance", daemon=True)
    th.start()
    return th


def start_auto_redeliver(home: Path | None = None) -> threading.Thread | None:
    """后台线程：每 `AGENT_ANP_REDELIVER_SEC`（默认 120 秒）扫一次到期的信。

    设 `AGENT_ANP_AUTO_REDELIVER=0` 可关掉。线程是 daemon，hub 退出即随之结束；
    hub 的优雅停止会等在途请求收尾，这里不占 INFLIGHT（它自己就是发起方）。
    """
    home = HOME if home is None else Path(home)
    if str(os.environ.get("AGENT_ANP_AUTO_REDELIVER") or "1").strip().lower() in ("0", "false", "no"):
        return None
    interval = max(15.0, float(os.environ.get("AGENT_ANP_REDELIVER_SEC") or 120))
    limit = max(1, int(os.environ.get("AGENT_ANP_REDELIVER_LIMIT") or 3))

    def loop() -> None:
        # 启动先等一会儿：避免和"刚落盘、正被正常投递"的信抢
        time.sleep(min(30.0, interval))
        while True:
            try:
                out = auto_redeliver(home, limit=limit)
                if out.get("retried") or out.get("gave_up"):
                    LOG.write(f"[hub] 自动重投：到期 {out['due']} 封，重投 {out['retried']} 封，"
                                     f"放弃 {out['gave_up']} 封\n")
                    with STORE_LOCK:
                        AUTO_STATE["last"] = {**out, "at": time.time()}
            except Exception as e:  # noqa: BLE001
                LOG.write(f"[hub] 自动重投出错（不影响服务）: {type(e).__name__}: {e}\n")
            time.sleep(interval)

    th = threading.Thread(target=loop, name="AgentNetPort-auto-redeliver", daemon=True)
    th.start()
    return th


def start_webui_companion(home: Path | None = None, hub_port: int | None = None) -> Any:
    """把只读看板作为**伴生进程**拉起：hub 在，它就在（主人 2026-09-21 定）。

    为什么放在 hub 里而不是让谁手工起：看板是信箱的一个视图，"信箱开着它就该开着"。
    之前的做法是"在某个 shell 里起一下"，那个 shell 一收尾看板就没了——主人打开浏览器就是"打不开"。

    规则：
      - 端口已在监听（可能是我上次留下的孤儿，或主人自己起的）→ 不重复拉起，但仍记下"不是我起的"；
      - `AGENT_ANP_WEBUI=0` 可关掉这个伴生行为；
      - 拉不起来（比如 python 路径不对）只记日志，**不影响 hub 本身**。
    """
    home = HOME if home is None else Path(home)
    if str(os.environ.get("AGENT_ANP_WEBUI") or "1").strip().lower() in ("0", "false", "no"):
        LOG.write("[hub] 看板伴生：已关闭（AGENT_ANP_WEBUI=0）\n")
        return None
    port = int(os.environ.get("AGENT_ANP_WEBUI_PORT") or 8898)
    host = os.environ.get("AGENT_ANP_WEBUI_HOST") or "127.0.0.1"
    # ★2026-09-26 外部审计★：这里一直找 `src/mailbox_webui.py`，而 WebUI 早在
    # 9 月 25 日迁到了 `webui/desktop.py`（不再是 src/ 的伴生兄弟）。结果是
    # "默认开启 + 静默跳过"——hub 日志里只有一行"找不到 xxx，跳过"，看板其实从来没起来。
    # 新位置按仓库布局找：src/ 的上一级（仓库根）/ webui/desktop.py。
    script = Path(__file__).resolve().parents[1] / "webui" / "desktop.py"
    if not script.is_file():
        LOG.write(f"[hub] 看板伴生：找不到 {script}，跳过"
                  f"（WebUI 已迁到 webui/desktop.py；要关掉伴生设 AGENT_ANP_WEBUI=0）\n")
        return None

    # ★必须问清"是谁在听"，不能只问"有没有人在听"★
    # 只用 connect_ex 的旧写法把"端口被别的东西占了"当成"看板在"：实测一个探针 hub
    # 占了 8898，于是两边都认定看板好好儿的、谁都不重启，主人看到的是 hub 的 404。
    alive, who = core.webui_alive(host, port)
    if alive:
        LOG.write(f"[hub] 看板伴生：{host}:{port} 已有看板在听（不是我起的，我不动它）\n")
        return None
    if who != "无响应（URLError）" and not who.startswith("无响应"):
        # 端口被非看板占着：说清楚是谁，别让人以为看板活着
        LOG.write(f"[hub] 看板伴生：{host}:{port} 被 **{who}** 占着，不是看板 —— "
                  f"看板起不来（请先让那位挪窝，或换 AGENT_ANP_WEBUI_PORT）\n")
        return None
    if int(hub_port or 0) == port:
        # 自己就绑在这个端口上：别把"我自己"当成占用者（探针 hub 把 --port 设成 8898 时踩过）
        LOG.write(f"[hub] 看板伴生：看板端口 {port} 与 hub 端口相同，跳过（换 AGENT_ANP_WEBUI_PORT）\n")
        return None

    import subprocess

    logdir = Path(home) / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    out = open(logdir / "webui.out.log", "a", encoding="utf-8")
    err = open(logdir / "webui.err.log", "a", encoding="utf-8")
    # 把 hub 自己的地址传给它：看板会周期性探活，**hub 不在了它自己退**——
    # 这是"信箱在它就在"最可靠的一层（强杀 hub 时也能生效，不依赖信号处理器）。
    hub_url = (f"http://{os.environ.get('AGENT_ANP_HOST') or '127.0.0.1'}:"
               f"{hub_port or os.environ.get('AGENT_ANP_PORT') or 8900}")
    # desktop.py 的接口是环境变量（它要同时服务"人双击启动"与"被 hub 拉起"两种场景），
    # 所以 hub 地址与 home 走 env，不走 argv。
    # ★--no-browser / --no-sync-adapters 是必须的★：伴生的是"信箱的一个视图"，
    #   不该弹浏览器窗口；而 sync_adapters 会反过来 POST 本 hub 的
    #   `/plugins/<id>/unload`（还要再 load）——hub 启动途中自己卸自己，纯属自伤。
    cmd = [sys.executable, str(script), "--host", host, "--port", str(port),
           "--no-browser", "--no-sync-adapters"]
    child_env = {**os.environ, "AGENT_ANP_HUB_URL": hub_url, "AGENT_ANP_HOME": str(home),
                 # ★看门狗开关★：只有"被 hub 拉起"的看板才在 hub 消失时自己退。
                 # 没有它，每个 hub 死后都留一个 idle 的看板进程（2026-09-26 实测：
                 # 跑一轮回归留下 100+ 个孤儿，各自占着一个随机端口）。
                 "ANP_WEBUI_COMPANION": "1"}
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(  # noqa: S603
            cmd, stdout=out, stderr=err, stdin=subprocess.DEVNULL, creationflags=creation,
            close_fds=True, env=child_env)
    except Exception as e:  # noqa: BLE001
        LOG.write(f"[hub] 看板伴生：起不来（{type(e).__name__}: {e}）—— hub 继续跑\n")
        return None
    LOG.write(f"[hub] 看板伴生：pid={proc.pid} → http://{host}:{port}/（hub 停它就停）\n")
    WEBUI_PROC["proc"] = proc
    # 兜底一：解释器退出时也收（异常/未捕获路径）
    import atexit

    atexit.register(lambda: stop_webui_companion(WEBUI_PROC.get("proc")))
    # 兜底二：Windows 上给子进程挂 Job 对象，"父进程死则子进程一起死"（最硬的一层，
    # 连强杀 hub 都能带走看板）。没有 pywin32 就跳过，不影响功能。
    try:
        import ctypes  # noqa: PLC0415

        job = ctypes.windll.kernel32.CreateJobObjectW(None, None)
        if job:
            class _Limits(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                            ("PerJobUserTimeLimit", ctypes.c_longlong),
                            ("LimitFlags", ctypes.c_uint32),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", ctypes.c_uint32),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", ctypes.c_uint32),
                            ("SchedulingClass", ctypes.c_uint32)]

            info = _Limits()
            info.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ctypes.windll.kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            handle = int(proc._handle)        # noqa: SLF001 - 标准库 Popen 的进程句柄
            ctypes.windll.kernel32.AssignProcessToJobObject(job, handle)
            WEBUI_PROC["job"] = job
    except Exception as e:  # noqa: BLE001
        LOG.write(f"[hub] 看板伴生：Job 对象兜底不可用（{type(e).__name__}），"
                         f"仍会在 hub 停止时收掉\n")
    return proc


def stop_webui_companion(proc: Any) -> None:
    """hub 退出时把伴生看板一起收掉（只收我起的那个；主人自己起的不动）。"""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()
            LOG.write("[hub] 看板伴生：已随 hub 停止\n")
    except Exception as e:  # noqa: BLE001
        LOG.write(f"[hub] 看板伴生：停止时出错（{type(e).__name__}: {e}）\n")


def main() -> int:
    global HOME  # noqa: PLW0603

    ap = argparse.ArgumentParser(description="AgentNetPort hub")
    ap.add_argument("--host", default=os.environ.get("AGENT_ANP_HOST") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("AGENT_ANP_PORT") or 8900))
    ap.add_argument("--home", default=str(HOME))
    args = ap.parse_args()

    HOME = Path(args.home)
    HOME.mkdir(parents=True, exist_ok=True)
    core.heartbeat("hub", HOME, description="AgentNetPort hub", client="hub")

    global HUB_PORT
    HUB_PORT = int(args.port)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    # 优雅停止：收到 SIGTERM/SIGINT 时等正在处理的请求收尾（最多 8 秒），
    # 否则"投递结果没记上"这类竞态会留下 attempts=0 / delivered_at=null 的假状态。
    def _graceful(signum, _frame):  # noqa: ANN001
        LOG.write(f"[hub] 收到信号 {signum}，等待在途请求收尾…\n")
        with INFLIGHT_LOCK:
            INFLIGHT["draining"] = True
            INFLIGHT["cv"].wait_for(lambda: INFLIGHT["n"] == 0, timeout=8)
            left = INFLIGHT["n"]
        if left:
            LOG.write(f"[hub] 仍有 {left} 个请求未收尾，直接退出\n")
        # 伴生看板随 hub 一起停：**信号路径必须显式收**——实测 CTRL_BREAK 时
        # `serve_forever` 的 finally 不一定会跑到，看板会变成孤儿继续占端口。
        stop_webui_companion(WEBUI_PROC.get("proc"))
        LOG.close()          # 退出前把队列里剩下的尽量落盘
        raise SystemExit(0)

    try:
        import signal

        signal.signal(signal.SIGTERM, _graceful)
        signal.signal(signal.SIGINT, _graceful)
    except (ImportError, ValueError):
        pass

    LOG.write(f"[hub] listening http://{args.host}:{httpd.server_address[1]}  home={HOME}\n")
    # 看板/会话大脑的管理员令牌：这两个入口能看**全部正文**或**工作区路径**，所以上了令牌。
    # ★只打前 8 位★（2026-09-26 起）：这枚令牌能读全部 agent 通信，而 hub.log 会被
    # spawn_hub 重定向、会被贴进 issue、会被同步工具搬走——整枚写进日志等于把钥匙贴在门外。
    # 完整令牌只在 `<home>/admin.token`（权限已收紧）里，第一次进看板从那儿取。
    _tok = core.admin_token(HOME)
    LOG.write(f"[hub] 管理员令牌（看板 /api/state 与 GET /sessions 用）：{_tok[:8]}…"
              f"（只显示前 8 位；完整令牌在 {core.admin_token_path(HOME)}）\n")
    LOG.write(f"[hub] 身份密钥：{'强制要求' if core.require_key(HOME) else '宽松（AGENT_ANP_REQUIRE_KEY=0）'}"
                     f"，共 {len(core.read_keys(HOME))} 个身份已发密钥\n")
    AUTO_STATE["interval"] = float(os.environ.get("AGENT_ANP_REDELIVER_SEC") or 120)
    AUTO_STATE["limit"] = int(os.environ.get("AGENT_ANP_REDELIVER_LIMIT") or 3)
    # ★先重绑再启动★：模块级单例在 import 时只有默认 home
    EVENTS.bind_home(HOME)
    EVENTS.start()
    start_event_scanner(HOME)
    mth = start_maintenance(HOME)
    MAINT_STATE["enabled"] = mth is not None
    MAINT_STATE["interval"] = max(60.0, float(os.environ.get("AGENT_ANP_MAINTENANCE_SEC") or 3600))
    LOG.write("[hub] 数据维护："
              + (f"每 {MAINT_STATE['interval']:.0f} 秒一次"
                 f"（收件箱保留最近 {int(os.environ.get('AGENT_ANP_PRUNE_KEEP') or 500)} 封 + 全部未读；"
                 f"账本每身份一条 + 最近 {core.LEDGER_KEEP} 行）\n"
                 if mth else "已关闭（AGENT_ANP_MAINTENANCE=0）\n"))
    th = start_auto_redeliver(HOME)
    # ★P0-1（2026-09-23）：先 accept，再 preload★。
    # 原来是 bind → plugin_reload_all → serve_forever，于是整个预加载期间
    # （每个插件 register_out 最长等 REGISTER_TIMEOUT=10s，多插件叠乘）
    # `/health`、`/admin/*` 全连不上或挂在 backlog 里；更糟的是插件若在
    # `/anp/register` 或任何同步路径里**回调 Hub**，而 Hub 还没 accept
    # → 对等死锁，直到超时。
    # 现在 serve 线程先起来（Hub 立刻可服务），preload 在它后面跑。
    serve_th = threading.Thread(target=httpd.serve_forever,
                                name="AgentNetPort-http", daemon=True)
    serve_th.start()
    LOG.write("[hub] HTTP 服务已开始 accept（插件预加载在其后）\n")

    # 插件：扫 plugins/ 并逐个 load（spec §3）。没有 plugins 目录就什么都不做。
    # ★坏掉的 manifest 只让那一个插件没起来，绝不让 hub 起不来★——插件是上层，不是本体。
    try:
        preload = plugin_reload_all(HOME)
        if preload["attempted"]:
            bad = [f"{x['id']}({x.get('code')})" for x in preload["items"] if not x.get("ok")]
            LOG.write(f"[hub] 插件预加载：{preload['loaded']}/{preload['attempted']} 起来"
                      + (f"；没起来的：{bad}" if bad else "") + "\n")
        PRELOAD_STATE.update({"done": True, "attempted": preload["attempted"],
                              "loaded": preload["loaded"]})
    except Exception as e:  # noqa: BLE001 - 插件这事不该拖垮信箱本体
        LOG.write(f"[hub] 插件预加载失败（忽略，信箱继续跑）：{type(e).__name__}: {e}\n")
        # ★失败也要置 done★：否则调用方会永远等一个不会来的信号
        PRELOAD_STATE.update({"done": True})
    AUTO_STATE["enabled"] = th is not None
    LOG.write("[hub] 自动重投："
              + (f"每 {AUTO_STATE['interval']:.0f} 秒扫一次（每轮最多 {AUTO_STATE['limit']} 封）\n"
                 if th else "已关闭（AGENT_ANP_AUTO_REDELIVER=0）\n"))
    # ★看板是信箱的伴生服务★（主人 2026-09-21 定："邮箱开着它就开着"）：
    # hub 起它就起，hub 停它就停；不用谁另外记得去起一个。
    webui_proc = start_webui_companion(HOME, hub_port=httpd.server_address[1])
    WEBUI_PROC["proc"] = webui_proc
    try:
        # serve 线程已经在跑，这里只是把主线程挂住等它结束
        while serve_th.is_alive():
            serve_th.join(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        stop_webui_companion(webui_proc)
        WEBUI_PROC["proc"] = None
        LOG.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
