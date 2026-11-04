# -*- coding: utf-8 -*-
"""按 README 清单**串行**跑完整回归，输出每套件的通过/失败摘要。

必须串行：审计报告已记「Chencode 与其他 adapter 验证并行时会互相移动 src/」
（多套件同时写同一份 vendor SDK → 假失败）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Windows PowerShell commonly exposes a GBK stdout stream.  The test summaries
# intentionally contain Unicode markers, so make the runner itself loss-tolerant
# instead of turning a green suite into a false non-zero run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

SUITES = [
    ("tests/selftest.py", 26),
    ("tests/selftest_sessions.py", 20),
    ("tests/selftest_regressions.py", 21),
    ("tests/selftest_plugins.py", 105),
    ("tests/selftest_p00_queue.py", 27),
    ("tests/selftest_p09_api.py", 23),
    ("tests/selftest_spec_conformance.py", 70),
    ("tests/selftest_trustroot.py", 20),
    ("tests/selftest_b1_wake_mode.py", 24),
    ("tests/selftest_d1_exec_delivery.py", 17),
    ("tests/selftest_g1_token_equality.py", 17),
    ("tests/selftest_b3_sticky_states.py", 15),
    ("tests/selftest_admin_api.py", 16),
    ("tests/selftest_webui_spa.py", 30),
    ("tests/selftest_audit_20260926.py", 70),
    ("tests/verify_webui_companion.py", 10),
    ("tests/selftest_mimo_work_state.py", 3),
    ("examples/adapter_demo/verify.py", 61),
    ("examples/adapter_plugin/experiment.py", 23),
    ("adapters/dsh/verify.py", 59),
    ("adapters/chencode/verify.py", 58),
    ("adapters/codex/verify.py", 34),
    ("adapters/mimo/verify.py", 36),
]

only = sys.argv[1:] if len(sys.argv) > 1 else None
out_path = REPO / "tools" / "regression-run.txt"
lines: list[str] = []
total_fail = 0
t_all = time.time()


def observed_pass_count(text: str) -> int | None:
    """Extract a suite's own summary count, not incidental PASS log lines."""
    candidates: list[int] = []
    patterns = (
        r"=====.*?(\d+)\s*/\s*\d+\s*通过",
        r"=====.*?(\d+)\s*(?:项\s*)?通过\s*/\s*\d+\s*失败",
        r"[:：]\s*(\d+)\s*(?:项\s*)?通过\s*/\s*\d+\s*失败",
        r"\bPASS\s+(\d+)\s+(?:FAIL|SKIP)\b",
        r"(?:自检|验收|验证)[^=\n]{0,100}?(\d+)\s*项通过",
        r"\b(\d+)\s+passed\b",
    )
    for pattern in patterns:
        candidates.extend(int(value) for value in re.findall(pattern, text, re.I))
    return max(candidates) if candidates else None


def failure_line_count(text: str) -> int:
    """Count explicit failing assertions, excluding summary text such as FAIL 0."""
    return len(re.findall(r"^\s*\[\s*FAIL\s*\]", text, re.I | re.MULTILINE))


for rel, _expected in SUITES:
    path = REPO / rel
    if only and not any(o in rel for o in only):
        continue
    if not path.is_file():
        lines.append(f"[ MISSING ] {rel}")
        total_fail += 1
        continue
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, str(path)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900,
                           cwd=str(REPO), env={**os.environ, "PYTHONUTF8": "1"})
        out = (r.stdout or "") + (r.stderr or "")
        code = r.returncode
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or "")) + " TIMEOUT"
        code = -1
    dt = time.time() - t0
    observed = observed_pass_count(out)
    n_fail = failure_line_count(out)
    count_mismatch = _expected > 0 and observed != _expected
    if count_mismatch:
        n_fail += 1
    tail = [line for line in out.splitlines() if line.strip()][-3:]
    suite_failed = code != 0 or n_fail > 0 or count_mismatch
    state = "OK " if not suite_failed else "FAIL"
    expected_note = (
        f"  count={observed}/{_expected}" if _expected > 0
        else "  count=unchecked"
    )
    lines.append(f"[{state}] {rel}  exit={code}  {dt:.0f}s  FAIL行={n_fail}{expected_note}")
    if count_mismatch:
        lines.append(f"        | summary count mismatch: expected {_expected}, observed {observed}")
    for line in tail:
        lines.append(f"        | {line[:170]}")
    if suite_failed:
        total_fail += 1
    print("\n".join(lines[-6:]), flush=True)

lines.append(f"\n===== 回归汇总：{total_fail} 个套件非零退出，总耗时 {time.time() - t_all:.0f}s =====")
out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines[-3:]))
raise SystemExit(1 if total_fail else 0)
