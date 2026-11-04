#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理联调/测试残留：测试身份收件箱、测试信、测试 journal、插件待办里的测试行。

默认目标是本机 `<home>`（~/.agentnetport）。不会动 credentials / keys 的「真身份」。

用法：
  python tools/purge_test_mail.py
  python tools/purge_test_mail.py --home ~/.agentnetport --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

TEST_IDENTS = {
    "tester.autoinstall", "webui.tester", "verify.sender", "webui.tester",
    "tester.auto2", "codex-verify-admin",
}
TEST_NAME_RE = re.compile(r"^(tester\.|webui\.tester$|verify\.|peer\.one$|fixprobe$|regressprobe$)", re.I)
TEST_BODY_RE = re.compile(
    r"互通信测试|联调|凭据恢复验证|wake 验证|恢复验证用|test-admin-token|"
    r"FAKE_CODEX_REPLY|【测试】|【测试\d】|probe\.(recovery|bidirectional)|"
    r"ANP →|ANP→|自动安装后的 DSH",
    re.I,
)
TEST_JOURNAL_RE = re.compile(r"^(autoinstall|webui|recovery|bidi-test)", re.I)


def is_test_message(m: dict) -> bool:
    frm = str(m.get("from") or "")
    to = str(m.get("to") or "")
    subj = str(m.get("subject") or "")
    body = str(m.get("body") or "")
    kind = str(m.get("kind") or "")
    if frm in TEST_IDENTS or to in TEST_IDENTS:
        return True
    if TEST_NAME_RE.match(frm) or TEST_NAME_RE.match(to):
        return True
    if kind.startswith("probe."):
        return True
    if TEST_BODY_RE.search(subj) or TEST_BODY_RE.search(body):
        return True
    return False


def filter_jsonl(path: Path, dry: bool) -> tuple[int, int]:
    if not path.is_file():
        return 0, 0
    rows = []
    kept = dropped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            rows.append(line)
            kept += 1
            continue
        if is_test_message(m):
            dropped += 1
        else:
            rows.append(line)
            kept += 1
    if dropped and not dry:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(r + "\n" for r in rows), encoding="utf-8")
        tmp.replace(path)
    return kept, dropped


def main() -> int:
    ap = argparse.ArgumentParser(description="清理 ANP 测试残留")
    ap.add_argument("--home", default=os.environ.get("AGENT_ANP_HOME")
                    or str(Path.home() / ".agentnetport"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    home = Path(args.home).expanduser()
    dry = args.dry_run
    print(f"home={home}  dry_run={dry}")

    # 1) 测试身份整箱删除
    inbox = home / "inbox"
    for p in sorted(inbox.glob("*.jsonl")) if inbox.is_dir() else []:
        name = p.stem
        if name in TEST_IDENTS or TEST_NAME_RE.match(name):
            print(f"  remove inbox {p.name}")
            if not dry:
                p.unlink(missing_ok=True)

    # 2) 真身份箱子里的测试信
    for p in sorted(inbox.glob("*.jsonl")) if inbox.is_dir() else []:
        kept, dropped = filter_jsonl(p, dry)
        if dropped:
            print(f"  filter {p.name}: drop {dropped} keep {kept}")

    # 3) 测试 journal
    journals = home / "journals"
    for p in sorted(journals.glob("*.jsonl")) if journals.is_dir() else []:
        if TEST_JOURNAL_RE.match(p.name) or TEST_NAME_RE.match(p.name.replace("%3A", ":")):
            print(f"  remove journal {p.name}")
            if not dry:
                p.unlink(missing_ok=True)

    # 4) 插件待办/工作队列里的测试行
    for p in (home / "plugins").glob("*/data/*") if (home / "plugins").is_dir() else []:
        if p.suffix == ".jsonl" and p.name.endswith(("-work.jsonl", "-pending.jsonl", "-seen.jsonl", "-outbox.jsonl")):
            kept, dropped = filter_jsonl(p, dry)
            if dropped:
                print(f"  filter {p.relative_to(home)}: drop {dropped} keep {kept}")

    # 5) keys.json 去掉测试身份（保留真 Agent / 插件）
    keys_path = home / "keys.json"
    if keys_path.is_file():
        keys = json.loads(keys_path.read_text(encoding="utf-8"))
        drop = [k for k in keys if k in TEST_IDENTS or TEST_NAME_RE.match(k)]
        if drop:
            print(f"  remove keys {drop}")
            if not dry:
                for k in drop:
                    keys.pop(k, None)
                keys_path.write_text(json.dumps(keys, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 6) ledger 去掉测试身份行（否则 /agents 还列着 tester.*）
    ledger = home / "ledger.jsonl"
    if ledger.is_file():
        out, drop = [], 0
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                name = str(json.loads(line).get("name") or "")
            except json.JSONDecodeError:
                out.append(line)
                continue
            if name in TEST_IDENTS or TEST_NAME_RE.match(name):
                drop += 1
                continue
            out.append(line)
        if drop:
            print(f"  filter ledger: drop {drop} keep {len(out)}")
            if not dry:
                tmp = ledger.with_suffix(".jsonl.tmp")
                tmp.write_text("".join(x + "\n" for x in out), encoding="utf-8")
                tmp.replace(ledger)

    print("done" + (" (dry-run)" if dry else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
