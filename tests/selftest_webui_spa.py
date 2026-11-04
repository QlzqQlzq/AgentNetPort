"""WebUI SPA 功能验收：API 面 + 无文件/旧模块依赖。

不打开浏览器，直接验证：
  1. SPA 源码不引用 inbox/ledger/mailbox_* / keys.json
  2. /admin/messages 无 correlation_id 可浏览
  3. 过滤 / 分页 / 只读
  4. /agents /health /admin/panel 面齐（CORS 头存在）
  5. admin 鉴权
  6. Core 断开时 SPA 侧仅有 offline 文案（源码含离线处理，无崩溃路径依赖文件）
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
WEBUI = REPO / "webui"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import anp_core as core  # noqa: E402

results: list[tuple[str, bool, str]] = []
TMP = Path(tempfile.mkdtemp(prefix="anp-webui-"))


def say(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, bool(ok), extra))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  <- {extra}" if extra else ""))


def get(base: str, path: str, admin: str = "") -> tuple[int, dict, dict]:
    headers = {"x-anp-admin": admin} if admin else {}
    if "?" in path:
        raw_path, raw_qs = path.split("?", 1)
        path = raw_path + "?" + urllib.parse.urlencode(urllib.parse.parse_qsl(raw_qs, keep_blank_values=True))
    req = urllib.request.Request(base + path, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}"), dict(e.headers)
        except Exception:  # noqa: BLE001
            return e.code, {}, dict(e.headers)


def main() -> int:
    # ---- 静态：SPA 无旧依赖 ----
    src_all = ""
    for name in ("index.html", "styles.css", "app.js", "i18n.js"):
        p = WEBUI / name
        say(f"S0 {name} 存在", p.is_file())
        if p.is_file():
            src_all += p.read_text(encoding="utf-8")
    for bad in ("inbox/", "ledger.jsonl", "mailbox_core", "mailbox_policy", "mailbox_webui", "keys.json"):
        say(f"S1 不引用 {bad}", bad not in src_all)
    say("S2 只走管理 API 路径",
        all(x in src_all for x in ("/admin/messages", "/agents", "/health", "/admin/panel")))
    # 文案在 i18n.js，行为在 app.js——两边都要在，不能只扫三件套
    say("S3 含离线/重连 UI",
        ("offline" in src_all.lower() or "离线" in src_all)
        and ("刷新" in src_all or "refresh" in src_all.lower())
        and ("connection_failed" in src_all or "core_offline" in src_all))
    say("S3b 心跳灯探测显式 platform_url",
        "platform_url" in src_all
        and "platformHealthUrlsOf" in src_all
        and "platformUrls.has(u)" in src_all)
    heartbeat_idx = src_all.find('if (heartbeatFresh) return "online";')
    fallback_idx = src_all.find('if (a.wake) return "wakeable";')
    say("S3d 进程探针优先，heartbeat 仅作兜底",
        "if (platformOk || adapterOk) return \"online\";" in src_all
        and heartbeat_idx >= 0 and fallback_idx >= 0
        and src_all.find('if (platformOk || adapterOk) return "online";') < heartbeat_idx
        and heartbeat_idx < fallback_idx)
    say("S3e Hub 地址不被当成消息搜索条件",
        "isHubAddressSearch" in src_all
        and "messageSearchValue" in src_all
        and "clearAccidentalHubSearch" in src_all)
    say("S3c 消息页不再按身份前缀二次删信",
        "filter(isRealPeerMessage)" not in src_all
        and "STATE.messages = Array.isArray(data.messages)" in src_all)
    # 实际形态是 Settings 里的 Admin Token，不是单独的 unlock 按钮
    say("S4 含 admin 解锁（Settings → Admin Token）",
        "adminToken" in src_all and "settings_admin_token" in src_all
        and "x-anp-admin" in src_all)
    say("S5 含搜索/过滤/分页", all(x in src_all for x in ("search", "unreadOnly", "PAGE_SIZE", "offset")))
    say("S6 不展示未实现的回复占位按钮",
        all(x not in src_all for x in (
            "btn-reply", "btn_reply", "Reply functionality not yet implemented")))

    # ---- 动态：对着真 hub ----
    home = TMP / "home"
    home.mkdir(parents=True, exist_ok=True)
    admin = "test-admin-token-selftest-webui"
    os.environ["AGENT_ANP_ADMIN_TOKEN"] = admin
    os.environ["AGENT_ANP_HUB_SECRET"] = "test-hub-secret-selftest-webui"
    (home / "keys.json").write_text(json.dumps({"alice": "ak-w", "bob": "bk-w"}), encoding="utf-8")
    core.send("alice", "bob", "无关联私信", subject="私信", home=home)
    core.send("alice", "bob", "有关联", subject="协作", correlation_id="ui:1", home=home)

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
         "--port", str(port), "--home", str(home)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "AGENT_ANP_WEBUI": "0", "AGENT_ANP_MAINTENANCE": "0",
             "AGENT_ANP_ADMIN_TOKEN": admin,
             "AGENT_ANP_HUB_SECRET": os.environ["AGENT_ANP_HUB_SECRET"]},
    )
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)

        code, r, hdrs = get(base, "/health")
        say("H1 /health 可用", code == 200 and r.get("ok") is True)
        say("H2 CORS 头存在（独立 SPA 跨源）", "access-control-allow-origin" in {k.lower() for k in hdrs})

        code, r, _ = get(base, "/agents")
        say("H3 /agents 可用", code == 200 and isinstance(r.get("agents"), list))

        code, r, _ = get(base, "/admin/messages")
        say("H4 无 admin → 401", code == 401)

        code, r, _ = get(base, "/admin/messages", admin=admin)
        msgs = r.get("messages") or []
        say("H5 全局消息浏览", code == 200 and len(msgs) >= 2)
        say("H6 无 correlation_id 可浏览", any(not m.get("correlation_id") for m in msgs))
        say("H7 消息带 inbox", all(m.get("inbox") for m in msgs))

        code, r, _ = get(base, "/admin/messages?q=无关联", admin=admin)
        say("H8 搜索过滤", len(r.get("messages") or []) == 1)

        code, r, _ = get(base, "/admin/messages?limit=1&offset=0", admin=admin)
        code2, r2, _ = get(base, "/admin/messages?limit=1&offset=1", admin=admin)
        say("H9 分页 offset", r.get("returned") == 1 and r2.get("returned") == 1
            and (r.get("messages") or [{}])[0].get("id") != (r2.get("messages") or [{}])[0].get("id"))

        code, r, _ = get(base, "/admin/panel", admin=admin)
        say("H10 /admin/panel 列表", code == 200)

        code, r, _ = get(base, "/admin/messages", admin=admin)
        before = {(m.get("id"), m.get("read"), m.get("attempts")) for m in (r.get("messages") or [])}
        get(base, "/admin/messages?unread=1", admin=admin)
        code, r2, _ = get(base, "/admin/messages", admin=admin)
        after = {(m.get("id"), m.get("read"), m.get("attempts")) for m in (r2.get("messages") or [])}
        say("H11 只读（状态不变）", before == after)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        if os.environ.get("ANP_TEST_KEEP") != "1":
            shutil.rmtree(TMP, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} 通过 =====")
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print("失败项: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
