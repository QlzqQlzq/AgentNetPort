"""agent-mailbox 在线验证：接线并重启各端之后跑一次，确认整条链路真的通。

检查项：
1. hub 存活 + 身份账本（被检查的几个身份是否都登记了唤醒端点）
2. 各身份侧 MCP 工具可用（走 anp_mcp.py，身份由 env 决定）
3. 真投一封信给对方，看唤醒回执（mode=inject 注入当前会话 / mode=new 新建会话）
4. 可选 --expect-reply：等对方真的回信（默认 240s）

身份名不写死：默认 `codex` / `dsh`（本机这套），别的机器用 `--agents` 或
`AGENT_ANP_VERIFY_AGENTS` 指定，例如 `--agents myagent,other`。

用法：
    python verify.py                      # 快速检查（不产生模型调用）
    python verify.py --agents myagent,other
    python verify.py --expect-reply       # 额外等一封真回信
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# 兼容两种布局：仓库里（本文件在 tools/，代码在 ../src/）与安装后平铺（同级）。
_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parent / "src", _HERE.parent):
    if (_candidate / "anp_preflight.py").is_file() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import anp_core as core  # noqa: E402
import anp_preflight  # noqa: E402

SRC = anp_preflight.src_dir()
PY = os.environ.get("AGENT_ANP_PYTHON") or sys.executable
HUB = core.DEFAULT_HUB
HOME = core.DEFAULT_HOME
LOG = _HERE / "verify-result.txt"

# 没指定 --agents 时，可以先用 AGENT_ANP_VERIFY_AGENTS 给一个名单（可选）；
# 两者都不给就探 live 账本里实际登记的身份（见 main 的注释）。
DEFAULT_AGENTS = [a for a in
                  (os.environ.get("AGENT_ANP_VERIFY_AGENTS") or "").replace(" ", "").split(",") if a]

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    line = f"[{'PASS' if ok else 'FAIL'}] {name}{('  <- ' + detail) if detail else ''}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fp:
        fp.write(line + "\n")


def http(path: str, payload: dict | None = None, timeout: float = 30.0,
         as_identity: str = "") -> dict:
    """打 hub 的 HTTP 接口。

    `as_identity` 非空时带上那个身份的 `x-mailbox-key`：hub 现在对所有写操作强制验令牌，
    **不带密钥的直接写请求一律 401**（这是有意的，不是配置问题）。密钥取不到（比如那个身份
    还没被签发过）就不带，让 hub 自己回 401 并把原因打在明细里。
    """
    url = HUB.rstrip("/") + path
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"content-type": "application/json"}
    if as_identity:
        try:
            key = core.key_for(as_identity, HOME)
        except Exception:  # noqa: BLE001 - 取不到就不带，交给 hub 判
            key = ""
        if key:
            headers["x-mailbox-key"] = key
    req = urllib.request.Request(url, data=data, headers=headers,
                                method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


class Mcp:
    """最小 MCP stdio 客户端：只用来验证工具真的能用。"""

    def __init__(self, self_id: str):
        import subprocess

        self.proc = subprocess.Popen(
            [PY, str(SRC / "anp_mcp.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                 "AGENT_ANP_SELF": self_id, "AGENT_ANP_HUB": HUB},
        )
        self.n = 0

    def _call(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        frame = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            frame["params"] = params
        self.proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline() or "{}")

    def init(self) -> dict:
        r = self._call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()
        return r

    def tool(self, name: str, args: dict) -> tuple[str, bool]:
        r = self._call("tools/call", {"name": name, "arguments": args}).get("result") or {}
        text = "\n".join(c.get("text", "") for c in r.get("content") or [])
        return text, bool(r.get("isError"))

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def _report_and_exit() -> int:
    """汇总并给退出码。拆出来，好让「跳过」的路径也走同一套汇总。"""
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} 通过（明细见 {LOG}）======", flush=True)
    with open(LOG, "w", encoding="utf-8") as fp:
        for name, ok, detail in results:
            mark = "PASS" if ok else "FAIL"
            fp.write(f"[{mark}] {name}  {detail}" + chr(10))
    return 0 if passed == len(results) else 1

def main() -> int:
    ap = argparse.ArgumentParser(description="AgentNetPort 在线验证")
    ap.add_argument("--expect-reply", action="store_true", help="等到对方真回信（默认 240s）")
    ap.add_argument("--timeout", type=float, default=240.0)
    # ★默认 None 而不是 codex,dsh★（2026-09-26 外部审计）：旧代码用"和默认名单相等"
    # 判断"用户没指定"，于是显式写 `--agents codex,dsh` 也会被 live 名单覆盖——
    # 想验指定名单的人偏偏验不到。现在只有"没给"才自动探 live 账本。
    ap.add_argument("--agents", default=None,
                    help="要检查的身份名，逗号分隔（默认：账本里实际登记的）")
    ap.add_argument("--wake", action="store_true",
                    help="验证信真唤醒对方（默认**不唤醒**：按缓存信发）。"
                         "为什么要这个默认值：唤醒会在收信人的会话里插一脚——实测连跑几次验证，"
                         "收信方就被反复叫醒、信封堆在对话里（主人会看到一堆「在线验证」）。"
                         "只有确实要验证唤醒链路（含 MCP 注入）时才加 --wake。")
    args = ap.parse_args()
    want = [a for a in (args.agents or "").replace(" ", "").split(",") if a]
    # ★第一轮 Core Extraction★：新 home 是干净的，那四家还没迁进来。
    # 所以默认改成"验**实际登记在账本里的**身份"（--agents 仍然可以显式指定）。
    # 验在场的人，而不是验想象中的名单 —— 否则新 home 上永远红一片，看着像 Core 坏了。
    if not want:
        want = list(DEFAULT_AGENTS)
    if not want:
        try:
            live = [a.get("name") for a in (http("/agents", timeout=8).get("agents") or [])
                    if a.get("name") and a.get("name") != "hub"]
            want = live
        except Exception as e:  # noqa: BLE001 - 账本读不到就明说，别假装有身份
            print(f"读不到 /agents 名册：{type(e).__name__}: {e}", file=sys.stderr)
            want = []
    if not want:
        print("hub 账本里还没有任何身份（新 home 还没接线；"
              "要验指定名单用 --agents a,b）", file=sys.stderr)
        return 2
    agents = want
    sender, receiver = agents[0], agents[-1]

    LOG.unlink(missing_ok=True)
    print(f"hub={HUB}  home={HOME}", flush=True)

    # 1. hub + 账本
    try:
        health = http("/health", timeout=4)
        check("hub 存活", bool(health.get("ok")), json.dumps(health, ensure_ascii=False)[:120])
    except Exception as e:  # noqa: BLE001
        check("hub 存活", False, f"{type(e).__name__}: {e}（先跑 install.py）")
        health = {}

    table = core.agent_table(HOME)
    for who in agents:
        entry = table.get(who) or {}
        wake = entry.get("wake") or {}
        # 两种档位都算"登记了"：http 档看 url，exec 档看 command（没有 url 是正常的）
        if isinstance(wake, dict):
            desc = wake.get("url") or (f"exec: {' '.join(map(str, wake.get('command') or []))}"
                                       if wake.get("command") else "")
        else:
            desc = str(wake)
        check(f"{who} 已登记唤醒端点", bool(desc),
              desc or "（没有登记：对应端还没启动过插件/没跑过 MCP/没跑过 register）")

    # 2/3. 各侧 MCP 工具。
    # ★第一轮 Core Extraction★：MCP server 是 adapter，不在 Core 仓库。
    # 不在场就跳过并记账——绝不假失败，也绝不假装测过。
    mcp_here = (Path(__file__).resolve().parents[1] / "src" / "anp_mcp.py").exists()
    if not mcp_here:
        print("[SKIP] 各侧 MCP 可用（MCP adapter 不在 Core 仓库，第二轮随 adapters/ 测）")
    for who in (agents if mcp_here else []):
        try:
            c = Mcp(who)
            info = c.init().get("result", {}).get("serverInfo", {})
            text, err = c.tool("mail_agents", {})
            check(f"{who} 侧 MCP 可用", info.get("name") == "AgentNetPort" and not err,
                  f"server={info.get('name')} / {text.replace(chr(10), ' | ')[:100]}")
            c.close()
        except Exception as e:  # noqa: BLE001
            check(f"{who} 侧 MCP 可用", False, f"{type(e).__name__}: {e}")

    # 4. 真投一封信给最后一个身份，看唤醒回执。
    # ★第一轮 Core Extraction★：新 home 是干净的，可能只登记了一两个身份（甚至只有默认插件）。
    # 少于两个身份就没有"投给别人"这回事，跳过并说明——不伪造一个成功出来。
    subject = f"[自检] 在线验证 {time.strftime('%H:%M:%S')}"
    if args.wake:
        body = (f"这是一封链路验证信（由 tools/verify.py 自动发出，不是主人的指令）。"
                f"收到后请回信给 {sender}：to=\"{sender}\"，reply_to 填本信 id，正文写一句结论即可。")
    else:
        body = ("这是一封链路验证信（**缓存信，不唤醒你**）：验落盘、令牌、信封格式。"
                "不用回信、不用处理，看到即说明链路通。")
    if len(agents) < 2:
        print(f"[SKIP] 投递 + 唤醒（账本里只有 {len(agents)} 个身份，"
              f"没有可投递的对端；接线后重跑即可）")
        return _report_and_exit()

    # ★这里以前有一个提前 return（2026-09-26 外部审计）★：上面的 SKIP 分支直接
    # `return _report_and_exit()`，于是下面"真投一封信"的整段对 <2 个身份的人
    # **永远不可达**——验证器看起来 10/12 全绿，其实投递/回信一步都没测过。
    # SKIP 只该跳过"投递"这一项，不该把整个后半段变成死代码。
    try:
        sent = http("/send", {"from": sender, "to": receiver, "subject": subject,
                              "body": body, "wake": bool(args.wake)}, as_identity=sender)
        wake = sent.get("wake") or {}
        mode = ((wake.get("results") or [{}])[0]).get("mode")
        if args.wake:
            check(f"投递 + 唤醒 {receiver}", bool(wake.get("ok")),
                  f"mode={mode} {json.dumps(wake, ensure_ascii=False)[:220]}")
        else:
            # 缓存信：本就不该唤醒；判据是"信落盘了"（未投递会体现在 /health 的 undelivered）
            check(f"投递 {receiver}（缓存信，按设计不唤醒）",
                  bool(sent.get("message", {}).get("id")),
                  f"id={sent.get('message', {}).get('id')} wake={json.dumps(wake, ensure_ascii=False)[:160]}")
        mail_id = (sent.get("message") or {}).get("id")
    except Exception as e:  # noqa: BLE001
        check(f"投递 + 唤醒 {receiver}", False, f"{type(e).__name__}: {e}")
        mail_id = None

    # 5. 等真回信
    if args.expect_reply and mail_id:
        deadline = time.time() + args.timeout
        got = None
        while time.time() < deadline:
            rows = [r for r in core.read_records(sender, HOME) if r.get("from") == receiver]
            if rows:
                got = rows[-1]
                break
            time.sleep(3)
        check(f"{receiver} 真回信给 {sender}", got is not None,
              (f"{got.get('id')}: {str(got.get('body'))[:120]}" if got else f"{args.timeout:.0f}s 内没等到"))

    return _report_and_exit()


if __name__ == "__main__":
    raise SystemExit(main())
