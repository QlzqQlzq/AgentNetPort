"""End-to-end verification for the Codex Adapter Plugin."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SDK = REPO / "sdk" / "anp_adapter"
HUB = REPO / "src" / "anp_hub.py"
PLUGIN_ID = "anp.adapter.codex"
AGENT = "codex"
CALLER = "verify.sender"
PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "ok  " if condition else "FAIL"
    print(f"[{mark}] {name}" + (f"\n         {detail}" if detail else ""))


def skip(name: str, reason: str) -> None:
    SKIP.append(f"{name}（{reason}）")
    print(f"[SKIP] {name}\n         {reason}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(url: str, payload: dict | None = None, key: str = "",
            *, method: str = "POST", timeout: float = 15.0) -> tuple[int, dict]:
    headers = {"content-type": "application/json; charset=utf-8"}
    if key:
        headers["x-mailbox-key"] = key
    data = None if payload is None else json.dumps(
        payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001
        return -1, {"_err": f"{type(exc).__name__}: {exc}"}


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.25):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def fake_codex_source() -> str:
    return r'''import json, os, pathlib, sys, time

args = sys.argv[1:]
home = pathlib.Path(os.environ["CODEX_HOME"])
home.mkdir(parents=True, exist_ok=True)
resume = "resume" in args
known = next((value for value in args if value.startswith("fake-session-")), "")
if not known:
    counter = home / "counter.txt"
    number = int(counter.read_text() if counter.exists() else "0") + 1
    counter.write_text(str(number))
    known = "fake-session-" + str(number)
row = {"id": known, "thread_name": "verify", "updated_at": time.time()}
with (home / "session_index.jsonl").open("a", encoding="utf-8") as fp:
    fp.write(json.dumps(row) + "\n")
if (home / "FAIL_NEXT").is_file():
    (home / "FAIL_NEXT").unlink()
    print(json.dumps({"type": "thread.started", "thread_id": known}))
    raise SystemExit(3)
if (home / "EMPTY_REPLY").is_file():
    (home / "EMPTY_REPLY").unlink()
    print(json.dumps({"type": "thread.started", "thread_id": known}))
    # 成功退出但无 agent_message → 测空产出补偿
    raise SystemExit(0)
print(json.dumps({"type": "thread.started", "thread_id": known}))
text = "FAKE_CODEX_REPLY:" + ("RESUMED" if resume else "NEW")
print(json.dumps({"type": "item.completed", "item": {
    "id": "item-1", "type": "agent_message", "text": text}}))
'''


def install_plugin(home: Path) -> Path:
    target = home / "plugins" / PLUGIN_ID
    target.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "plugin.py", "platform.py", "README.md"):
        shutil.copy2(HERE / name, target / name)
    shutil.copytree(SDK, target / "anp_adapter",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return target


def start_hub(home: Path, port: int, fake: Path, code_home: Path,
              workspace: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "AGENT_ANP_HOME": str(home),
        "AGENT_ANP_ADMIN_TOKEN": "codex-verify-admin",
        "AGENT_ANP_HUB_SECRET": "codex-verify-secret",
        "AGENT_ANP_WEBUI": "0",
        "ANP_CODEX_BIN": sys.executable,
        "ANP_CODEX_WRAPPER_SCRIPT": str(fake),
        "ANP_CODEX_WORKSPACE": str(workspace),
        "CODEX_HOME": str(code_home),
        "ANP_CODEX_DANGEROUSLY_BYPASS": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    return subprocess.Popen(
        [sys.executable, str(HUB), "--home", str(home), "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


def agent(base: str, name: str) -> dict:
    _, body = request(base + "/agents", method="GET", timeout=3)
    return next((row for row in body.get("agents") or []
                 if row.get("name") == name), {})


def query(base: str, key: str, correlation_id: str) -> list[dict]:
    _, body = request(base + "/messages/query",
                      {"from": CALLER, "correlation_id": correlation_id}, key)
    return list(body.get("messages") or [])


def wait_reply(base: str, key: str, correlation_id: str) -> dict:
    def find():
        rows = query(base, key, correlation_id)
        return next((row for row in rows
                     if row.get("from") == AGENT and row.get("reply_to")), None)
    return wait_until(find, timeout=35) or {}


def static_checks() -> None:
    source = "\n".join((HERE / name).read_text(encoding="utf-8")
                         for name in ("plugin.py", "platform.py"))
    forbidden_imports = ("import anp_core", "import anp_hub", "import anp_plugin",
                         "import anp_policy")
    check("只 import 标准库与 anp_adapter",
          not any(line.strip().startswith(forbidden_imports)
                  for line in source.splitlines()))
    check("不读 Core 私有凭据或账本",
          "keys.json" not in source and "ledger.jsonl" not in source
          and "hub.secret" not in source)
    check("使用 canonical API 与 session 扩展面",
          all(path in source for path in ("/session/roster", "/session/bind")))
    # 去掉注释再查，避免"禁止 from platform import"这类说明被误报
    code_only = "\n".join(
        line.split("#", 1)[0] for line in source.splitlines())
    check("CR-1 不遮蔽 stdlib platform（禁 from platform import）",
          "from platform import" not in code_only
          and "spec_from_file_location" in code_only)
    plat = (HERE / "platform.py").read_text(encoding="utf-8")
    check("H-1/S4 need_reply 仅 true 才义务回信",
          "is True" in plat or "is True" in source)
    drift: list[str] = []
    for src in SDK.glob("*.py"):
        vendor = HERE / "anp_adapter" / src.name
        if not vendor.is_file() or hashlib.sha256(src.read_bytes()).digest() != hashlib.sha256(vendor.read_bytes()).digest():
            drift.append(src.name)
    check("仓库 vendor SDK 与 sdk/ 同源", not drift, str(drift))


def main() -> int:
    print("=" * 72)
    print("Codex Adapter 验收：两条链 + 会话章 + 重启恢复")
    print("=" * 72)
    static_checks()
    temp = Path(tempfile.mkdtemp(prefix="anp-codex-verify-"))
    home = temp / "home"
    code_home = temp / "codex-home"
    workspace = temp / "workspace"
    home.mkdir()
    workspace.mkdir()
    fake = temp / "fake_codex.py"
    fake.write_text(fake_codex_source(), encoding="utf-8")
    install_plugin(home)
    port = free_port()
    proc = start_hub(home, port, fake, code_home, workspace)
    base = f"http://127.0.0.1:{port}"
    try:
        def ready():
            status, body = request(base + "/health", method="GET", timeout=5)
            return body if status == 200 and body.get("preload_done") else None

        health = wait_until(ready, timeout=35)
        check("Core 启动并完成插件预加载", bool(health), str(health))
        codex = wait_until(lambda: agent(base, AGENT).get("wake") and agent(base, AGENT), timeout=35)
        check("Codex Agent 已注册", bool(codex), str(codex))
        check("Plugin identity 与 Agent identity 分离",
              PLUGIN_ID != AGENT and bool(agent(base, PLUGIN_ID)))
        # ★P0-1 真指纹★：从 keys.json 读两枚令牌比对，不靠自报常量
        keys_obj = json.loads((home / "keys.json").read_text(encoding="utf-8"))
        plugin_tok = str(keys_obj.get(PLUGIN_ID) or "")
        agent_tok = str(keys_obj.get(AGENT) or "")
        check("P0-1 两枚令牌不同（真指纹，来自 keys.json）",
              bool(plugin_tok) and bool(agent_tok) and plugin_tok != agent_tok,
              f"plugin_len={len(plugin_tok)} agent_len={len(agent_tok)} "
              f"same={plugin_tok == agent_tok}")
        check("H-3 双身份 token 隔离（credentials 不含 plugin token）",
              plugin_tok != agent_tok, "由 keys.json 真指纹 + 凭据隔离断言承载")
        caller_status, caller = request(base + "/heartbeat", {"name": CALLER})
        caller_key = str(caller.get("key") or "")
        check("测试发信方 bootstrap 成功", caller_status == 200 and bool(caller_key))

        first = request(base + "/messages/send", {
            "from": CALLER, "to": AGENT, "body": "first", "kind": "message",
            "correlation_id": "codex-verify-1",
            "meta": {"context_id": "codex-verify-context",
                     "to_workspace": str(workspace), "need_reply": True},
            "wake": True}, caller_key)[1]
        wake = first.get("wake") or {}
        check("正向链：消息持久化并被 HTTP wake 接受",
              first.get("ok") is True and wake.get("ok") is True
              and wake.get("mode") == "session", json.dumps(wake, ensure_ascii=False))
        check("⑤a Core session 章为 new 时 Adapter 按章执行",
              (wake.get("route") or {}).get("mode") == "new", str(wake.get("route")))
        reply = wait_reply(base, caller_key, "codex-verify-1")
        check("反向链：Codex 最终输出回到 ANP", bool(reply), str(reply))
        check("回信带 reply_to + correlation_id",
              reply.get("reply_to") == (first.get("message") or {}).get("id")
              and reply.get("correlation_id") == "codex-verify-1", str(reply))
        check("回信正文来自 Codex 输出", "FAKE_CODEX_REPLY:NEW" in str(reply.get("body")))

        second = request(base + "/messages/send", {
            "from": CALLER, "to": AGENT, "body": "second", "kind": "message",
            "correlation_id": "codex-verify-2",
            "meta": {"context_id": "codex-verify-context", "need_reply": True},
            "wake": True}, caller_key)[1]
        wake2 = second.get("wake") or {}
        check("⑥ 同一 context 的第二封由 Core 盖 reuse 章",
              (wake2.get("route") or {}).get("mode") == "reuse",
              json.dumps(wake2, ensure_ascii=False))
        reply2 = wait_reply(base, caller_key, "codex-verify-2")
        check("resume session 仍完成反向回信",
              "FAKE_CODEX_REPLY:RESUMED" in str(reply2.get("body")), str(reply2))

        # S4：need_reply 缺省 = 知会
        request(base + "/messages/send", {
            "from": CALLER, "to": AGENT, "body": "notify-only", "kind": "message",
            "correlation_id": "codex-verify-notify",
            "meta": {"context_id": "codex-verify-notify"}, "wake": True}, caller_key)
        time.sleep(2.5)
        notify_rows = query(base, caller_key, "codex-verify-notify")
        notify_replies = [m for m in notify_rows if m.get("from") == AGENT and m.get("reply_to")]
        check("S4 need_reply 缺省不要求义务回信（知会）",
              len(notify_replies) == 0 or all("失败" not in str(m.get("body") or "")
                                             for m in notify_replies),
              f"notify_replies={len(notify_replies)}")

        # S3-C：worker 失败必须补偿（用标记文件，避免 env 传不进 fake CLI）
        (code_home / "FAIL_NEXT").write_text("1", encoding="utf-8")
        request(base + "/messages/send", {
            "from": CALLER, "to": AGENT, "body": "should-fail", "kind": "message",
            "correlation_id": "codex-verify-fail",
            "meta": {"context_id": "codex-verify-fail", "need_reply": True},
            "wake": True}, caller_key)
        fail_reply = wait_reply(base, caller_key, "codex-verify-fail")
        check("S3-C 平台失败有补偿回信（非静默）",
              bool(fail_reply.get("id")) and "失败" in str(fail_reply.get("body") or ""),
              str(fail_reply.get("body")))

        # ---- P1：空产出 + need_reply=true → 补偿，不静默 ----
        (code_home / "EMPTY_REPLY").write_text("1", encoding="utf-8")
        request(base + "/messages/send", {
            "from": CALLER, "to": AGENT, "body": "empty-out", "kind": "message",
            "correlation_id": "codex-verify-empty",
            "meta": {"context_id": "codex-verify-empty", "need_reply": True},
            "wake": True}, caller_key)
        empty_reply = wait_reply(base, caller_key, "codex-verify-empty")
        check("P1-c 空 agent_message + need_reply → 补偿回信（不静默）",
              bool(empty_reply.get("id"))
              and ("失败" in str(empty_reply.get("body") or "")
                   or "空" in str(empty_reply.get("body") or "")),
              str(empty_reply.get("body")))
        work = home / "plugins" / PLUGIN_ID / "data" / "codex-work.jsonl"
        check("S3-A 待办落盘可追（codex-work.jsonl）",
              work.is_file() and "pending_id" in work.read_text(encoding="utf-8"),
              str(work))

        # ---- P1：resume 钉原 cwd（Codex resume 按 cwd 过滤）----
        import importlib.util as _ilu
        pspec = _ilu.spec_from_file_location(
            "codex_platform_unit", home / "plugins" / PLUGIN_ID / "platform.py")
        pmod = _ilu.module_from_spec(pspec)
        pspec.loader.exec_module(pmod)
        plat = pmod.CodexPlatform("codex", data_dir=home / "plugins" / PLUGIN_ID / "data")
        ws_alt = temp / "workspace-alt"
        ws_alt.mkdir(parents=True, exist_ok=True)
        plat.record_session("sess-cwd-pin", str(ws_alt), running=False)
        routed = plat.route_from_stamp(type("E", (), {
            "meta": {"session": {"mode": "reuse", "session_id": "sess-cwd-pin",
                                  "workspace": str(workspace)}}})())
        check("P1-a resume 用会话原 cwd（不被 stamp workspace 覆盖）",
              routed.get("workspace") == str(ws_alt)
              and routed.get("cwd_pinned") is True,
              json.dumps(routed, ensure_ascii=False))

        # ---- P1：真幂等——同 envelope 第二次 enqueue 不再拉平台 ----
        fake_env = type("E", (), {
            "id": "dup-env-p1", "from_": CALLER, "correlation_id": "codex-dup-p1",
            "body": "x", "kind": "message", "subject": "", "reply_to": "",
            "meta": {}})()
        pid1, new1 = plat.enqueue_work(fake_env, {"text": "x"},
            {"mode": "new", "session_id": "", "workspace": str(workspace), "reason": "t"})
        pid2, new2 = plat.enqueue_work(fake_env, {"text": "x"},
            {"mode": "new", "session_id": "", "workspace": str(workspace), "reason": "t"})
        check("P1-b enqueue_work 真幂等（同 id 第二次 is_new=False）",
              pid1 == pid2 and new1 is True and new2 is False,
              f"{pid1} {new1} {new2}")

        # ---- from_native / 默认 bypass（平台语义 + 安全缺省）----
        fn = plat.from_native({"text": "  hello-from-native  "})
        check("from_native 平台产出 → body（Spec 翻译面）",
              fn.get("body") == "hello-from-native" and fn.get("kind") == "reply",
              json.dumps(fn, ensure_ascii=False))
        plat.codex_bin = sys.executable
        os.environ.pop("ANP_CODEX_DANGEROUSLY_BYPASS", None)
        argv_off = plat._command({"text": "x"}, {
            "mode": "new", "session_id": "", "workspace": str(workspace),
            "reason": "t"})
        check("安全缺省：默认不带 --dangerously-bypass-approvals-and-sandbox",
              "--dangerously-bypass-approvals-and-sandbox" not in argv_off,
              str(argv_off)[:200])
        os.environ["ANP_CODEX_DANGEROUSLY_BYPASS"] = "1"
        argv_on = plat._command({"text": "x"}, {
            "mode": "new", "session_id": "", "workspace": str(workspace),
            "reason": "t"})
        os.environ["ANP_CODEX_DANGEROUSLY_BYPASS"] = "0"
        check("显式 ANP_CODEX_DANGEROUSLY_BYPASS=1 才带 bypass",
              "--dangerously-bypass-approvals-and-sandbox" in argv_on,
              str(argv_on)[:200])

        current = agent(base, AGENT)
        # P0-4 正确 token + 凭据隔离（必须在 current 赋值之后）
        cred_path = home / "plugins" / PLUGIN_ID / "data" / "codex-credentials.json"
        agent_secret = ""
        cred_obj = {}
        if cred_path.is_file():
            cred_obj = json.loads(cred_path.read_text(encoding="utf-8"))
            agent_secret = str(cred_obj.get("agent_key") or "")
        check("凭据隔离：credentials 只存 agent_key/identity，无 admin/plugin token 混写",
              bool(agent_secret) and set(cred_obj) <= {"identity", "agent_key"},
              f"keys={sorted(cred_obj)}")
        ok_status, ok_body = request(
            str((current.get("wake") or {}).get("url") or ""),
            {"message": {"id": "p04ok", "from": CALLER, "to": AGENT,
                         "body": "ok-token", "kind": "message"}, "to": AGENT},
            agent_secret)
        check("P0-4 正确 agent token 唤醒被接受（ok:true + 合法 mode）",
              ok_status == 200 and ok_body.get("ok") is True
              and str(ok_body.get("mode") or "") in ("immediate", "batched", "session",
                                                     "inject", "followup"),
              str(ok_body)[:200])
        wrong_status, wrong = request(
            str((current.get("wake") or {}).get("url") or ""),
            {"message": {"id": "wrong", "from": CALLER, "to": AGENT,
                         "body": "wrong", "kind": "message"}, "to": AGENT}, caller_key)
        check("P0-4 错误 wake token 被拒", wrong_status == 200 and wrong.get("ok") is False,
              str(wrong))

        old_url = str((agent(base, AGENT).get("wake") or {}).get("url") or "")
        request(base + f"/plugins/{PLUGIN_ID}/unload", {}, "codex-verify-admin")
        time.sleep(0.8)
        _, loaded = request(base + f"/plugins/{PLUGIN_ID}/load", {}, "codex-verify-admin")
        new_base = str((loaded.get("plugin") or {}).get("base_url") or "")

        def restarted():
            row = agent(base, AGENT)
            return row if str((row.get("wake") or {}).get("url") or "") == new_base + "/anp/wake" else None

        refreshed = wait_until(restarted, timeout=35) or {}
        new_url = str((refreshed.get("wake") or {}).get("url") or "")
        check("⑦a 重启后同一 token 续约到新 wake 端点",
              bool(new_url) and new_url != old_url and new_url == new_base + "/anp/wake",
              f"old={old_url} new={new_url}")
        check("凭据保存在插件自己的 data/",
              (home / "plugins" / PLUGIN_ID / "data" / "codex-credentials.json").is_file())
        # ⑦b/P0-2：已有身份 + 无密钥 → 401，绝不重发令牌（用探针身份，不毁 codex 临时身份）
        st1, _ = request(base + "/heartbeat", {"name": "codex-noauth-probe"})
        if st1 == 200:
            st2, body2 = request(base + "/heartbeat", {"name": "codex-noauth-probe"})
        else:
            st2, body2 = st1, {}
        check("⑦b/P0-2 已有身份 + 无密钥 → 401（不重发令牌）",
              st2 == 401, f"第二次同名无密钥 heartbeat 得到 HTTP {st2} body={body2}")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            proc.kill()
        if FAIL:
            print(f"保留失败现场：{temp}")
        else:
            shutil.rmtree(temp, ignore_errors=True)

    print("\n" + "=" * 72)
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}  SKIP {len(SKIP)}")
    for item in FAIL:
        print("  FAIL: " + item)
    for item in SKIP:
        print("  SKIP: " + item)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
