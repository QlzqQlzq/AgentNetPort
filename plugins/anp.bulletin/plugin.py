"""公告栏插件（ANP 的第一个默认插件）。

★这个文件的存在本身就是一次验证★：公告栏曾经是 Core 的一部分（`core.post_bulletin()` +
hub 的 `do_announce()` + MCP 的 `mail_announce`/`mail_board`）。搬成插件之后，
Core 里**一行公告相关代码都不剩**——公告栏是"通信的一种用法"，不是通信本身。

用法（对 agent 暴露的两个工具）：
    anp_bulletin_post   贴一份公告并推送给在线的 agent
    anp_bulletin_list   回看最近的公告（不需要推送权限）

数据只落在自己的目录里（`data/bulletin.jsonl`），不碰 `<home>` 下任何信箱文件。
第一次启动时如果自己的数据还没有、而信箱根目录有旧的 `bulletin.jsonl`，就把它迁进来。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
BOARD = DATA / "bulletin.jsonl"
PORT = int(os.environ.get("ANP_PLUGIN_PORT") or "0")
HOME = Path(os.environ.get("AGENT_ANP_HOME") or Path.home() / ".agentnetport")
# Core 在 /anp/register 时给；拿不到就退回默认端口（插件自己起在 8900 旁时才用得上）
IDENTITY = ""
TOKEN = ""
CONFIG: dict = {}

CONFIG_PATH = HERE / "config.json"
if CONFIG_PATH.exists():
    try:
        CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 配置读坏不该让插件起不来
        CONFIG = {}

LIMIT = int(CONFIG.get("keep", 500))


# --------------------------------------------------------------------------- #
# 与 Core 通话（只走 Plugin → Core 那一侧，spec §4 方向 A）
# --------------------------------------------------------------------------- #
def _core_post(path: str, payload: dict) -> tuple[bool, dict]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"content-type": "application/json; charset=utf-8"}
    if TOKEN:
        headers["x-mailbox-key"] = TOKEN
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{os.environ.get('ANP_HUB_PORT', '8900')}{path}",
            data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return True, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:  # noqa: BLE001
            return False, {"error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return False, {"error": f"{type(e).__name__}: {e}"}


def _core_get(path: str) -> tuple[bool, dict]:
    headers = {}
    if TOKEN:
        headers["x-mailbox-key"] = TOKEN
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{os.environ.get('ANP_HUB_PORT', '8900')}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return True, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception as e:  # noqa: BLE001
        return False, {"error": f"{type(e).__name__}: {e}"}


def _hub_port() -> str:
    """hub 端口：Core 在 register 时通过 base_url 给，拿不到就猜 8900。"""
    return str(CONFIG.get("hub_port") or os.environ.get("ANP_HUB_PORT") or "8900")


# --------------------------------------------------------------------------- #
# 公告栏自己的存储
# --------------------------------------------------------------------------- #
def _rows() -> list[dict]:
    if not BOARD.exists():
        return []
    out = []
    for line in BOARD.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _migrate_legacy() -> int:
    """把信箱根目录的旧 bulletin.jsonl 搬进来（只做一次，幂等）。"""
    legacy = HOME / "bulletin.jsonl"
    if BOARD.exists() or not legacy.exists():
        return 0
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        moved = 0
        with open(BOARD, "a", encoding="utf-8") as out:
            for line in legacy.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.write(line.rstrip() + "\n")
                    moved += 1
        # 旧文件改名留档，不删（搬迁要可回滚）
        legacy.rename(HOME / "bulletin.jsonl.migrated")
        return moved
    except Exception:  # noqa: BLE001 - 迁失败不该让插件起不来
        return 0


def _append(rec: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with open(BOARD, "a", encoding="utf-8") as fp:
        print(json.dumps(rec, ensure_ascii=False), file=fp)
    # 只留最近 LIMIT 条（公告是及时性的，不需要无限长）
    rows = _rows()
    if len(rows) > LIMIT:
        BOARD.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[-LIMIT:]),
                         encoding="utf-8")


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #
def _online_names(exclude: set[str]) -> list[str]:
    """在线身份。从 `/agents` 自己判断——Core 只返回 last_seen，不替插件做"在线"决定。"""
    ok, data = _core_get("/agents")
    if not ok:
        return []
    ttl = float(CONFIG.get("online_ttl", 300))
    now = time.time()
    out = []
    for a in data.get("agents") or []:
        name = str(a.get("name") or "")
        if not name or name == "hub" or name in exclude:
            continue
        if now - float(a.get("last_seen") or 0) <= ttl:
            out.append(name)
    return sorted(out)


def tool_post(args: dict, caller: str) -> dict:
    body = str(args.get("body") or "").strip()
    if not body:
        return {"ok": False, "error": "公告正文不能为空"}
    exclude = {str(x).strip() for x in (args.get("exclude") or []) if str(x).strip()}
    include_self = bool(args.get("include_self"))
    strong = bool(args.get("online_only") is False)   # 兼容旧参数：online_only=false = 强唤醒
    online_only = not strong
    push = bool(args.get("wake", True))
    subject = args.get("subject")
    kind = str(args.get("kind") or "announcement")
    need_reply = bool(args.get("need_reply"))

    rec = {
        "id": f"b-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        "ts": time.time(),
        "from": caller,
        "subject": subject,
        "body": body,
        "kind": kind,
        "need_reply": need_reply,
    }
    _append(rec)

    if not include_self:
        exclude.add(caller)
    audience = _online_names(exclude) if online_only else _all_names(exclude)
    pushed = 0
    failures: list[dict] = []
    if push and audience:
        # 用一封多人信推给全体：Core 的群发语义，插件不自己实现广播。
        # meta.broadcast=true 让看板仍能把它们标成"广播副本"。
        ok, res = _core_post("/messages/send", {
            "from": IDENTITY or "anp.bulletin",
            "to": audience,
            "body": body,
            "subject": subject,
            "kind": kind,
            "reply_to": None,
            "correlation_id": None,
            "wake": True,
            "meta": {"broadcast": True, "board_id": rec["id"],
                     "need_reply": need_reply, "posted_by": caller},
        })
        if ok and res.get("ok"):
            pushed = int(res.get("delivered") or 0)
        else:
            failures.append({"error": res.get("error"), "code": res.get("code")})
    return {"ok": True, "text": json.dumps({
        "id": rec["id"], "pushed": pushed, "audience": len(audience),
        "online_only": online_only, "board": str(BOARD),
        "note": "公告已贴上公告栏；在线的人已推送，其他人上线后可用 anp_bulletin_list 看",
        **({"push_error": failures[0]} if failures else {}),
    }, ensure_ascii=False)}


def _all_names(exclude: set[str]) -> list[str]:
    ok, data = _core_get("/agents")
    if not ok:
        return []
    return sorted({str(a.get("name") or "") for a in (data.get("agents") or [])
                   if a.get("name") and a["name"] != "hub" and a["name"] not in exclude})


def tool_list(args: dict, caller: str) -> dict:
    limit = int(args.get("limit") or 20)
    rows = _rows()[-limit:][::-1]
    return {"ok": True, "text": json.dumps({
        "count": len(rows),
        "bulletins": [{
            "id": r.get("id"), "ts": r.get("ts"), "from": r.get("from"),
            "subject": r.get("subject"), "kind": r.get("kind"),
            "need_reply": r.get("need_reply"), "body": r.get("body"),
            "time": time.strftime("%m-%d %H:%M:%S", time.localtime(float(r.get("ts") or 0))),
        } for r in rows],
        "note": "公告是给全体的、及时性的信息；不记已读未读。",
    }, ensure_ascii=False)}


TOOLS = {
    "post": tool_post,
    "list": tool_list,
}


# --------------------------------------------------------------------------- #
# HTTP 端点（Core → Plugin 那一侧，spec §4 方向 B）
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: N802
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] == "/anp/panel":
            # 面板：给看板的一段内容。Core 只原样转发，不理解它。
            rows = _rows()[-50:][::-1]
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            body = json.dumps({
                "ok": True,
                "plugin": IDENTITY or "anp.bulletin",
                "title": "公告栏",
                "note": "一份公告给全体，不记已读未读",
                "count": len(rows),
                "bulletins": [{
                    "id": r.get("id"), "time": time.strftime(
                        "%m-%d %H:%M:%S", time.localtime(float(r.get("ts") or 0))),
                    "from": r.get("from"), "subject": r.get("subject"),
                    "kind": r.get("kind"), "body": r.get("body"),
                } for r in rows],
            }, ensure_ascii=False).encode()
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}
        path = self.path.split("?")[0]

        if path == "/anp/register":
            global IDENTITY, TOKEN
            IDENTITY = str(body.get("identity") or "")
            TOKEN = str(body.get("token") or "")
            # Core 通过 config 把 hub 端口捎来，插件自己拼 Messages API 的 base_url
            hub_port = str((body.get("config") or {}).get("hub_port") or "8900")
            CONFIG["hub_port"] = hub_port
            self._send(200, {
                "ok": True,
                "tools": [
                    {"name": "post",
                     "description": "贴一份公告并推送给在线的 agent（一份公告给全体，"
                                    "不落任何人的收件箱之外的地方；只有公告栏存一份记录）",
                     "input_schema": {"type": "object", "properties": {
                         "body": {"type": "string", "description": "公告正文"},
                         "subject": {"type": "string", "description": "可选主题"},
                         "kind": {"type": "string", "description": "类别，默认 announcement"},
                         "need_reply": {"type": "boolean", "description": "是否需要大家答复"},
                         "online_only": {"type": "boolean",
                                         "description": "false = 强唤醒，推给名册全体"},
                         "wake": {"type": "boolean", "description": "false = 只贴不推"},
                         "include_self": {"type": "boolean"},
                         "exclude": {"type": "array", "items": {"type": "string"}},
                     }, "required": ["body"]}},
                    {"name": "list",
                     "description": "回看最近的公告（不需要推送权限）",
                     "input_schema": {"type": "object", "properties": {
                         "limit": {"type": "integer", "description": "最多几条，默认 20"},
                     }}},
                ],
                "events": [],
                "panel": "/anp/panel",
            })
            return

        if path == "/anp/wake":
            # 公告推送走的是普通多人信；真有人直接唤醒本插件时，记一下就行
            self._send(200, {"ok": True, "mode": "session"})
            return

        if path == "/anp/tool":
            name = str(body.get("name") or "")
            args = body.get("arguments") if isinstance(body.get("arguments"), dict) else {}
            caller = str(body.get("caller_identity") or "unknown")
            fn = TOOLS.get(name)
            if not fn:
                self._send(200, {"ok": False, "error": f"公告栏没有 {name} 这个工具"})
                return
            try:
                self._send(200, fn(args, caller))
            except Exception as e:  # noqa: BLE001
                self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return

        self._send(404, {"ok": False, "error": f"未知端点 {path}"})


def main() -> int:
    _migrate_legacy()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
