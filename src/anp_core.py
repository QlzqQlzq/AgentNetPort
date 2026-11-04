"""AgentNetPort 核心：零依赖的信箱存储 + 唤醒投递。

设计要点
- 存储：每个身份一个 JSONL 收件箱，追加写 + 跨进程文件锁，进程崩溃也不丢信。
- 唤醒：send 之后由 hub 负责"叫醒"收件人（HTTP 回调 / 拉起子进程），存储与唤醒解耦。
- 身份：任意短名字（codex / dsh / ...），hub 的 registry 记录每个身份的唤醒端点。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Plugin API 代次（v0.1 规范）：**整数**，只在破坏性变更时 +1，纯增永不破坏。
# Core 与插件各报一个整数，加载规则只有两条：
#     plugin.api_level >  CORE_API_LEVEL → 拒绝（api.level.unsupported）
#     plugin.api_level <= CORE_API_LEVEL → 尝试加载（尽力向后兼容）
# 刻意不用 semver range：pre-1.0 阶段让第三方去理解 "^0.3.0 || >=0.4" 没有收益。
CORE_API_LEVEL = 1

DEFAULT_HOME = Path(os.environ.get("AGENT_ANP_HOME") or (Path.home() / ".agentnetport"))
DEFAULT_HUB = os.environ.get("AGENT_ANP_HUB") or "http://127.0.0.1:8900"
DEFAULT_SELF = os.environ.get("AGENT_ANP_SELF") or "unknown"

REPLACEMENT = "\ufffd"


def clean_text(value: Any) -> str:
    """把任意文本规范成"一定能编码成 UTF-8"的字符串。

    模型偶尔会吐出孤立代理字符（例如截断的 emoji 残成 `\\udc97`），这种字符串
    送进 utf-8 编码器会直接抛 UnicodeEncodeError，导致整封信发不出去。这里统一
    清洗：能还原的代理对还原成真字符，孤立代理按字节替换成 U+FFFD，不丢整封信。
    """
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    if not text:
        return text
    if any("\ud800" <= ch <= "\udfff" for ch in text):
        try:
            fixed = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
            if not any("\ud800" <= ch <= "\udfff" for ch in fixed):
                return fixed
            text = fixed
        except (UnicodeError, ValueError):
            pass
        text = text.encode("utf-8", "replace").decode("utf-8", "replace")
    # 再兜一层：控制字符之外的不可编码字符统一替换
    if any("\ud800" <= ch <= "\udfff" for ch in text):
        text = "".join(REPLACEMENT if "\ud800" <= ch <= "\udfff" else ch for ch in text)
    return text


def safe(obj: Any) -> Any:
    """递归清洗 dict / list / str，保证 json.dumps + utf-8 编码不会炸。"""
    if isinstance(obj, str):
        return clean_text(obj)
    if isinstance(obj, dict):
        return {clean_text(k): safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [safe(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- #
# 非阻塞日志（★2026-09-22：修掉"日志把服务卡死"这个可用性故障★）
# --------------------------------------------------------------------------- #
# 故障实况：hub 的 stderr 常被调用方重定向进一个**没人读的管道**（Windows 匿名管道缓冲区
# 只有几 KB）。写满之后 `sys.stderr.write` 会**阻塞**——整个 hub 停止响应，连 /health
# 都不通，看起来像死锁。A/B 实测：发行包旧代码与当前代码都在约 103 个请求后出现。
# 每个请求都打一行访问日志，所以任何正常用量都能把它推到这个点。
#
# 原则：**日志是旁路，绝不允许它卡住主路。** 所以写日志 = 塞队列，队列满就丢并计数；
# 真正的落盘由后台线程做。丢了日志不影响一封信，卡住服务会。
class AsyncLog:
    """非阻塞日志。构造后与 `sys.stderr` 一样 `.write()` 就行。"""

    def __init__(self, stream: Any = None, maxsize: int | None = None):
        self._stream = stream if stream is not None else sys.stderr
        size = maxsize if maxsize is not None else int(
            os.environ.get("AGENT_ANP_LOG_QUEUE") or 2000)
        self._q: queue.Queue = queue.Queue(maxsize=max(16, size))
        self._dropped = 0
        self._written = 0
        self._closed = False
        threading.Thread(target=self._worker, name="mailbox-asynclog", daemon=True).start()

    def write(self, text: str) -> None:
        if not text or self._closed:
            return
        try:
            self._q.put_nowait(text)
        except queue.Full:
            # 丢**新的**、保旧的：日志开头通常才是根因，末尾的重复噪声最可弃
            self._dropped += 1

    def flush(self) -> None:
        """尽力排空（供退出前调用；不保证，写不进就算了）。"""
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if self._q.empty():
                return
            time.sleep(0.02)

    def close(self, timeout: float = 2.0) -> None:
        self.flush()
        self._closed = True

    def stats(self) -> dict[str, int]:
        return {"queued": self._q.qsize(), "dropped": self._dropped, "written": self._written}

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                self._stream.write(item)
                self._stream.flush()
                self._written += 1
            except Exception:  # noqa: BLE001 - 日志写失败只能忍，绝不能让线程死掉
                self._dropped += 1



# --------------------------------------------------------------------------- #
# 账本（身份注册表）
# --------------------------------------------------------------------------- #
def ledger_path(home: Path = DEFAULT_HOME) -> Path:
    return Path(home) / "ledger.jsonl"


def read_ledger(home: Path = DEFAULT_HOME) -> list[dict[str, Any]]:
    p = ledger_path(home)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in read_text_shared(p).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def agent_table(home: Path = DEFAULT_HOME) -> dict[str, dict[str, Any]]:
    """把账本折成 name -> 最新一条记录。"""
    table: dict[str, dict[str, Any]] = {}
    for row in read_ledger(home):
        name = str(row.get("name") or "").strip()
        if name:
            table[name] = row
    return table


# 心跳里"一旦登记过就该留住"的字段：后来的心跳**没给**这些字段时，沿用账本里已有的值。
# ★为什么必须合并★（2026-09-21 实测）：账本语义是"取每个身份最后一条"，所以一条**不带**
# `process_api` 的心跳会把之前登记好的往生 API 抹掉——实测桥接每 30 秒一次的心跳把我手工登记的
# `dsh.process_api` 反复清空。登记态是信箱要维护的状态，不该被"这次没提"变成"没有"。
# `skills` 也在内：它是身份自报的**任务能力**（自由词表，Core 不解释），和 `caps`
# （平台能力）是两回事。没有这条的话，一次不带 skills 的心跳就会把它抹掉。
STICKY_FIELDS = ("wake", "caps", "skills", "process_api", "description", "workspace", "client")


def heartbeat(name: str, home: Path = DEFAULT_HOME, **extra: Any) -> dict[str, Any]:
    """登记/刷新一个身份（唤醒端点、描述、标签、进程 API）。

    合并规则：本次**显式给了**的字段覆盖旧值；没给的字段沿用旧值（见 `STICKY_FIELDS`）。
    想主动清掉某个字段，显式传 `None`（例如 `process_api=None`）。
    """
    who = str(name or "").strip()
    prev = agent_table(home).get(who, {}) if who else {}
    merged: dict[str, Any] = {}
    for field in STICKY_FIELDS:
        if field in extra:
            if extra[field] is not None:
                merged[field] = extra[field]
            # 显式 None → 不写（等于清掉）
        elif prev.get(field) is not None:
            merged[field] = prev[field]
    rec = safe({
        "ts": time.time(),
        "name": who,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        **merged,
        **extra,
    })
    append_line(ledger_path(home), rec)
    return rec


# --------------------------------------------------------------------------- #
# 存储（每身份一个 JSONL 收件箱）
# --------------------------------------------------------------------------- #
def _lock(fp) -> None:
    fp.seek(0)
    for _ in range(200):
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError("AgentNetPort: 等待文件锁超时")


def _unlock(fp) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


_PATH_LOCK_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    """Return a process-local lock matching the cross-process sidecar lock."""
    key = os.path.abspath(str(path))
    if os.name == "nt":
        key = key.lower()
    with _PATH_LOCK_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


@contextmanager
def _mutation_lock(path: Path):
    """Serialize JSONL mutations across threads and processes.

    The data file itself still gets a byte lock while it is read or appended,
    preserving the existing Windows-reader retry behavior. The sidecar is
    what makes a read/modify/atomic-replace operation coordinate with appenders
    without holding an open handle to the file being replaced.
    """
    path = Path(path)
    local_lock = _path_lock(path)
    local_lock.acquire()
    sidecar = path.with_name(path.name + ".lock")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    lock_fp = None
    try:
        lock_fp = open(sidecar, "a+b")
        if os.fstat(lock_fp.fileno()).st_size == 0:
            lock_fp.write(b"\0")
            lock_fp.flush()
        _lock(lock_fp)
        yield
    finally:
        if lock_fp is not None:
            _unlock(lock_fp)
            lock_fp.close()
        local_lock.release()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text durably and replace the target in one filesystem operation."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fp:
            fp.write(text)
            fp.flush()
            try:
                os.fsync(fp.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _jsonl_text(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(safe(row), ensure_ascii=False) + "\n" for row in rows)


# 在线判定：账本里最近一次心跳在这个秒数内，就算"在线"（各端心跳间隔 30s 量级）
ONLINE_TTL = float(os.environ.get("AGENT_ANP_ONLINE_TTL") or 300)


def online(*, ttl: float = ONLINE_TTL, home: Path = DEFAULT_HOME,
           exclude: set[str] | None = None) -> list[str]:
    """在线身份：账本里最近一次心跳在 ttl 秒内的身份（hub 自身不算）。

    只接受关键字参数：这个函数原来写成 `online(ttl, home, exclude)`，
    结果 `online(home)` 会把 home 当成 ttl 用（实测炸过一次），关键字-only 直接堵掉这种误用。
    """
    now = time.time()
    skip = {str(x).strip() for x in (exclude or set())}
    out = []
    for name, row in agent_table(home).items():
        if name == "hub" or name in skip:
            continue
        if now - float(row.get("ts") or 0) <= ttl:
            out.append(name)
    return sorted(out)


def append_line(path: Path, record: dict[str, Any]) -> None:
    """带锁追加一行 JSON。写前做一次文本清洗，保证落盘内容一定可编码。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(safe(record), ensure_ascii=False) + "\n"
    with _mutation_lock(path):
        with open(path, "a+", encoding="utf-8") as fp:
            _lock(fp)
            try:
                fp.seek(0, os.SEEK_END)
                fp.write(line)
                fp.flush()
                try:
                    os.fsync(fp.fileno())
                except OSError:
                    pass
            finally:
                _unlock(fp)


def _safe(name: str) -> str:
    """把身份名 / 关联线 id 编码成可安全落盘的文件名。**必须是单射**。

    ★为什么从"过滤"改成"编码"★（2026-09-26 外部审计复现实测）：
    旧实现是"滤掉不认识的字符"，于是 `a/b`、`a:b`、`ab` **全被滤成 `ab`**——
      · 身份名：不同身份共用同一个收件箱文件，能互相读到、签收、重投对方的信
        （数据隔离失效，实测复现）；
      · 关联线 id：不同关联线共用同一个 `journals/<id>.jsonl`，而授权判据是
        共享文件头里的 owner → 关联线内容混淆 / 越权读。
    过滤天然不单射，能修它的只有编码：不认识的字符按 UTF-8 字节百分号转义
    （`%` 自己转成 `%25`）。`a/b → a%2Fb`、`a:b → a%3Ab`、`ab → ab`，三者互不相同。
    只含 `[alnum -_.@]` 的名字（现有全部身份）输出**一字不变**，老收件箱照旧命中。

    逆变换见 `_unsafe()`（展示面用）。空串仍回落 `"unknown"`——上游（`send` /
    `/heartbeat`）都不允许空身份名，所以它只可能对上字面量叫 `unknown` 的身份。
    """
    out: list[str] = []
    for ch in str(name):
        if ch.isalnum() or ch in "-_.@":
            out.append(ch)
        else:
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
    return "".join(out) or "unknown"


def _unsafe(name: str) -> str:
    """`_safe()` 的逆变换：把文件名还原成原始身份名（给 `/agents` 这类展示面）。

    宽容解码：遇到不是 `%XX` 的 `%` 就按原样留下，绝不在展示面抛异常。
    没有 `%` 的文件名直接返回本身（绝大多数情况，零成本）。
    """
    raw = str(name)
    if "%" not in raw:
        return raw
    out: list[str] = []
    buf = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == "%" and i + 3 <= len(raw):
            try:
                buf.append(int(raw[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        if buf:
            out.append(buf.decode("utf-8", "replace"))
            buf.clear()
        out.append(raw[i])
        i += 1
    if buf:
        out.append(buf.decode("utf-8", "replace"))
    return "".join(out)


def inbox_path(name: str, home: Path = DEFAULT_HOME) -> Path:
    return Path(home) / "inbox" / f"{_safe(name)}.jsonl"


def _retry_perm(fn, attempts: int = 40, sleep: float = 0.03):
    """对"文件被别的写者锁住"做退避重试。

    ★为什么必须有★（2026-09-20 审阅实测）：Windows 的 `msvcrt.locking` 是**字节锁**，
    写者锁住 1 字节期间，读者的 `read_text` 会直接抛 `PermissionError`（不是读到半截，是硬失败）。
    实测真账本并发下约 25% 的读失败；`/health` 一挂，客户端就把 hub 判死 → 降级 direct + 拉起冗余 hub。
    配合把 `append_line` 的 `fsync` 挪出锁区，读到的窗口从"几十毫秒"缩到"一毫秒级"，重试即可稳。
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except PermissionError as e:      # Windows：文件正被别的进程锁着
            last = e
            time.sleep(sleep * (1 + i / 4))
        except OSError as e:
            if getattr(e, "errno", None) not in (13, 11):   # EACCES / EAGAIN
                raise
            last = e
            time.sleep(sleep * (1 + i / 4))
    raise last if last else RuntimeError("读文件重试耗尽")


def read_text_shared(path: Path) -> str:
    """读文件，容忍并发的写者（Windows 字节锁下重试）。

    ★连 `exists()` 也要包进来★：Windows 上 `Path.exists()` 在文件被写者持锁时同样会抛
    `PermissionError`（实测：光修 `read_text` 还剩 2/120 失败，全出在这一句）。
    文件不存在 → 返回空串（调用方按"空"处理，与原先 `if not p.exists(): return []` 等价）。
    """
    p = Path(path)

    def _read() -> str:
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8", errors="replace")

    return _retry_perm(_read)


def read_records(name: str, home: Path = DEFAULT_HOME) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in read_text_shared(inbox_path(name, home)).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def message_id() -> str:
    return "m-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def recipients_of(raw: Any, sender: str = "") -> list[str]:
    """解析收件人：支持 `to="codex"` 与 `to=["codex","mimo"]` 两种写法。

    ★为什么放在 core 而不是 hub★（2026-09-21 陈的复审逼出来的）：
    这个解析原本只在 hub 里有一份，于是 hub 可达时多人信能正确拆成 N 份副本，
    **hub 不可达走 direct 降级时没有这个解析**——`to` 是列表时被字符串化成
    `"['codex', 'dsh']"` 再过滤成 `codexdsh`，投给一个不存在的身份，静默丢信。
    同一语义只允许有一份实现（"两处实现"我们已经栽过三次），所以下沉到这里，两条路径共用。

    规则与协议一致：去空、去重、跳过发信人自己、跳过 hub（它不是收信人）。
    """
    names = [str(x).strip() for x in raw] if isinstance(raw, (list, tuple, set)) else [str(raw or "").strip()]
    out: list[str] = []
    me = clean_text(sender).strip()
    for name in names:
        if not name or name in out:
            continue
        if me and name == me:
            # 自发自收由 send() 兜底拒绝；这里跳过，避免整封信失败
            continue
        if name == "hub":
            continue
        out.append(name)
    return out


def send(
    sender: str,
    to: str,
    body: str,
    *,
    subject: str | None = None,
    reply_to: str | None = None,
    kind: str = "message",
    correlation_id: str | None = None,
    meta: dict[str, Any] | None = None,
    need_reply: bool = False,
    home: Path = DEFAULT_HOME,
    id: str | None = None,
) -> dict[str, Any]:
    """投一封信进 to 的收件箱（只负责落盘，不负责唤醒）。

    拒收"发信人 == 收件人"：那类信没有真实寄件人，却会在收信端长得跟真来信一模一样
    （信封上印着"发信: <自己>"，"回信"就是自己回自己），既是死循环的入口，
    也会被用来伪造"某人给我写了信"的假象（2026-09-20 实测：探针冒用 codex 之名给自己写信，
    在信封上与真来信无法区分）。要给自己留底请写文件或发给 hub 存档，不要占用收信通道。

    `correlation_id`：**关联线**（任务 / 游戏 / 一次协作的 id）。与 `meta.context_id` 是两件事：
      · `context_id` 是**平台会话线**，被会话决策消费（哪封信落进哪个会话）；
      · `correlation_id` 是**应用关联线**，Core 只携带、索引、**绝不解释**。
    带 correlation_id 的信会额外落一份到 `journals/<id>.jsonl`，且这条关联线的
    **创建者（第一个发送者）**会被记下来，之后只有创建者能读这条线（见 journal_owner）。

    `id`：**多人信共用同一个 message id**（主人 2026-09-21 定："同一封信 N 份副本"）。
    不传则各自新生成——单人信就该是新 id。
    """
    to = clean_text(to).strip()
    if not to:
        raise ValueError("收件人不能为空")
    me = clean_text(sender).strip() or "unknown"
    if me == to:
        raise ValueError(
            f"收件人不能是发信人自己（from == to == {to}）："
            "这类信没有真实寄件人，收信端无法与真来信区分。要给自己留底请直接写文件。"
        )
    body_text = clean_text(body)
    if not body_text.strip():
        raise ValueError("正文不能为空")
    cid = clean_text(correlation_id or "").strip() or None
    rec = {
        "id": clean_text(id).strip() or message_id(),
        "ts": time.time(),
        "from": me,
        "to": to,
        "subject": clean_text(subject or "").strip(),
        "body": body_text,
        "kind": clean_text(kind),
        "reply_to": clean_text(reply_to) or None,
        "correlation_id": cid,
        "read": False,
        "meta": safe(dict(meta or {}, **({"need_reply": True} if need_reply else {}))),
        # 状态机初值：落盘那一刻**还没有投递**，所以 next_retry_at 先留空。
        # ★别再写成"立刻到期"★：那会让自动重投的扫描线程把"正在投递中"的信也当成待重投，
        # 同一封信被投两遍（2026-09-20 实测复现：慢收信端 3 秒，扫描在 0.8 秒时插一脚 → 唤醒 2 次）。
        # 投递方在真正动手前用 claim_lease() 占位，record_delivery() 再写最终结果。
        "attempts": 0,
        "delivered_at": None,
        "next_retry_at": None,
    }
    rec["sig"] = sign_envelope(rec, home)      # ★信封内容签名，状态机字段不签★
    append_line(inbox_path(to, home), rec)
    if cid:
        _journal_record(home, cid, me, rec)
    return rec


# --------------------------------------------------------------------------- #
# 关联线日志（journals）—— 消息记录，Plugin API 的地基
# --------------------------------------------------------------------------- #
# 为什么需要它：`mail_inbox` 只能读自己那一箱且不带过滤；唯一的"全量查询"实现在看板的
# `collect()` 里，而它是个界面不是 API。插件（狼人杀 / Multi-Agent Coding）要重建一件事的
# 全过程，就只能去直读别人的收件箱文件——那正是"插件必须知道 Core 数据结构"的反模式。
#
# 做法：带 `correlation_id` 的信，除了进收件箱，**再落一份**到 `journals/<id>.jsonl`。
#   第一行是**头记录**：`{"journal": true, "owner": <第一个发送者>, "created_at": ...}`。
# 授权规则只有一条：**只有 owner 能读这条线**（或 hub 自己 / 管理员令牌）。
# 不建任务注册表、不做 KV——关联线的"owner"就写在它自己的文件头里，没有需要同步的第二份状态。
JOURNAL_HEAD_KEY = "journal"


def _safe_correlation(correlation_id: str) -> str:
    """关联线 id 落盘前的清洗：与 `_safe()` 同一套字符集，避免目录穿越。"""
    return _safe(clean_text(correlation_id).strip())


def journal_path(correlation_id: str, home: Path = DEFAULT_HOME) -> Path:
    return Path(home) / "journals" / f"{_safe_correlation(correlation_id)}.jsonl"


def _journal_record(home: Path, correlation_id: str, sender: str,
                    rec: dict[str, Any]) -> None:
    """把一封信抄进它的关联线日志（首次顺带头记录 = owner 登记）。"""
    p = journal_path(correlation_id, home)
    try:
        fresh = not p.exists() or p.stat().st_size == 0
    except OSError:
        fresh = True
    if fresh:
        head = {"journal": True, "owner": sender, "created_at": time.time(),
                "correlation_id": correlation_id}
        append_line(p, head)
    append_line(p, {k: v for k, v in rec.items() if k != "read"})


def journal_owner(correlation_id: str, home: Path = DEFAULT_HOME) -> str | None:
    """这条关联线的创建者；不存在返回 None。"""
    p = journal_path(correlation_id, home)
    if not p.exists():
        return None
    for line in read_text_shared(p).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get(JOURNAL_HEAD_KEY):
            return str(rec.get("owner") or "") or None
        break           # 第一行不是头记录（老数据/被改过）→ 不当它有 owner
    return None


def read_journal(correlation_id: str, home: Path = DEFAULT_HOME, limit: int = 500) -> list[dict[str, Any]]:
    """读一条关联线的全部信（按时间升序；超 limit 取最后 limit 条）。"""
    p = journal_path(correlation_id, home)
    out: list[dict[str, Any]] = []
    for line in read_text_shared(p).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get(JOURNAL_HEAD_KEY):
            continue
        out.append(rec)
    if limit and len(out) > int(limit):
        return out[-int(limit):]
    return out


def list_journals(home: Path = DEFAULT_HOME) -> list[dict[str, Any]]:
    """全部关联线概览：id、owner、消息数、首末时间。给"我建过哪些线"用。"""
    d = Path(home) / "journals"
    out: list[dict[str, Any]] = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.jsonl")):
        rows = read_journal(p.stem, home, limit=0)
        if not rows:
            continue
        out.append({
            "correlation_id": rows[0].get("correlation_id") or p.stem,
            "owner": journal_owner(p.stem, home),
            "messages": len(rows),
            "first_ts": rows[0].get("ts"),
            "last_ts": rows[-1].get("ts"),
        })
    return out


def find_message(ident: str, home: Path = DEFAULT_HOME) -> tuple[dict[str, Any], str] | None:
    """按 id（支持前缀）在所有收件箱里找一封信。"""
    ident = str(ident or "").strip()
    if not ident:
        return None
    for name in known_inboxes(home):
        for rec in read_records(name, home):
            mid = str(rec.get("id") or "")
            if mid == ident or (len(ident) >= 4 and mid.startswith(ident)):
                return rec, name
    return None


def known_inboxes(home: Path = DEFAULT_HOME) -> list[str]:
    """收件箱文件对应的身份名（**解码回原名**，不是文件名）。

    ★为什么必须解码★：文件名是 `_safe()` 编码后的结果（`a%2Fb.jsonl` ↔ `a/b`）。
    直接把 stem 当身份名返回，`/agents` 会显示编码名，而调用方拿显示名再来读
    （`inbox_path()` 会再编码一次）虽能命中，但"身份名"这件事在展示面就失真了。
    """
    d = Path(home) / "inbox"
    if not d.exists():
        return []
    return sorted({_unsafe(p.stem) for p in d.glob("*.jsonl")})


def browse_messages(
    home: Path = DEFAULT_HOME,
    *,
    inbox: str = "",
    sender: str = "",
    to: str = "",
    unread_only: bool = False,
    kind: str = "",
    correlation_id: str = "",
    q: str = "",
    limit: int = 200,
    offset: int = 0,
    verify_sig: bool = True,
) -> dict[str, Any]:
    """全局只读消息浏览（管理面 / WebUI）。

    ★只读★：不 mark_read、不 claim、不改投递状态。与 `/messages/query` 的
    「关联线 + owner」是两套能力——这里扫**全部收件箱**，不要求 correlation_id。

    过滤后按 `ts` 降序，再做 offset/limit 分页；`totals` 始终是全库 inbox_health 口径。
    """
    inbox = str(inbox or "").strip()
    sender = str(sender or "").strip()
    to = str(to or "").strip()
    kind = str(kind or "").strip()
    correlation_id = str(correlation_id or "").strip()
    q = str(q or "").strip().lower()
    try:
        limit = max(1, min(int(limit or 200), 1000))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0

    names = [inbox] if inbox else known_inboxes(home)
    hits: list[dict[str, Any]] = []
    for name in names:
        if inbox and name != inbox:
            continue
        for rec in read_records(name, home):
            if unread_only and rec.get("read"):
                continue
            if sender and str(rec.get("from") or "") != sender:
                continue
            if to and str(rec.get("to") or "") != to:
                continue
            if kind and str(rec.get("kind") or "message") != kind:
                continue
            if correlation_id and str(rec.get("correlation_id") or "") != correlation_id:
                continue
            if q:
                meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
                hay = " ".join(str(x or "") for x in (
                    rec.get("subject"), rec.get("body"), rec.get("from"),
                    rec.get("to"), rec.get("id"), meta.get("context_id"),
                )).lower()
                if q not in hay:
                    continue
            item = dict(rec)
            item["inbox"] = name
            hits.append(item)

    hits.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    total = len(hits)
    page = hits[offset:offset + limit]
    if verify_sig:
        for rec in page:
            try:
                ok, why = verify_envelope(rec, home)
            except Exception as e:  # noqa: BLE001 - 验签失败不挡浏览
                ok, why = False, f"verify-error: {type(e).__name__}"
            rec["sig_ok"] = ok
            rec["sig_why"] = why
    tampered = [m.get("id") for m in page if m.get("sig_ok") is False]
    return {
        "messages": page,
        "total": total,
        "returned": len(page),
        "offset": offset,
        "limit": limit,
        "tampered": tampered,
        "totals": inbox_health(home).get("totals") or {},
    }


def _inbox_transact(name: str, transform, home: Path = DEFAULT_HOME) -> tuple[int, list[dict[str, Any]]]:
    """带锁地原地改写一个收件箱：transform(rec) 返回要合并的字段（None 表示不改）。

    任何 JSON 解析失败的行原样保留——绝不因为一封信格式坏了就丢掉整箱信。
    返回 (改动条数, 改动后的记录列表)。
    """
    p = inbox_path(name, home)
    changed = 0
    touched: list[dict[str, Any]] = []
    with _mutation_lock(p):
        try:
            fp = open(p, "r+", encoding="utf-8")
        except FileNotFoundError:
            return 0, []
        with fp:
            _lock(fp)
            try:
                fp.seek(0)
                rows: list[str] = []
                for line in fp.read().splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        rec = json.loads(stripped)
                    except json.JSONDecodeError:
                        rows.append(stripped)  # 坏行原样留下
                        continue
                    patch = transform(rec)
                    if patch:
                        rec.update(patch)
                        changed += 1
                        touched.append(rec)
                    rows.append(json.dumps(safe(rec), ensure_ascii=False))
                # ★没有改动就一个字节都别写★（2026-09-20 审阅发现）：
                # 原先无论 changed 是否为 0，都会 truncate + 重写整个文件 + fsync。
                # 收件箱可能上百 KB~MB，一次"签收一个不存在的 id"就能触发整箱重写，
                # 这正是把并发读者（Windows 字节锁下读会被拒）打死的放大器。
                if changed:
                    fp.seek(0)
                    fp.truncate()
                    fp.write("".join(row + "\n" for row in rows))
                    fp.flush()
                    try:
                        os.fsync(fp.fileno())
                    except OSError:
                        pass
            finally:
                _unlock(fp)
    return changed, touched


def claim_lease(name: str, ids: set[str], home: Path = DEFAULT_HOME, by: str = "",
                ttl: float | None = None) -> int:
    """投递占位：动手投之前先"认领"这批信，避免自动重投把正在飞的信又投一遍。

    机制：把 `next_retry_at` 推到 `now + ttl`（默认 `AGENT_ANP_CLAIM_TTL`，300 秒），
    这样扫描线程在租约期内看不到它。投递结束由 `record_delivery` 覆盖：
      - 成功 → `next_retry_at = None`（不再重投）
      - 失败 → `next_retry_at = now + 退避`（等真的到期再重投）
    若投递方中途死掉：租约自然过期，扫描线程到点后照常重投（fail-safe，不会永久卡住）。
    """
    if not ids:
        return 0
    if ttl is None:
        ttl = float(os.environ.get("AGENT_ANP_CLAIM_TTL") or 300)
    now = time.time()
    patch = {"next_retry_at": now + max(30.0, float(ttl)), "claimed_at": now, "claimed_by": str(by or "delivery")}

    def transform(rec: dict[str, Any]) -> dict[str, Any] | None:
        if str(rec.get("id")) in ids and not rec.get("delivered_at"):
            return dict(patch)
        return None

    return _inbox_transact(name, transform, home)[0]


def in_flight(rec: dict[str, Any], now: float | None = None) -> bool:
    """这封信是否"正在投递中"（占了租约）：未投递成功、但 next_retry_at 还在未来。"""
    if rec.get("delivered_at") or rec.get("read"):
        return False
    nr = rec.get("next_retry_at")
    if not nr:
        return False
    return float(nr) > float(now if now is not None else time.time())


def sign_off(name: str, ids: set[str], home: Path = DEFAULT_HOME, by: str = "") -> int:
    """签收：把若干封信标为已读，并记下"是谁签的收"（`read_by` / `read_ts`）。

    ★为什么这件事必须由信箱侧做★（主人 2026-09-20 定）：
    "哪封信读过了"如果指望 agent 自己 `mail_ack`，它想不起来就会永远挂着未读，
    而它在唤醒会话里最不可靠的恰恰就是"记得做收尾动作"。
    所以签收点放在**已知"这封信真的落地了"**的地方：
      - 收信端把信渲染进会话后（http 档桥接）；
      - 唤醒进程成功跑完一轮后（exec 档唤醒器）；
      - 或者 agent 自己主动读（`mail_inbox(peek=false)` / `mail_ack`）——这只是补充路径。
    `read_by` 里写清是谁签的，事后能追"这封是凭什么被标已读的"。
    """
    if not ids:
        return 0
    stamp = {"read": True, "read_ts": time.time(), "read_by": str(by or "sign_off")}

    def transform(rec: dict[str, Any]) -> dict[str, Any] | None:
        if str(rec.get("id")) in ids and not rec.get("read"):
            return dict(stamp)
        return None

    return _inbox_transact(name, transform, home)[0]


def stamp_delivered(name: str, ids: set[str], home: Path = DEFAULT_HOME) -> int:
    """给"已确认收到"的信补写 `delivered_at`（只在为空时写，幂等）。

    ★甲（D1 修复）★：`delivered_at` 的语义是**唤醒器确认收到/处理了**，
    不是"Core 把进程拉起来了"。exec 档 spawn 成功因此不再写它，
    等 agent 自己 ack / 收信时由这里补上。

    刻意**不**在 `sign_off` 里顺手写：`sign_off` 有四个调用点，
    其中 `cache:silent-delivery`（发送方 wake=false）是另一套独立语义，
    不能因为改 D1 就把缓存信的 delivered_at 也一起改掉。
    """
    if not ids:
        return 0

    def transform(rec: dict[str, Any]) -> dict[str, Any] | None:
        if str(rec.get("id")) in ids and not rec.get("delivered_at"):
            # 顺手清掉重投排期：人已经确认收到了，别再让这封信"等着被重投"。
            return {"delivered_at": time.time(), "next_retry_at": None}
        return None

    return _inbox_transact(name, transform, home)[0]


def mark_read(name: str, ids: set[str], home: Path = DEFAULT_HOME) -> int:
    """把收件箱里指定 id 标记为已读（重写整份收件箱，带锁）。"""
    if not ids:
        return 0

    def transform(rec: dict[str, Any]) -> dict[str, Any] | None:
        if str(rec.get("id")) in ids and not rec.get("read"):
            return {"read": True, "read_ts": time.time()}
        return None

    return _inbox_transact(name, transform, home)[0]


# 消息状态机字段（协议 §5）：delivered_at 投递成功 / claimed_at 被消费 /
# replied_at 已回信 / attempts 投递尝试次数 / last_error 最近一次唤醒失败原因 /
# next_retry_at 建议重投时间。全部由信箱自己写，agent 不需要维护。
def update_message(name: str, message_id: str, patch: dict[str, Any], home: Path = DEFAULT_HOME) -> dict[str, Any] | None:
    """按 id 给一封已在收件箱里的信打状态补丁。"""
    if not message_id or not patch:
        return None

    def transform(rec: dict[str, Any]) -> dict[str, Any] | None:
        return dict(patch) if str(rec.get("id")) == str(message_id) else None

    _changed, touched = _inbox_transact(name, transform, home)
    return touched[0] if touched else None


def record_delivery(name: str, outcomes: dict[str, bool], errors: dict[str, str] | None = None,
                    home: Path = DEFAULT_HOME, skipped: set[str] | None = None,
                    no_push: set[str] | None = None,
                    spawned: dict[str, dict[str, Any]] | None = None) -> int:
    """记录一轮投递结果：成功的记 delivered_at、清零重试；失败的累加 attempts 并排重投时间。

    幂等：重复记录同一封信只是把 delivered_at 保持为首次成功时间。

    `skipped`：**发信方明确说"不唤醒"**（`wake=false`）的那些信——它们既不算"投递失败"、
    也不该排重投。以前这里把 `wake=false` 记成失败（`attempts=1` + `next_retry_at`），
    结果自动重投到期后**照样把收件人叫醒了**，直接违背发信方的意图（2026-09-20 审阅实测）。

    `no_push`：**收件人本身就是 queue 档**（没有可推的端点）的那些信（P0-0，2026-09-22）。
    与 `skipped` 是两个不同的原因，绝不能合并：
        skipped   发送方主动选择不唤醒      → wake_skipped = True
        no_push   收件人没有 push 目标      → wake_skipped = "no_push_target"
    两者都不算投递失败、都不排重投，但标记不同——否则"我让它别唤醒"和
    "这个收件人本来就只能自己来取"在状态机里就分不开了。

    `spawned`：**exec 档进程拉起来了，但还没人确认收到**（D1，2026-09-23）。
    值是 mid → 附加字段（如 `{"pid": …}`）。
    ★为什么不走 outcomes={mid: True}★：那会立刻写 `delivered_at`，
    而 exec 的 `ok` 只代表 `Popen` 成功——子进程可能秒崩、可能跑完但不 ack。
    一旦 `delivered_at` 有值，`auto_redeliver` 就**永不**再捞它
    （D1 实测：信永久卡在"已投递但没人读过"，attempts 停在 1）。
    三个事实必须分开：
        attempted = Core 尝试叫醒了          （attempts / last_attempt_at）
        spawned   = 进程成功启动了           （spawned_at / wake_pid）
        delivered = 唤醒器确认收到/处理了    （delivered_at，只在 sign_off 时写）
    这里只认前两个。`next_retry_at` 照排——那是留给 ack 的窗口，
    过期还没 ack 就自动重投，最多 max_attempts 次后明说"已放弃"
    （实测有限：5 次后 `next_retry_at=null` + last_error 说明，不是无限重启）。
    """
    if not outcomes and not skipped and not no_push and not spawned:
        return 0
    errors = errors or {}
    skipped = skipped or set()
    no_push = no_push or set()
    spawned = spawned or {}
    now = time.time()
    retry_base = float(os.environ.get("AGENT_ANP_RETRY_SEC") or 120)

    def transform(rec: dict[str, Any]) -> dict[str, Any] | None:
        mid = str(rec.get("id"))
        if mid in skipped:
            # 只留痕，不排重投、不算失败：这是"发送方选择不唤醒"，不是投递事故。
            return {"wake_skipped": True, "last_attempt_at": now, "next_retry_at": None,
                    "last_error": None}
        if mid in no_push:
            # ★P0-0★：收件人是 queue 档。消息**已经正常持久化**了，这就是终态——
            # 不递增 attempts（保持 0）、不排重投、不记错误、也不触发 message.undelivered。
            return {"wake_skipped": "no_push_target", "last_attempt_at": now,
                    "next_retry_at": None, "last_error": None}
        if mid in spawned:
            # ★甲（D1 修复）★：进程起来了 ≠ 信送到了。不写 delivered_at，
            # 只记 spawned_at / wake_pid，并给 ack 留一个退避窗口。
            n_here = int(rec.get("attempts") or 0) + 1
            patch: dict[str, Any] = {
                "attempts": n_here,
                "last_attempt_at": now,
                "spawned_at": now,
                "next_retry_at": now + retry_base * min(n_here, 6),
                "last_error": None,
            }
            pid = (spawned.get(mid) or {}).get("pid")
            if pid:
                patch["wake_pid"] = pid
            return patch
        if mid not in outcomes:
            return None
        attempts = int(rec.get("attempts") or 0) + 1
        if outcomes[mid]:
            patch = {"attempts": attempts, "last_attempt_at": now}
            if not rec.get("delivered_at"):
                patch["delivered_at"] = now
            patch["next_retry_at"] = None
            patch["last_error"] = None
            return patch
        patch = {
            "attempts": attempts,
            "last_attempt_at": now,
            "last_error": str(errors.get(mid) or "唤醒失败")[:300],
            "next_retry_at": now + retry_base * min(attempts, 6),
        }
        return patch

    return _inbox_transact(name, transform, home)[0]


def claim(name: str, ids: set[str], home: Path = DEFAULT_HOME, by: str = "") -> int:
    """把信标记为"已被消费"（读了）。claimed_at 与 read 同步维护。"""
    if not ids:
        return 0
    now = time.time()

    def transform(rec: dict[str, Any]) -> dict[str, Any] | None:
        if str(rec.get("id")) not in ids:
            return None
        patch: dict[str, Any] = {}
        if not rec.get("read"):
            patch.update({"read": True, "read_ts": now})
        if not rec.get("claimed_at"):
            patch["claimed_at"] = now
        if by and not rec.get("claimed_by"):
            patch["claimed_by"] = by
        return patch or None

    return _inbox_transact(name, transform, home)[0]


def mark_replied(owner: str, reply_to: str, reply_id: str = "", home: Path = DEFAULT_HOME) -> int:
    """回信落盘后，给原信打上 replied_at（同一封信只记第一次）。"""
    if not reply_to:
        return 0
    now = time.time()

    def transform(rec: dict[str, Any]) -> dict[str, Any] | None:
        if str(rec.get("id")) != str(reply_to):
            return None
        if rec.get("replied_at"):
            return None
        return {"replied_at": now, "replied_by": reply_id or None}

    return _inbox_transact(owner, transform, home)[0]


def pending_messages(home: Path = DEFAULT_HOME, name: str = "", due_only: bool = False,
                     undelivered_only: bool = False, order: str = "oldest",
                     limit: int | None = None) -> list[dict[str, Any]]:
    """列出"还没被消费"的信（未读）。

    两个筛选口径：
      due_only          → "排了重投且已到期"的（`next_retry_at` 已过）；
                          ★`next_retry_at` 为空＝从未安排过投递（例如发送方在唤醒前就死了）
                          → 也算到期，否则这类信永远不会被自动重投捡起来★。
                          "正在投递中"（租约未过期，见 `claim_lease`）不算到期，避免重复投递。
      undelivered_only  → 只返回"从未投递成功"的（delivered_at 为空）。
    """
    inboxes = [name] if name else known_inboxes(home)
    now = time.time()
    out: list[dict[str, Any]] = []
    for ident in inboxes:
        for rec in read_records(ident, home):
            if rec.get("read"):
                continue
            if due_only and rec.get("wake_skipped"):
                # 发信方明确说过"不唤醒"：这是**选择**，不是待重投的失败项，永远别捡。
                continue
            next_retry = rec.get("next_retry_at")
            if due_only and next_retry and float(next_retry) > now:
                continue          # 已排期未到期（含投递租约）：不捡
            if undelivered_only and rec.get("delivered_at"):
                continue
            out.append({"inbox": ident, **rec})
    out.sort(key=lambda r: float(r.get("ts") or 0))
    if str(order or "oldest").lower() in ("newest", "latest", "desc"):
        out.reverse()
    if limit is not None and int(limit) > 0:
        out = out[: int(limit)]
    return out


# --------------------------------------------------------------------------- #
# 身份密钥（握手令牌，协议 §3.1）
# --------------------------------------------------------------------------- #
class KeysUnreadable(Exception):
    """密钥表存在但读不动/解析失败 —— 此时**不能**当成"没有密钥"，否则鉴权整体 fail-open。"""


def keys_path(home: Path = DEFAULT_HOME) -> Path:
    return Path(home) / "keys.json"


def read_keys(home: Path = DEFAULT_HOME) -> dict[str, str]:
    """读身份密钥表：{身份名: 令牌}。

    ★坏文件不能当成空表★（2026-09-20 审阅发现）：文件存在但解析失败时若返回 `{}`，
    `verify_key` 会对所有身份走 "issued-pending"，鉴权整体失效（fail-open）。
    所以这里：解析失败 → 抛 `KeysUnreadable`，由调用方决定怎么办（默认拒绝）。
    文件不存在 → 返回空表（合法状态：全新 home）。
    """
    p = keys_path(home)
    if not p.exists():
        return {}
    try:
        data = json.loads(read_text_shared(p))
    except (json.JSONDecodeError, OSError) as e:
        raise KeysUnreadable(f"{p} 读取失败（{type(e).__name__}: {e}）") from e
    if not isinstance(data, dict):
        raise KeysUnreadable(f"{p} 内容不是 JSON 对象")
    return {str(k): str(v) for k, v in data.items() if str(v)}


def _tighten(path: Path) -> None:
    """把文件权限收紧到"仅本人可读写"（POSIX 0o600；Windows 用 icacls 去继承）。

    ★Windows 坑（2026-09-24）★：只 `grant {USERNAME}:F` 时，USERNAME 往往不是
    icacls 认的账号名（要 `DOMAIN\\user` 或 SID）。`/inheritance:r` 先剥继承、
    grant 又静默失败 → 文件对**包括 hub 自己在内的所有人**不可读写，
    下次 `write_text`/`read_text` 全是 PermissionError（keys.json / plugins.json /
    admin.token / hub.secret 都踩过）。这里用 `whoami` 的权威账号名，并在
    剥继承后**显式**授予 Full，失败则回滚继承，绝不锁死自己。
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name != "nt":
        return
    try:
        import subprocess

        hidden = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        try:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            hidden["startupinfo"] = startup
        except AttributeError:
            pass

        users = []
        try:
            who = subprocess.check_output(["whoami"], capture_output=True, timeout=5,
                                          **hidden)
            who = who.decode("utf-8", "replace").strip()
            if who:
                users.append(who)
        except Exception:  # noqa: BLE001
            pass
        for key in ("USERNAME", "USERDOMAIN"):
            pass
        user = os.environ.get("USERNAME") or ""
        dom = os.environ.get("USERDOMAIN") or ""
        if user and dom:
            users.append(f"{dom}\\{user}")
        if user:
            users.append(user)
        seen = []
        for u in users:
            if u and u not in seen:
                seen.append(u)
        if not seen:
            return
        grants = []
        for u in seen:
            grants.extend(["/grant:r", f"{u}:F"])
        r = subprocess.run(  # noqa: S603
            ["icacls", str(path), "/inheritance:r", *grants],
            capture_output=True, timeout=10,
            **hidden,
        )
        if r.returncode != 0:
            # 剥继承后 grant 失败 = 可能已锁死；恢复继承，宁可不收紧
            subprocess.run(["icacls", str(path), "/inheritance:e"],
                           capture_output=True, timeout=10,
                           **hidden)
    except Exception:  # noqa: BLE001
        pass


def _write_secret_text(path: Path, text: str) -> None:
    """写入私密文本；Windows 上旧 ACL 会挡 rewrite，此时删掉重建再收紧。"""
    try:
        path.write_text(text, encoding="utf-8")
    except PermissionError:
        try:
            path.unlink()
        except OSError:
            pass
        path.write_text(text, encoding="utf-8")
    _tighten(path)


def write_keys(keys: dict[str, str], home: Path = DEFAULT_HOME) -> None:
    """写密钥表，并把文件权限收紧到"仅本人可读写"（POSIX 0o600；Windows 尽量）。"""
    # ★永久架构约束★：hub.secret 不得以任何身份 token 的形式落进 keys.json
    try:
        secret = hub_secret(home)
        for name, token in keys.items():
            if token and secret and str(token) == secret:
                raise ValueError(f"拒绝写密钥表：身份 {name} 的 token 与 hub.secret 相同"
                                 "（违反『Agent token ≠ Hub signing secret』）")
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 - 取不到 secret 时不阻塞正常的密钥写入
        pass
    p = keys_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_secret_text(p, json.dumps(keys, ensure_ascii=False, indent=2) + "\n")


def admin_token_path(home: Path = DEFAULT_HOME) -> Path:
    return Path(home) / "admin.token"


def admin_token(home: Path = DEFAULT_HOME) -> str:
    """管理员令牌：看守板 `/api/state` 与 hub `/sessions`（唯一能看**全部正文**的两个入口）。

    优先级：`AGENT_ANP_ADMIN_TOKEN` 环境变量 > 本地 `admin.token` 文件（首次用时生成）。
    文件权限与 `keys.json` 同等收紧——它就是"能看所有 agent 通信内容"的那把钥匙。
    """
    forced = str(os.environ.get("AGENT_ANP_ADMIN_TOKEN") or "").strip()
    if forced:
        return forced
    p = admin_token_path(home)
    try:
        existing = read_text_shared(p).strip()
    except Exception:  # noqa: BLE001
        existing = ""
    if existing:
        return existing
    import secrets

    token = secrets.token_hex(24)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token + "\n", encoding="utf-8")
    _tighten(p)
    return token


def admin_token_ok(given: str, home: Path = DEFAULT_HOME) -> bool:
    """比对管理员令牌（恒定时间比较，避免时序侧信道）。"""
    import secrets as _secrets

    got = str(given or "").strip()
    want = admin_token(home)
    return bool(got) and _secrets.compare_digest(got, want)


# --------------------------------------------------------------------------- #
# 信封完整性签名（HMAC-SHA256）—— ★2026-09-22★
# --------------------------------------------------------------------------- #
# 它解决什么：inbox 里的 JSON **同 OS 账户下任何进程都能改**，而改完之后从外表
# 看与真信完全一样。有了签名，管理员终于能区分「信箱当时写下的那封」与「现在盘上这封」。
#
# ★它不解决什么★（说清楚，别当过安全边界用）：密钥是信箱自己的一个本地秘密
# （`hub.secret`，ACL 与 keys.json 同等收紧），而所有 agent 跑在同一个 OS 账户下，
# 读得到它的进程照样能签。所以这一步给的是**可审计性与防意外改写/磁盘损坏**，
# 不是防恶意冒充。真正的身份隔离仍靠 OS 账户/沙箱（协议 §3.1，未做）。
#
# ★为什么不用「发件人令牌」当密钥★：所有令牌同在 keys.json 里，谁都能读，用它签名等于没签；
# 用信箱自己的秘密至少保证「只有信箱写过」。
#
# ★签名边界★（这是本设计最关键的一条）：
#   签   —— 信封**内容**：id / ts / from / to / subject / body / kind / reply_to /
#           correlation_id / meta
#   不签 —— 全部**状态机字段**：read / read_ts / read_by / attempts / delivered_at /
#           last_attempt_at / last_error / next_retry_at / claimed_* / replied_* /
#           wake_skipped / sig 自己
# 原因：信箱每次签收、投递、重投都会改状态字段。把它们签进去，第一次 mark_read 就会让签名失效。
# 一句话：**信封签名，状态不签。**
SIGNED_FIELDS = ("id", "ts", "from", "to", "subject", "body", "kind",
                 "reply_to", "correlation_id", "meta")


def hub_secret_path(home: Path = DEFAULT_HOME) -> Path:
    return Path(home) / "hub.secret"


def hub_secret(home: Path = DEFAULT_HOME) -> str:
    """信箱自己的签名密钥：首次用时生成并收紧权限；之后每次读同一枚。"""
    forced = str(os.environ.get("AGENT_ANP_HUB_SECRET") or "").strip()
    if forced:
        return forced
    p = hub_secret_path(home)
    try:
        existing = read_text_shared(p).strip()
    except Exception:  # noqa: BLE001
        existing = ""
    if existing:
        return existing
    import secrets

    token = secrets.token_hex(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token + "\n", encoding="utf-8")
    _tighten(p)
    return token


# --------------------------------------------------------------------------- #
# ★永久架构约束（主人 2026-09-22 定，写进代码而不只是文档）★
# --------------------------------------------------------------------------- #
#     Agent token  ≠ Hub signing secret
#     插件身份认证 ≠ Envelope 签名密钥
#
# 钥匙串在同一个仓房里可以，但**两串绝不允许互相顶替**：
#   · `keys.json` 里的 agent token：证明"这个进程拿得到某个身份的钥匙"，按现有模型管理，
#     插件也会拿到自己那一枚（在 `/anp/register` 的响应里）；
#   · `hub.secret`：证明"这封信是信箱写的"，只允许出现在 `core.sign_envelope()` 内部。
#
# 一旦插件 / 普通 agent / 任何进程因为"能调用 Core API"就顺手拿到 hub.secret，
# "只有 Hub 能合法生成签名"这条立刻失效，第 3 步的完整性闭环变成装饰。
# 所以第 4 步落地时必须守住两条：
#     1. `/anp/register` 只发**插件自己那一枚 agent token**，绝不在任何响应体里带 hub.secret；
#     2. Core 不提供"读取 hub.secret"的接口（连管理员也不要——管理员要看的是验签结论，
#        不是密钥本身；`/health.integrity` 给的正是结论）。
# 下面这个函数是这条约束的可执行形态。
SECRET_ENV_NAMES = ("AGENT_ANP_HUB_SECRET",)


def secret_isolation_ok(home: Path = DEFAULT_HOME) -> tuple[bool, str]:
    """两串钥匙有没有互相顶替。返回 `(是否隔离, 原因)`。

    检查：hub.secret 不得等于任何 agent token、不得等于 admin.token。
    （反方向由 `issue_key()` 的发证漏斗保证：新发的 token 当场与 hub.secret 比一次。）
    """
    import hmac as _hmac

    try:
        secret = hub_secret(home)
    except Exception as e:  # noqa: BLE001
        return False, f"hub.secret 读不到：{type(e).__name__}: {e}"
    if not secret:
        return False, "hub.secret 为空"
    try:
        tokens = read_keys(home)
    except KeysUnreadable as e:
        return False, f"keys.json 读不动：{e}"
    for name, token in tokens.items():
        if token and _hmac.compare_digest(str(token), secret):
            return False, f"★架构约束被破坏★：身份 {name} 的 token 与 hub.secret 相同"
    admin = str(os.environ.get("AGENT_ANP_ADMIN_TOKEN") or "").strip()
    if admin and _hmac.compare_digest(admin, secret):
        return False, "★架构约束被破坏★：admin.token 与 hub.secret 相同"
    return True, "isolated"


def _assert_token_not_secret(token: str, home: Path = DEFAULT_HOME) -> None:
    """发证漏斗：新 token 不得正好是 hub.secret / admin.token（概率为零，但约束要可执行）。"""
    import hmac as _hmac

    got = str(token or "")
    if not got:
        return
    for bad in (hub_secret(home), str(os.environ.get("AGENT_ANP_ADMIN_TOKEN") or "").strip()):
        if bad and _hmac.compare_digest(got, bad):
            raise ValueError("拒绝发证：新 token 与 hub.secret / admin.token 相同"
                             "（违反『Agent token ≠ Hub signing secret』）")


def _canonical(rec: dict[str, Any]) -> str:
    """签名用的规范化串：只取内容字段、排序、无空格——保证任何时候重算结果一致。"""
    subset = {k: rec.get(k) for k in SIGNED_FIELDS}
    return json.dumps(subset, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def sign_envelope(rec: dict[str, Any], home: Path = DEFAULT_HOME) -> str:
    """给一封信算签名（不修改 rec，只返回签名字符串）。"""
    import hashlib
    import hmac as _hmac

    return _hmac.new(hub_secret(home).encode("utf-8"),
                     _canonical(rec).encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def verify_envelope(rec: dict[str, Any], home: Path = DEFAULT_HOME) -> tuple[bool, str]:
    """校验一封信的签名。返回 `(是否通过, 说明)`。

    三种结果：`unsigned`（老信，没签过——不是错误）/ `ok` / `tampered`（内容被改过）。
    """
    given = str(rec.get("sig") or "")
    if not given:
        return True, "unsigned"        # 升级前的信没有签名，不当它坏
    want = sign_envelope(rec, home)
    import hmac as _hmac

    if _hmac.compare_digest(given, want):
        return True, "ok"
    return False, "tampered"


def integrity_report(home: Path = DEFAULT_HOME, cap: int = 5000) -> dict[str, Any]:
    """全信箱签名体检：多少封验过、多少封被改过、哪些线被改过。给 `/health` 用。"""
    checked = unsigned = tampered = 0
    bad: list[dict[str, Any]] = []
    for name in known_inboxes(home):
        for rec in read_records(name, home):
            if checked >= cap:
                break
            checked += 1
            ok, why = verify_envelope(rec, home)
            if why == "unsigned":
                unsigned += 1
            elif not ok:
                tampered += 1
                bad.append({"inbox": name, "id": rec.get("id"), "from": rec.get("from"),
                            "to": rec.get("to"), "ts": rec.get("ts"), "why": why})
    return {"checked": checked, "signed": checked - unsigned, "unsigned": unsigned,
            "tampered": tampered, "tampered_list": bad[:20], "cap": cap}


def issue_key(name: str, home: Path = DEFAULT_HOME) -> str:
    """给一个身份发密钥；已有则原样返回（幂等）。"""
    name = str(name).strip()
    if not name:
        raise ValueError("身份名不能为空")
    keys = read_keys(home)
    if keys.get(name):
        return keys[name]
    import secrets

    token = secrets.token_hex(24)
    _assert_token_not_secret(token, home)     # ★永久架构约束★：Agent token ≠ Hub signing secret
    keys[name] = token
    write_keys(keys, home)
    return keys[name]


def key_for(name: str, home: Path = DEFAULT_HOME) -> str:
    """取某身份的密钥（没有就现发一个）。"""
    return issue_key(name, home)


def wake_sign_off_ids(outcome: dict[str, Any], rec: dict[str, Any]) -> list[str]:
    """这次唤醒是否等于"信已经落地"？是则返回可以签收的 id 列表。

    ★放在 core 里，两条投递路径（hub / direct）共用★（2026-09-21 定）：
    这段判断原先只写在 hub 里，direct 降级路径没有——于是走 direct 投出去的信**永远不签收**，
    实测后果：chencode 那封信一直挂着未读，被反复重新推送（同一封收到两次）。
    教训与"双投递/静默信被重投叫醒"同源：**同一语义只准有一份实现**。

    http 档（收信端是常驻服务）：返回 200 且带 `mode`（batched/immediate/session/inject/followup）
    ＝信已进会话队列，可以当场签收；401/400 这类失败（`ok=false`、没有 `mode`）不签收。
    exec 档这里拿不到子进程结果（只有 pid），一律不签收，交给唤醒器回报。
    """
    if not outcome.get("ok"):
        return []
    kind = str(outcome.get("kind") or "")
    if kind == "exec":
        return []
    if not str(outcome.get("mode") or ""):
        return []
    mid = str(rec.get("id") or "")
    return [mid] if mid else []


def require_key(home: Path = DEFAULT_HOME) -> bool:
    """本机是否强制要求身份令牌。**默认强制**（2026-09-20 实测后改的）。

    原先默认"没带密钥就放行并标注"，理由是别掐断还没升级的老客户端；
    但那样等于**密钥形同虚设**：不带 `x-mailbox-key` 就能读任何身份的收件箱
    （自称 server=<别人> 即可，`auth` 只会记成 "missing"）。既然所有端都已带上密钥，
    就没有继续放行的理由。要临时退回宽松模式：设 AGENT_ANP_REQUIRE_KEY=0。
    """
    return str(os.environ.get("AGENT_ANP_REQUIRE_KEY") or "1").strip().lower() not in ("0", "false", "no")


def verify_key(name: str, token: str, home: Path = DEFAULT_HOME) -> tuple[bool, str]:
    """校验令牌。返回 (是否通过, 说明)。

    状态与取舍：
      - "ok"             令牌正确
      - "rejected"       带了令牌但不对 → 一律拒绝（这是唯一能立刻挡住的攻击面）
      - "missing"        该身份已有密钥，但请求没带 → 默认**拒绝**；设 AGENT_ANP_REQUIRE_KEY=0 才放行并标注
      - "issued-pending" 该身份从未发过密钥 → **也拒绝并标注**（见下），只有 AGENT_ANP_REQUIRE_KEY=0 才放行

    为什么"没带密钥"必须拒绝：身份名是**请求方自己填的**（`/tool` 的 server 字段、`/send` 的 from 字段），
    所以"没带密钥"不是"诚实但没证明"，而是"可以随便冒充别人"。实测（2026-09-20）：
    不带任何密钥、只说 server=chencode / server=mimo，就能读到两家的收件箱正文。
    真正的隔离仍靠本地文件权限（keys.json 仅供本人），密钥是第二道门。

    ★为什么 issued-pending 也拒绝（2026-09-20 审阅后改）★：
    原先它无条件放行，等于"只要报一个**从没注册过的名字**就免检"——连带**错的令牌**也放行
    （`verify_key("newbie", "deadbeef")` 曾返回 True），而且可以抢先用别人的名字把身份注册掉。
    正确做法：**密钥由服务端在响应里发**（`POST /heartbeat` 的响应带 `key`），
    新身份的引导因此不需要"免检放行"这条后门。
    """
    want = read_keys(home).get(str(name).strip())
    got = str(token or "").strip()
    if not want:
        return (True, "issued-pending") if not require_key(home) else (False, "issued-pending")
    if not got:
        return (True, "missing") if not require_key(home) else (False, "missing")
    if got == want:
        return True, "ok"
    return False, "rejected"


def identity_known(name: str, home: Path = DEFAULT_HOME) -> bool:
    """这个身份在这台信箱里**登记过**吗？判据只有两条，都必须来自"该身份自己做过的事"：

    1. 密钥表里已经有它的令牌（它领取过）；
    2. 账本里有它的记录（它心跳过）。

    ★故意**不**看收件箱是否存在★：任何人都能靠"给某个名字发一封信"凭空造出一个收件箱，
    若把它当成"登记过"，就等于让陌生人用别人的名字占位（2026-09-20 审阅时踩到：
    自检脚本先给 `peer` 落了一封信，随后 `peer` 的心跳就被判成"已存在但没带密钥" → 401）。
    """
    who = str(name or "").strip()
    if not who:
        return False
    try:
        if who in read_keys(home):
            return True
    except KeysUnreadable:
        return False
    return who in agent_table(home)


def identity_casefold_conflict(name: str, home: Path = DEFAULT_HOME) -> str | None:
    """返回与 *name* 大小写不敏感相撞的既有身份名。

    Windows 文件系统不区分文件名大小写，而 `_safe()` 为兼容旧账本会保留
    大小写；因此 `Ab` 与 `ab` 不能同时登记。已有身份文件不重命名，只在新
    注册入口拒绝会发生碰撞的名字。
    """
    who = str(name or "").strip()
    if not who:
        return None
    folded = who.casefold()
    names: set[str] = set()
    try:
        names.update(str(k).strip() for k in read_keys(home))
    except KeysUnreadable:
        pass
    names.update(agent_table(home))
    for existing in sorted(n for n in names if n):
        if existing != who and existing.casefold() == folded:
            return existing
    return None


def inbox_health(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    """信箱体检：各箱未读/待重投/已投递未消费的计数，给 hub 的 /health 用。

    ★缓存信（`wake_skipped`）不算"未投递"★（2026-09-21 修）：
    它是发信方**故意不唤醒**的（"只发送，等对方自己读"），从来就没有"投递"这回事。
    原先把它计进 `undelivered`，于是"只是存了几封缓存信"就会让体检飘红、看起来像投递故障——
    实测：我发两封缓存信后 `undelivered=3`。现在它单独计进 `cached`，不进 `undelivered`/`retry_due`。
    """
    now = time.time()
    per_inbox: dict[str, dict[str, int]] = {}
    totals = {"messages": 0, "unread": 0, "undelivered": 0, "retry_due": 0,
              "claimed": 0, "replied": 0, "cached": 0}
    for ident in known_inboxes(home):
        rows = read_records(ident, home)
        stats = {"messages": len(rows), "unread": 0, "undelivered": 0, "retry_due": 0,
                 "claimed": 0, "replied": 0, "cached": 0}
        for rec in rows:
            # `cached` 是"这封是安静送达的"这一**事实**，与读没读无关（缓存信现在也会被签收）
            if rec.get("wake_skipped"):
                stats["cached"] += 1
            if rec.get("read"):
                stats["claimed"] += 1
            else:
                stats["unread"] += 1
                if not rec.get("wake_skipped") and not rec.get("delivered_at"):
                    stats["undelivered"] += 1
                nr = rec.get("next_retry_at")
                if (not rec.get("wake_skipped") and not rec.get("delivered_at")
                        and (not nr or float(nr) <= now)):
                    stats["retry_due"] += 1
            if rec.get("replied_at"):
                stats["replied"] += 1
        per_inbox[ident] = stats
        for key in stats:
            totals[key] += stats[key]
    return {"totals": totals, "inboxes": per_inbox}


def prune(home: Path = DEFAULT_HOME, keep: int = 500) -> int:
    """每个收件箱只保留最近 keep 封，返回删除条数。"""
    removed = 0
    d = Path(home) / "inbox"
    if not d.exists():
        return 0
    for p in d.glob("*.jsonl"):
        with _mutation_lock(p):
            # ★文件名是编码后的，先还原再读★：直接拿 stem 当身份名会被二次编码
            # （`a%2Fb` → `a%252Fb`），读到的是另一个文件（2026-09-26 同批修）。
            rows = read_records(_unsafe(p.stem), home)
            if len(rows) <= keep:
                continue
            tail = rows[-keep:]
            # Never delete unread records, even when they are older than the
            # retention window. Keep the newest `keep` records plus old unread.
            keep_rows = [r for r in rows[:-keep] if not r.get("read")] + tail
            removed += len(rows) - len(keep_rows)
            # 同 id 只留一份，但**未读的那一份永远优先**（2026-09-22 修）：WORKLOG 记的
            # "dedupe 抹掉未读信"就是倒序去重留下的后一条可能是已读的。删未读等于丢信。
            by_id: dict[str, dict[str, Any]] = {}
            for r in keep_rows:                     # 正序：后出现的天然覆盖先出现的
                rid = str(r.get("id"))
                prev = by_id.get(rid)
                if prev is not None:
                    prev_unread = not prev.get("read")
                    new_unread = not r.get("read")
                    if prev_unread and not new_unread:
                        continue                    # 别用已读的覆盖未读的
                    if prev_unread == new_unread:
                        continue                    # 两份同状态，留先遇到的那份
                by_id[rid] = r
            out = sorted(by_id.values(), key=lambda r: float(r.get("ts") or 0))
            _atomic_write_text(p, _jsonl_text(out))
    return removed


# --------------------------------------------------------------------------- #
# 数据生命周期（★2026-09-22 接线★）
# --------------------------------------------------------------------------- #
# 背景：`prune()` 从写下那天起就**全仓没有调用点**，账本也没有压缩逻辑，于是
#   · 收件箱无限增长（`mail_inbox` 每次都要把整箱读进来再切 limit）；
#   · 账本无限增长——实测 `ledger.jsonl` 4.0 MB / 17045 行，其中 16952 行是 dsh 一家
#     的心跳（桥接每 30 秒一条），而 `agent_table()` 只看每身份最后一条。
# "保留未读"是硬约束：删未读等于丢信。所以这里两件事都只动"已读且陈旧"的部分。

LEDGER_KEEP = int(os.environ.get("AGENT_ANP_LEDGER_KEEP") or 500)


def compact_ledger(home: Path = DEFAULT_HOME, keep: int | None = None) -> dict[str, Any]:
    """压缩账本：每身份只留最后一条，外加最近 keep 行心跳（供"谁什么时候在过"回看）。

    安全性：`keys.json` 才是密钥的权威存放处，账本只是**审计流水**，压缩不丢任何可用状态；
    `identity_known()` 也仍然成立（每身份最后一条一定在）。压缩前先把原文件留一份
    `<ledger>.compact-bak`（覆盖式，只有一个），要翻旧账有处可查。
    """
    p = ledger_path(home)
    # ★读必须和替换在同一把锁里★（2026-09-26 审计复现实测）：旧实现在锁外读账本、
    # 直到写之前才加锁，"读快照 → 并发 heartbeat 追加 → 用旧快照整文件替换"会把这
    # 期间的新心跳**整条抹掉**（账本语义是取每身份最后一条，被抹掉的就是"还在线"这个事实）。
    # 与 `prune()` 用同一套写法：持锁读-改-写，heartbeat 的 `append_line` 在此期间等锁。
    with _mutation_lock(p):
        rows = read_ledger(home)
        if not rows:
            return {"ok": True, "before": 0, "after": 0, "kept_unread_names": []}
        limit = LEDGER_KEEP if keep is None else int(keep)
        last_by_name: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = str(row.get("name") or "").strip()
            if name:
                last_by_name[name] = row
        tail = rows[-max(1, limit):]
        seen: set[int] = set()
        out: list[dict[str, Any]] = []
        for row in [*last_by_name.values(), *tail]:
            key = id(row)
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        out.sort(key=lambda r: float(r.get("ts") or 0))
        # 备份一份再替换（覆盖式：只留最近一次压缩前的原件，翻旧账有处可查）
        try:
            if p.is_file():
                Path(str(p) + ".compact-bak").write_bytes(p.read_bytes())
        except OSError:
            pass
        _atomic_write_text(p, _jsonl_text(out))
    return {"ok": True, "before": len(rows), "after": len(out),
            "backup": str(Path(str(p) + ".compact-bak")),
            "names": sorted(last_by_name)}


def maintenance(home: Path = DEFAULT_HOME, keep_inbox: int = 500,
                keep_ledger: int | None = None) -> dict[str, Any]:
    """跑一次数据维护：收件箱裁剪 + 账本压缩。由 hub 的后台线程定期调用。"""
    out: dict[str, Any] = {"at": time.time()}
    try:
        out["inbox_removed"] = prune(home, keep_inbox)
    except Exception as e:  # noqa: BLE001 - 维护失败不能影响服务，只记账
        out["inbox_error"] = f"{type(e).__name__}: {e}"
    try:
        out["ledger"] = compact_ledger(home, keep_ledger)
    except Exception as e:  # noqa: BLE001
        out["ledger_error"] = f"{type(e).__name__}: {e}"
    return out


# --------------------------------------------------------------------------- #
# 启动上下文（agent 侧看不到"我是被谁、用哪条凭证拉起来的"，由中继写、MCP 读）
# --------------------------------------------------------------------------- #
def launch_context_path(home: Path = DEFAULT_HOME) -> Path:
    return Path(home) / "launch-context.json"


def write_launch_context(identity: str, info: dict[str, Any], home: Path = DEFAULT_HOME) -> None:
    """中继派活前写一份"本次是怎么被拉起来的"：通道 / 凭证 / 模型 / 时间。

    agent 侧（headless 会话）看不到自己的启动方式，回信时就没法说明来源；
    这份上下文让 MCP 发信时能自动带上它（协议 §3.3）。
    """
    rec = safe({"identity": identity, "ts": time.time(), **info})
    p = launch_context_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_launch_context(identity: str = "", home: Path = DEFAULT_HOME,
                        max_age: float = 1800.0) -> dict[str, Any]:
    """读最近的启动上下文；超过 max_age 秒就当作过期（避免把上一轮的凭证当成本轮）。"""
    p = launch_context_path(home)
    if not p.exists():
        return {}
    try:
        rec = json.loads(read_text_shared(p))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(rec, dict):
        return {}
    if identity and str(rec.get("identity") or "") != str(identity):
        return {}
    if time.time() - float(rec.get("ts") or 0) > max_age:
        return {}
    return rec


# --------------------------------------------------------------------------- #
# 唤醒配置的信任边界（★安全：挡住"追加一行账本 → 任意命令执行"★）
# --------------------------------------------------------------------------- #
# 唤醒端点存在 `ledger.jsonl`，而那份文件是**追加式、无完整性保护**的：本机任何进程
# （所有 agent 跑在同一个 OS 账户下）都能往里加一行
#     {"name":"x","wake":{"kind":"exec","command":["powershell","-enc","..."]}}
# `agent_table()` 取每个身份的最后一条，于是下一封发给 x 的信就会**以当前用户身份执行那条命令**。
# （2026-09-22 实测复现：追加一行假 wake 后 `core.wake()` 正常返回 pid，进程真的起来了。）
#
# 修法不是"把账本锁起来"——同账户下锁不住——而是在**唯一执行点**设信任边界：
#   · exec：命令行里必须引用一个落在**信箱安装目录**内的脚本（见 `install_roots()`）；
#     `AGENT_ANP_TRUST_WAKE_PATHS` 可追加白名单根（`;` 分隔）。目录外的一律拒绝。
#   · http：URL 主机必须是回环。否则一封来信的**正文**会被 POST 到任意主机（外泄面），
#     而且一个伪造的 200 + `mode` 响应能骗过"信已进会话队列"的签收判断。
#
# ★这不是彻底解决★：能往安装目录里写文件的攻击者仍在信任圈内，`/heartbeat` 的令牌也仍在
# 同一个 keys.json 里。真正的隔离要靠"一身份一 OS 账户"（协议 §3.1，仍未实现）。
# 这一步只做一件事：把"任意进程追加一行账本 = 拿到当前用户的命令执行"降级为
# "必须先能写进信箱安装目录"。
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _is_loopback_host(host: str) -> bool:
    text = str(host or "").strip().lower().strip("[]")
    if not text:
        return False
    if text in LOOPBACK_HOSTS:
        return True
    return text.startswith("127.")      # 127.0.0.0/8 整段都是回环


def install_roots(home: Path | str | None = None) -> list[Path]:
    """唤醒脚本允许落在哪些根目录之内。

    默认三个来源：
      1. 信箱自己的安装目录（src / repo root）—— 历来如此；
      2. **`<home>/plugins/`**（#4.1a 加）—— Adapter Plugin 的合法安装位置。

    ★为什么加第 2 个★：Adapter Plugin 按设计装在 `<home>/plugins/<id>/`，
    而 exec 唤醒的信任校验只认安装目录，于是真实部署下插件的 exec 唤醒一律被拒
    （Mimo 实测撞到，靠改 http wake 绕开；Codex/Chen 的 spawn.py 更依赖 exec，绕不开）。
    `<home>/plugins/` 是**信箱自己创建并管理**的目录——它扫这里、从这里拉进程，
    本来就在信任圈内。加它只是把这个既成事实表达出来，不是放宽边界。

    ★没有放宽的部分★：任意 `<home>/` 下的脚本（例如 `<home>/evil.py`）**仍然被拒**。
    受信任的只有 plugins/ 这一棵，不是整个 home。
    """
    roots: list[Path] = []
    try:
        import anp_preflight as _pf  # PLC0415 - 惰性 import，避免 core 被提前加载时成环

        roots.extend([_pf.src_dir(), _pf.repo_root()])
    except Exception:  # noqa: BLE001 - 取不到就退回"本文件所在目录"，别让校验本身崩掉
        roots.append(Path(__file__).resolve().parent)
    # 合法插件安装位置：<home>/plugins（#4.1a）
    try:
        roots.append(Path(home if home is not None else DEFAULT_HOME) / "plugins")
    except (TypeError, ValueError):  # home 给了个奇怪的值，别让校验本身崩掉
        pass
    extra = str(os.environ.get("AGENT_ANP_TRUST_WAKE_PATHS") or "")
    for item in extra.replace(",", ";").split(";"):
        item = item.strip()
        if item:
            roots.append(Path(item).expanduser())
    seen: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _looks_like_path(arg: str) -> bool:
    """这个 argv 元素是不是"一个脚本路径"（而不是 `--agent` / `wake` 这类纯参数）。"""
    text = str(arg or "")
    if not text or text.startswith("-"):
        return False
    return ("\\" in text) or ("/" in text) or bool(Path(text).suffix)


# "让解释器执行**内联代码 / 模块**"的开关。出现它，argv 里就不存在"被执行的脚本"
# 这个可校验对象——`python -c "…"` 的正文是任意代码，尾随一个可信路径也改变不了
# "真正在执行什么"（2026-09-26 审计实测的绕过路径）。白名单式闭集，宁少放行不多放行。
INLINE_EXEC_FLAGS = frozenset({"c", "e", "m", "eval", "exec"})


def _is_inline_exec_flag(arg: str) -> bool:
    """这个 argv 元素是不是"执行内联代码/模块"的开关。

    除了完整 flag，还覆盖 `--eval=...`、PowerShell 的
    `-Command/-EncodedCommand`，以及 shell 常见的 `-lc`/`-cl` 组合写法。
    """
    raw = str(arg or "").strip()
    if not raw or not raw.startswith(("-", "/")):
        return False
    text = raw.lstrip("-/").lower()
    if text in INLINE_EXEC_FLAGS or text in {
        "command", "encodedcommand", "enc", "lc", "cl", "ec", "ce",
    }:
        return True
    head, sep, _value = text.partition("=")
    if sep and head in INLINE_EXEC_FLAGS | {"command", "encodedcommand", "enc"}:
        return True
    # Python/Node/shell accept compact short forms such as -cprint(1), -lc...
    if raw.startswith("--"):
        return False
    return text.startswith(("c", "lc", "cl")) and len(text) > 1


def _trusted_interpreter_paths() -> set[Path]:
    """返回 PATH/当前运行时解析出的解释器路径。

    绝对路径的 `python.exe` 不能只凭 basename 获信任；只有它确实指向当前
    运行时或 PATH 上同名的解释器时才允许，避免把任意同名可执行文件放过。
    """
    out: set[Path] = set()
    candidates = [sys.executable]
    for name in INTERPRETERS:
        try:
            found = shutil.which(name)
        except (OSError, ValueError):
            found = None
        if found:
            candidates.append(found)
    for value in candidates:
        try:
            out.add(Path(value).resolve())
        except OSError:
            continue
    return out


# argv[0] 允许是"解释器"：它们装在任意位置（venv / bun / node），所以**认名字不认路径**。
# 除了解释器，argv[0] 还可以是**安装目录内的可执行文件**——那本来就在信任圈内。
# 第三种情况（任意路径的可执行文件）一律拒绝：argv[0] 才是进程真正执行的东西，
# 旧实现"argv[0] 是解释器，不看它"漏掉的正是这一种——
# `["/tmp/evil.exe", "<任意可信路径>"]` 旧规则照样放行（2026-09-26 同批审计发现）。
INTERPRETERS = frozenset({
    "python", "python3", "pythonw", "py", "python.exe", "pythonw.exe",
    "node", "bun", "deno",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "bash", "sh",
})

# "按路径再加载一份代码"的开关：它同样让命令行里**多出一个可执行对象**，
# 而那个路径不在脚本位置上、容易被漏检（`node --require x.js script.js`）。
CODE_LOADING_FLAGS = frozenset({
    "r", "require", "import", "loader", "experimental-loader", "preload", "pre-load",
})

# 一眼看上去就是"源代码"的扩展名：出现在脚本位置之后，也按代码校验
# （`dump.json`、`--hub http://…` 这类**数据参数**不在此列，否则合法配置会被误伤）。
SCRIPT_SUFFIXES = frozenset({
    ".py", ".pyw", ".pyc", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".psm1",
    ".sh", ".bash", ".bat", ".cmd", ".vbs", ".rb", ".pl", ".php", ".lua", ".r",
})


def _is_code_loading_flag(arg: str) -> bool:
    raw = str(arg or "").strip()
    if not raw or not raw.startswith(("-", "/")):
        return False
    text = raw.lstrip("-/").lower()
    if text in CODE_LOADING_FLAGS:
        return True
    head, sep, _value = text.partition("=")
    return bool(sep and head in CODE_LOADING_FLAGS)


def _looks_like_script(arg: str) -> bool:
    return Path(str(arg or "")).suffix.lower() in SCRIPT_SUFFIXES


def _check_exec_command(argv: list[str], cwd: str,
                        roots: list[Path]) -> tuple[bool, str]:
    """exec 唤醒命令逐条过信任边界。规则见 `check_wake_config` 的说明。

    ★为什么要"认出脚本"而不是"逐个参数查"★（2026-09-26 审计复现）：
      · 旧规则"任意一个像路径的参数在信任根内就放行" → `python -c <任意代码>
        <任意可信路径>` 通过，真正执行的是 `-c` 的正文；
      · 第一版收紧成"每个像路径的参数都要在信任根内" → 把**合法配置**打死了
        （G1 的 dump 文件、demo adapter 的 `--hub http://127.0.0.1:PORT`
        都被当成脚本拒掉，实测 3 个套件翻红）。
    所以规则必须分三层：**开关**（拒）、**argv[0]**（解释器或安装目录内）、
    **被执行的脚本**（第一个像脚本的参数，必须在信任根内），
    其余参数是脚本自己的**数据**（输出路径、--hub 地址…），不参与校验。
    """
    roots_text = " | ".join(str(r) for r in roots)
    # --- ① 开关层：内联执行 / 按路径加载代码，都让"到底在执行什么"变得不可校验 ---
    for arg in argv[1:]:
        if _is_inline_exec_flag(arg):
            return False, (f"exec 唤醒出现内联执行开关 {arg!r}：命令行里没有'被执行的脚本'"
                           "可校验（-c 的正文是任意代码），一律拒绝")
        if _is_code_loading_flag(arg):
            return False, (f"exec 唤醒出现按路径加载代码的开关 {arg!r}：那条路径也要能校验，"
                           "一律拒绝（要加载请写进安装目录内的脚本再唤醒它）")

    # --- ② argv[0] 层：解释器（认名字）或安装目录内的可执行文件 ---
    exe = argv[0]
    exe_in_roots = _looks_like_path(exe) and _in_trust_roots(exe, cwd, roots)
    if not exe_in_roots:
        if _looks_like_path(exe):
            try:
                resolved_exe = Path(exe).resolve()
            except OSError:
                resolved_exe = Path(exe)
            if resolved_exe not in _trusted_interpreter_paths():
                return False, (f"exec 唤醒的argv[0] {exe!r} 既不在信箱安装目录内"
                               f"（{roots_text}），也不是当前环境解析出的可信解释器"
                               "——拒绝执行任意可执行文件")
        elif Path(exe).name.lower() not in INTERPRETERS:
            return False, (f"exec 唤醒的argv[0] {exe!r} 不是已知解释器"
                           f"（{sorted(INTERPRETERS)}）——它会被 PATH 解析成任意程序，拒绝")

    # --- ③ 脚本层：第一个像脚本的参数才是"被执行的脚本" ---
    # 先找带源码扩展名的；一个都没有时才退回"第一个像路径的参数"——
    # 两种写法都支持（`python plugin.py` 与 `python ./myscript`），
    # 但**先认源码扩展名**：输出文件、--hub 地址这类数据参数常排在脚本后面，
    # 用"第一个像路径的"会把它们误当成脚本（第一版收紧就这么误伤了 G1/demo）。
    # argv[0] 本身就在安装目录内时（安装的可执行文件 / .bat），它自己就是被执行的代码，
    # 不要求命令行里再出现一个脚本。
    tail = argv[1:]
    if exe_in_roots:
        script_idx = 0
    else:
        script_idx = next((i for i, a in enumerate(argv[1:], 1) if _looks_like_script(a)), None)
        if script_idx is None:
            script_idx = next((i for i, a in enumerate(argv[1:], 1) if _looks_like_path(a)), None)
        if script_idx is None:
            return False, (f"exec 唤醒的命令行里找不到被执行的脚本（{roots_text}）；"
                           "要放行请设 AGENT_ANP_TRUST_WAKE_PATHS，或显式给出脚本路径")
        if not _in_trust_roots(argv[script_idx], cwd, roots):
            return False, (f"exec 唤醒的脚本 {argv[script_idx]!r} 不在信箱安装目录内"
                           f"（{roots_text}）；要放行请设 AGENT_ANP_TRUST_WAKE_PATHS")
        tail = argv[script_idx + 1:]

    # --- ④ 其余位置再出现"像代码"的参数，也一律要过信任根（挡住 --require 之外的旁路）---
    for arg in tail:
        if _looks_like_script(arg) and not _in_trust_roots(arg, cwd, roots):
            return False, (f"exec 唤醒的参数 {arg!r} 看起来是另一份代码，但不在信箱安装目录内"
                           f"（{roots_text}）；数据文件请用别的扩展名，代码请放进安装目录")
    return True, ""


def _in_trust_roots(arg: str, cwd: str, roots: list[Path]) -> bool:
    """这个路径解析后落在某个信任根内吗？相对路径按**实际执行目录**解析。

    ★为什么不再用 hub 进程的 cwd★（2026-09-26 审计复现）：`wake()` 用 `wake.cwd`
    起子进程，校验却按 hub 自己的 cwd 猜——同一个相对脚本，一边放行一边在别处执行。
    """
    candidate = Path(str(arg))
    if not candidate.is_absolute():
        candidate = Path(str(cwd).strip() or os.getcwd()) / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def check_wake_config(entry: dict[str, Any],
                      home: Path | str | None = None) -> tuple[bool, str]:
    """这个唤醒配置能不能执行？返回 `(可否, 原因)`。

    规则见上一段安全说明。**队列档/未登记**返回 True（它们不执行任何东西），
    由 `wake()` 按原来的口径回"未配置唤醒端点"。

    ★exec 档的四层规则（2026-09-26 审计后收紧）★：
      1. 不许出现内联执行开关（`-c` / `-e` / `-m` / `--eval` / `/c`）或按路径再加载
         代码的开关（`-r` / `--require` / `--import` / `--loader`）——出现就看不出
         到底在执行什么，一律拒绝；
      2. `argv[0]` 要么是已知解释器（认名字不认路径），要么本身就在信任根内；
         其它可执行文件一律拒绝（旧实现完全不看 argv[0]，而它才是进程真正执行的东西）；
      3. **被执行的脚本**（第一个带源码扩展名的参数，没有则退回第一个像路径的参数）
         必须落在信任根内；它之后再出现"像代码"的参数也要过同一道；
      4. 相对路径一律按 `wake.cwd`（实际执行目录）解析，不再按 hub 进程自己的 cwd 猜。
    实现见 `_check_exec_command()`。数据参数（`dump.json`、`--hub http://…`）**不**参与
    校验——把它们当脚本会误伤合法配置（实测 G1 与 demo adapter 翻红过）。
    """
    cfg = entry.get("wake") or {}
    if isinstance(cfg, str):
        cfg = {"kind": "http", "url": cfg}
    if not isinstance(cfg, dict):
        return False, "wake 配置不是对象"
    kind = str(cfg.get("kind") or "").strip().lower()
    if kind in ("", "queue", "none"):
        return True, ""
    if kind == "http":
        url = str(cfg.get("url") or "").strip()
        if not url:
            return False, "http 唤醒缺少 url"
        try:
            host = urllib.parse.urlparse(url).hostname or ""
        except ValueError as e:
            return False, f"http 唤醒 url 解析失败（{e}）"
        if not _is_loopback_host(host):
            return False, (f"http 唤醒 url 的主机 {host or '(空)'} 不是回环地址——"
                           "来信正文会被 POST 到它，出于安全拒绝")
        return True, ""
    if kind == "exec":
        argv = [str(c) for c in (cfg.get("command") or [])]
        if not argv:
            return False, "exec 唤醒缺少 command"
        return _check_exec_command(argv, str(cfg.get("cwd") or ""), install_roots(home))
    return False, f"未知唤醒类型: {kind}"


# --------------------------------------------------------------------------- #
# 对话线消息密度上限（★安全：挡住 A↔B / A→B→C→A 无限互刷★）
# --------------------------------------------------------------------------- #
# 已实测：`send()` 与 `do_send()` 原本都没有任何 hop / TTL / 循环判断，两个 agent
# 可以无限互相唤醒（默认 wake=true），每一跳都要花 token。
# 现有的上限只管"投递重试 5 次"，管不了 agent 之间的来回。
#
# 做法：**按"收件箱 × 时间窗"计数**，不引入任何新状态文件——
#   · 同一 `context_id` 在最近 window 秒内投进同一个收件箱超过 ctx_limit 封 → 拒；
#   · 最近 window 秒内投进同一个收件箱的信超过 any_limit 封 → 拒（兜住不带 context_id 的刷屏）。
# A↔B 与 A→B→C→A 的每一跳都会落进某个人的收件箱，所以任何回路都会在至少一个箱里堆积，
# 一定被其中一条兜住。重启后依然有效（判据就在收件箱里），失败了也是"放行"而不是误伤。
# 逃生阀：信里写 `meta.rate_bypass=true`。
CTX_WINDOW_SEC = float(os.environ.get("AGENT_ANP_CTX_WINDOW") or 600)
CTX_LIMIT = int(os.environ.get("AGENT_ANP_CTX_LIMIT") or 30)
ANY_LIMIT = int(os.environ.get("AGENT_ANP_ANY_LIMIT") or 120)


def context_rate_exceeded(name: str, context_id: str = "", home: Path = DEFAULT_HOME,
                          window: float | None = None, ctx_limit: int | None = None,
                          any_limit: int | None = None) -> tuple[bool, dict[str, Any]]:
    """同一个收件箱在最近 window 秒里是不是收到了太多信。

    返回 `(是否超限, 计数详情)`。一次读完整箱，同时数两个口径（同对话线 / 全部），
    调用方决定用哪个。`context_id` 为空时只数"全部"那一个口径。
    """
    win = CTX_WINDOW_SEC if window is None else float(window)
    ctx_cap = CTX_LIMIT if ctx_limit is None else int(ctx_limit)
    any_cap = ANY_LIMIT if any_limit is None else int(any_limit)
    cutoff = time.time() - max(1.0, win)
    cid = str(context_id or "").strip()
    # 快速预筛：json.dumps 默认分隔符是 ", " 和 ": "，所以这个字面量一定出现在序列化结果里。
    needle = '"context_id": "' + cid + '"' if cid else ""
    ctx_hits = 0
    any_hits = 0
    for line in read_text_shared(inbox_path(name, home)).splitlines():
        line = line.strip()
        if not line:
            continue
        if needle and needle in line:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                rec = {}
            if str((rec.get("meta") or {}).get("context_id") or "") == cid:
                if float(rec.get("ts") or 0) >= cutoff:
                    ctx_hits += 1
        # "全部"口径不需要解析：ts 在行首附近，取不到就当它不在窗口内（宁可放过）
        if '"ts": ' in line:
            try:
                ts = float(line.split('"ts": ', 1)[1].split(",", 1)[0])
            except (IndexError, ValueError):
                continue
            if ts >= cutoff:
                any_hits += 1
    exceeded = (bool(cid) and ctx_hits > ctx_cap) or (any_cap > 0 and any_hits > any_cap)
    return exceeded, {"context_id": cid, "ctx_hits": ctx_hits, "ctx_limit": ctx_cap,
                      "any_hits": any_hits, "any_limit": any_cap, "window_sec": win}


# --------------------------------------------------------------------------- #
# 唤醒投递
# --------------------------------------------------------------------------- #
def http_post(url: str, payload: dict[str, Any], timeout: float = 5.0,
              key: str = "", extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    """POST 一个 JSON 载荷。

    key 非空时带上 `x-mailbox-key` 头：**信箱是唯一知道收件人密钥的地方**，
    投递时顺手捎上，收信端就不必自己去盘上翻密钥文件（协议 §3.1）。
    """
    try:
        data = json.dumps(safe(payload), ensure_ascii=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError) as e:
        return {"ok": False, "error": f"载荷无法编码为 UTF-8: {type(e).__name__}: {e}"}
    headers = {"content-type": "application/json; charset=utf-8"}
    if key:
        headers["x-mailbox-key"] = str(key)
    if extra_headers:
        headers.update({str(k): str(v) for k, v in extra_headers.items()})
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return {"ok": True, "status": resp.status, **json.loads(raw or "{}")}
            except json.JSONDecodeError:
                return {"ok": True, "status": resp.status, "raw": raw}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return {"ok": False, "status": e.code, "error": body[:500]}
    except Exception as e:  # noqa: BLE001 - 唤醒失败不能影响投递
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def delivery_mode(entry: dict[str, Any]) -> str:
    """这个身份的投递档位：`http`（Core push）/ `exec`（Core launch）/ `queue`（只落盘）。

    ★为什么要有这个函数★（P0-0，2026-09-22）：以前"没有唤醒端点"被当成一次**投递失败**，
    于是 queue 档身份每收一封信都会 `attempts+1` 并排重投，自动重投再白试 4 次，
    最后标"已放弃自动重投"并误发一个 `message.undelivered`。而错误信息本身已经写着
    "仅落盘，等待对方主动收信"——**代码知道这是 queue，却没有按 queue 处理**。

    判定规则与 `wake()` 自己的分派保持一致：kind 显式为 queue → queue；
    http 有 url → http；exec 有 command → exec；其余一律 queue（没有 push 目标）。
    """
    wake_cfg = entry.get("wake") or {}
    if isinstance(wake_cfg, str):
        wake_cfg = {"kind": "http", "url": wake_cfg}
    kind = str(wake_cfg.get("kind") or "").strip().lower()
    if kind == "queue":
        return "queue"
    if kind == "http" and wake_cfg.get("url"):
        return "http"
    if kind == "exec" and wake_cfg.get("command"):
        return "exec"
    return "queue"


def wake(entry: dict[str, Any], message: dict[str, Any], timeout: float = 5.0,
         key: str = "", home: Path | str | None = None) -> dict[str, Any]:
    """按身份登记信息唤醒收件人。没配唤醒端点就只落盘（返回 ok=False + 说明）。

    key：收件人自己的身份令牌。http 档会带在 `x-mailbox-key` 头里一起投过去，
    exec 档会放进子进程环境 `AGENT_ANP_SELF_KEY`，让唤醒链一路都拿得到——
    这样收信端校验身份时不用自己去猜、去翻盘。
    """
    wake_cfg = entry.get("wake") or {}
    if isinstance(wake_cfg, str):
        wake_cfg = {"kind": "http", "url": wake_cfg}
    # ★信任边界★：唤醒配置来自无完整性保护的账本，执行前必须过一道校验
    # （exec 限安装目录内 / http 限回环），否则"能写账本"就等于"能以本用户执行任意命令"。
    trusted, why = check_wake_config(entry, home)
    if not trusted:
        return {"ok": False, "kind": str(wake_cfg.get("kind") or "none"),
                "error": f"唤醒配置未通过信任校验（{why}）"}
    kind = str(wake_cfg.get("kind") or "").strip()
    recipient = str(entry.get("name") or wake_cfg.get("identity") or "")
    if kind == "http" and wake_cfg.get("url"):
        return http_post(str(wake_cfg["url"]), {"message": message, "to": recipient},
                         timeout, key=key)
    if kind == "exec" and wake_cfg.get("command"):
        import subprocess

        cmd = [str(c) for c in wake_cfg["command"]]
        env = {**os.environ, "AGENT_ANP_WAKE_JSON": json.dumps(safe(message), ensure_ascii=False)}
        if recipient:
            env["AGENT_ANP_SELF"] = recipient
        if key:
            # 两个名字都给：SELF_KEY 是本协议的显式名，AGENT_ANP_KEY 是既有约定，
            # 收信端（含"子进程再转发一次"的链路）任取其一即可比对。
            env["AGENT_ANP_SELF_KEY"] = str(key)
            env["AGENT_ANP_KEY"] = str(key)
        try:
            proc = subprocess.Popen(  # noqa: S603 - 命令来自本机配置
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                cwd=wake_cfg.get("cwd") or None,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return {"ok": True, "kind": "exec", "pid": proc.pid}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "kind": "exec", "error": f"{type(e).__name__}: {e}"}
    if kind in ("queue", "none", ""):
        return {"ok": False, "kind": kind or "none", "error": "收件人未配置唤醒端点（仅落盘，等待对方主动收信）"}
    return {"ok": False, "kind": kind, "error": f"未知唤醒类型: {kind}"}


def fallback_endpoint(name: str, hub: str = DEFAULT_HUB) -> dict[str, Any]:
    """本地兜底：把唤醒端点直接指向 DSH web 服务上的桥接路由。"""
    return {"name": name, "wake": {"kind": "http", "url": f"{hub.rstrip('/')}/mail/wake"}}


# --------------------------------------------------------------------------- #
# hub 存活保障（懒启动单例）
# --------------------------------------------------------------------------- #
def hub_url(hub: str = DEFAULT_HUB) -> str:
    return hub.rstrip("/")


def hub_alive(hub: str = DEFAULT_HUB, timeout: float = 1.5) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(hub_url(hub) + "/health", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def webui_alive(host: str = "127.0.0.1", port: int = 8898, timeout: float = 1.5) -> tuple[bool, str]:
    """判断端口上跑的**是不是看板**。返回 (是否看板, 占用者的 service 名或错误原因)。

    为什么不能只问"端口有没有人在听"：
        曾经两边（hub 的伴生启动、supervisor 的守护循环）都只做 connect_ex，
        有就认为"看板在"。于是一个**探针 hub 占了看板端口**之后，两边都认定看板好好的，
        谁都不去重启 —— 主人打开浏览器看到的是 hub 的 JSON 404，表现为"看板掉了"。
        必须问清"是谁在听"：看板的 /health 自报 service=AgentNetPort-webui。

    第二个返回值用来把占用者说清楚，好让人知道该请谁挪位子。
    """
    import json
    import urllib.request

    url = f"http://{host}:{int(port)}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return False, f"无响应（{type(e).__name__}）"
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return False, "响应不是 JSON"
    service = str(data.get("service") or "") if isinstance(data, dict) else ""
    if service == "AgentNetPort-webui":
        return True, "AgentNetPort-webui"
    return False, service or "未知服务"


def spawn_hub(hub: str = DEFAULT_HUB, home: Path = DEFAULT_HOME) -> dict[str, Any]:
    """后台拉起 hub（分离进程，不占控制台）。已监听时由 hub 自己退出，天然单例。"""
    import subprocess
    import urllib.parse

    parsed = urllib.parse.urlparse(hub_url(hub))
    host = parsed.hostname or "127.0.0.1"
    # ★与 hub 的 argparse 默认值同一个数★（2026-09-26 审计：这里写 8899、
    # README/SDK/DEFAULT_HUB 写 8900，"默认端口"有两个值就等于没有默认值）
    port = parsed.port or 8900
    hub_py = Path(__file__).resolve().parent / "anp_hub.py"
    log_dir = Path(home) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "hub.log", "a", encoding="utf-8")  # noqa: SIM115 - 交给子进程持有
    try:
        proc = subprocess.Popen(  # noqa: S603
            [_python(), str(hub_py),
             "--host", host, "--port", str(port), "--home", str(home)],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        return {"ok": True, "pid": proc.pid, "port": port, "log": str(log_dir / "hub.log")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _python() -> str:
    configured = os.environ.get("AGENT_ANP_PYTHON") or sys.executable or "python"
    path = Path(configured)
    # A desktop launcher may itself run under pythonw. The Hub needs a console
    # interpreter so redirected stdout/stderr work reliably.
    if path.name.lower() == "pythonw.exe":
        console = path.with_name("python.exe")
        if console.is_file():
            return str(console)
        fallback = shutil.which("python.exe") or shutil.which("python")
        if fallback:
            return fallback
    return configured


def ensure_hub(hub: str = DEFAULT_HUB, home: Path = DEFAULT_HOME, timeout: float = 6.0) -> dict[str, Any]:
    """hub 不在就拉起并等它就绪。返回 {ok, started, waited}。"""
    if hub_alive(hub):
        return {"ok": True, "started": False}
    spawn = spawn_hub(hub, home)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if hub_alive(hub, timeout=0.5):
            return {"ok": True, "started": True, "spawn": spawn}
        time.sleep(0.25)
    return {"ok": False, "started": True, "spawn": spawn, "error": "hub 启动超时"}
