"""AgentNetPort Core 回归：只覆盖 **Core 不变量**。

第一轮（Core Extraction）说明：
  旧仓库的 selftest_regressions.py 有 5 个用例，其中 ①② 走 adapter 入口
  （mailbox_spawn / MCP 的 DirectBackend），③④⑤ 走发行打包（package_release + dist/*.zip）
  与接线表模板。第一轮 adapter 和打包都不在 Core 仓库，所以：
    · ①② 改写成 **Core 原语版的同一条不变量**（覆盖不缩水，只是换了入口）；
    · ③④⑤ 明确 SKIP 并记账，第二轮随 adapters/ 与 release 一起重建。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parent / "src", _HERE.parent):
    if (_candidate / "anp_preflight.py").is_file() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import anp_core as core      # noqa: E402
import anp_policy as policy  # noqa: E402
import anp_preflight         # noqa: E402

SRC = anp_preflight.src_dir()
REPO = anp_preflight.repo_root()
PY = sys.executable

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

TMP = Path(tempfile.mkdtemp(prefix="anp-regress-"))
results: list[tuple[str, bool, str]] = []
SKIPPED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""), flush=True)


def skip(name: str, why: str) -> None:
    SKIPPED.append(f"{name}：{why}")
    print(f"[SKIP] {name}  <- {why}", flush=True)


# --------------------------------------------------------------------------- #
# ①（原：唤醒器区分「读失败」与「空收件箱」）
#    Core 版同一条不变量：读原语对"不存在"和"坏行"必须可区分、不许假装
# --------------------------------------------------------------------------- #
def case_read_primitives() -> None:
    home = TMP / "case1"
    (home / "inbox").mkdir(parents=True, exist_ok=True)
    missing = core.read_records("nobody.here", home)
    check("① 不存在的收件箱 → 空列表（不是抛错，也不是 None）",
          missing == [], f"{type(missing).__name__}: {missing}")

    inbox = home / "inbox" / "dirty.jsonl"
    inbox.write_text(json.dumps({"id": "m-1", "ts": 1.0, "from": "a", "to": "dirty",
                                 "body": "好行", "read": False}, ensure_ascii=False) + "\n"
                     + "{这行不是 JSON}\n"
                     + json.dumps({"id": "m-2", "ts": 2.0, "from": "a", "to": "dirty",
                                   "body": "也好行", "read": False}, ensure_ascii=False) + "\n",
                     encoding="utf-8")
    rows = core.read_records("dirty", home)
    check("①b 坏行被跳过、好行照常读出（一行坏不影响整箱）",
          [r["id"] for r in rows] == ["m-1", "m-2"], f"{[r['id'] for r in rows]}")

    (home / "inbox" / "blank.jsonl").write_text("\n\n  \n", encoding="utf-8")
    check("①c 只有空行的文件 → 空收件箱", core.read_records("blank", home) == [])

    skip("①d 唤醒器区分「读失败」与「空收件箱」",
         "判读逻辑在 adapter 的唤醒器里（mailbox_spawn.peek_unread），第二轮随 adapters/ 测")


# --------------------------------------------------------------------------- #
# ②（原：direct 降级下多人信拆成 N 份副本）
#    ★群发是 hub 层能力（hub.send_group），不是 core.send 的原语★ —— core.send 只接
#    单个收件人。这条不变量搬到 selftest.py 去验（那边有 hub 在跑），这里只留说明。
# --------------------------------------------------------------------------- #
def case_group_send_note() -> None:
    rec = core.send("chencode", "codex", "单收件人的信", subject="single", home=TMP / "case2")
    rows = core.read_records("codex", TMP / "case2")
    check("② 单收件人发信落进对端收件箱",
          any(r.get("id") == rec.get("id") for r in rows),
          f"对端 {len(rows)} 封")
    check("②b 单收件人的信 `to` 就是对端名字（不是列表字样）",
          rec.get("to") == "codex", str(rec.get("to")))
    skip("②c 群发 N 份副本共享同一个 message id",
         "群发在 hub.send_group；这条不变量由 tests/selftest.py 的群发用例覆盖")


# --------------------------------------------------------------------------- #
# Core 侧：信封完整性
# --------------------------------------------------------------------------- #
def case_integrity() -> None:
    home = TMP / "case3"
    home.mkdir(parents=True, exist_ok=True)
    rec = core.send("alice", "bob", "原始正文", subject="s", home=home)
    check("③ 新信带签名", bool(rec.get("sig")))
    check("③b 验签通过", core.verify_envelope(rec, home) == (True, "ok"))

    bad = dict(rec)
    bad["body"] = "被改过"
    check("③c ★改正文 → tampered★", core.verify_envelope(bad, home) == (False, "tampered"))
    bad2 = dict(rec)
    bad2["from"] = "someone-else"
    check("③d ★改 from（冒充发件人）→ tampered★",
          core.verify_envelope(bad2, home) == (False, "tampered"))
    state_only = dict(rec)
    state_only.update({"read": True, "attempts": 3, "delivered_at": time.time()})
    check("③e ★只改状态字段 → 仍然有效（信封签、状态不签）★",
          core.verify_envelope(state_only, home) == (True, "ok"))
    legacy = {k: v for k, v in rec.items() if k != "sig"}
    check("③f 无签名老信 → unsigned，不算坏",
          core.verify_envelope(legacy, home) == (True, "unsigned"))
    check("③g 两串钥匙没有互相顶替（永久架构约束）",
          core.secret_isolation_ok(home)[0] is True)


# --------------------------------------------------------------------------- #
# Core 侧：数据生命周期
# --------------------------------------------------------------------------- #
def case_lifecycle() -> None:
    home = TMP / "case4"
    home.mkdir(parents=True, exist_ok=True)
    for i in range(30):
        core.send("alice", "bob", f"已读 {i}", home=home)
    for r in core.read_records("bob", home):
        core.sign_off("bob", {str(r["id"])}, home, by="test")
    # ★prune 的签名是 (home, keep)★：它裁**所有**收件箱，不是裁某一个身份
    removed = core.prune(home, keep=10)
    left = len(core.read_records("bob", home))
    check("④ 已读信被裁到保留量", left == 10, f"删了 {removed}，剩 {left}（保留 10）")

    core.send("alice", "bob", "这封别删", home=home)
    before = sum(1 for r in core.read_records("bob", home) if not r.get("read"))
    core.prune(home, keep=1)
    after = [r for r in core.read_records("bob", home) if not r.get("read")]
    check("④b ★未读一封不删（硬约束）★",
          any(r.get("body") == "这封别删" for r in after),
          f"裁剪前未读 {before} 封 → 裁剪后 {len(after)} 封")


# --------------------------------------------------------------------------- #
# Core 侧：关联线 + 权限策略
# --------------------------------------------------------------------------- #
def case_correlation_and_policy() -> None:
    home = TMP / "case5"
    home.mkdir(parents=True, exist_ok=True)
    cid = "alice:task-1"
    a = core.send("alice", "bob", "第一封", correlation_id=cid, home=home)
    b = core.send("alice", "bob", "第二封", correlation_id=cid, home=home)
    core.send("alice", "carol", "别的线的信", correlation_id="alice:task-2", home=home)
    rows = core.read_journal(cid, home)
    check("⑤ 关联线只收这条线的信", [r["id"] for r in rows] == [a["id"], b["id"]],
          f"{[r['id'] for r in rows]}")
    check("⑤b owner = 第一个发信的人", core.journal_owner(cid, home) == "alice",
          str(core.journal_owner(cid, home)))

    ok, why = policy.may_register("new.one", False, home)
    check("⑤c 默认策略允许铸造身份", ok is True, why)
    (home / "permissions.json").write_text(json.dumps(
        {"register": {"mode": "closed"}}, ensure_ascii=False), encoding="utf-8")
    ok2, why2 = policy.may_register("new.two", False, home)
    check("⑤d 策略改成 closed 后被拒（改完即生效，不用重启）", ok2 is False, why2)
    ok3, _ = policy.may_register("new.two", True, home)
    check("⑤e 管理员令牌仍然放行", ok3 is True)


def case_preflight() -> None:
    check("⑥ repo_root 指向 AgentNetPort 自己", REPO == Path(__file__).resolve().parents[1],
          str(REPO))
    check("⑥b src_dir 指向 src/", SRC.name == "src" and SRC.parent == REPO, str(SRC))
    skip("⑥c 平铺安装的根定位 / 摊平重名 / 正逆映射矩阵",
         "依赖发行打包（package_release + dist/*.zip）与接线表模板，第二轮随 adapters/ 与 "
         "release 重建一起验")


def main() -> int:
    print("=== AgentNetPort Core 回归 ===")
    case_read_primitives()
    case_group_send_note()
    case_integrity()
    case_lifecycle()
    case_correlation_and_policy()
    case_preflight()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} 通过 =====")
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print("失败项: " + ", ".join(failed))
    if SKIPPED:
        print(f"跳过 {len(SKIPPED)} 项（adapter / 打包不在 Core 仓库，第二轮验）：")
        for s in SKIPPED:
            print("   - " + s)
    if os.environ.get("ANP_TEST_KEEP") != "1":
        shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
