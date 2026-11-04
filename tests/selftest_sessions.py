"""信箱"会话大脑"自检：决策矩阵 + 名册/绑定 + 过期处理。

用法：
    python selftest_sessions.py            # 用临时 home，不碰真信箱
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

# Keep the test runner readable on Windows consoles that still default to cp936.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

_HERE = Path(__file__).resolve().parent
# 兼容两种布局：仓库里（tests/ 的兄弟 src/）与安装后平铺（同级）。
for _candidate in (_HERE, _HERE.parent / "src", _HERE.parent):
    if (_candidate / "anp_preflight.py").is_file() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import anp_sessions as S  # noqa: E402

# 测试用工作区：默认取一组**与真实机器无关**的占位路径，保证任何机器上都能跑。
# 想测特定目录就设 AGENT_ANP_TEST_WS（子目录与"别处"由它派生）。
# 曾经这里写死某台机器的真实工作区，换个环境跑就三条用例全落空。
_BASE = os.environ.get("AGENT_ANP_TEST_WS") or r"C:\workspace-root"
WS = _BASE
SUB = WS + r"\subproject"
OTHER_WS = os.environ.get("AGENT_ANP_TEST_OTHER_WS") or r"D:\elsewhere"

passed = 0
failed = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}" + (f"  → {extra}" if extra else ""))
    else:
        failed += 1
        print(f"  FAIL {label}" + (f"  → {extra}" if extra else ""))


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="mailbox-sessions-"))

    def roster(sessions, adapter: str = "dsh") -> None:
        S.update_roster(adapter, sessions, home, workspace=WS)

    def decide(meta, expect_mode: str, expect_sid: str | None = None, label: str = "") -> dict:
        d = S.decide("dsh", meta, home)
        ok = d["mode"] == expect_mode and (expect_sid is None or d["session_id"] == expect_sid)
        check(label or str(meta), ok, f"{d['mode']} {d['session_id'] or '-'} | {d['reason']}")
        return d

    print("—— 名册：根目录两个会话（main 更活跃、running）+ 子目录一个 ——")
    roster([
        {"sessionId": "session-main", "cwd": WS, "lastPromptAt": 1_789_900_000, "running": True},
        {"sessionId": "session-old", "cwd": WS, "lastPromptAt": 1_789_800_000},
        {"sessionId": "session-l2d", "cwd": SUB, "lastPromptAt": 1_789_850_000},
    ])

    print("\n① 点名 / 策略")
    decide({"session": "session-l2d"}, "reuse", "session-l2d", "点名存在的会话 → 复用")
    decide({"session": "session-ghost"}, "new", None, "点名不存在的会话 → 开新对话（不猜）")
    decide({"session_policy": "new", "to_workspace": WS}, "new", None, "policy=new → 开新对话（压过工作区匹配）")
    decide({"session_policy": "reuse", "to_workspace": OTHER_WS}, "reuse", "session-main",
           "policy=reuse 且工作区不匹配 → 取最活跃")

    print("\n② 工作区匹配（精确 → 子目录 → 上级）")
    decide({"to_workspace": WS}, "reuse", "session-main", "根目录精确匹配 → 取 running 的")
    decide({"to_workspace": SUB}, "reuse", "session-l2d", "子目录精确匹配")
    decide({"to_workspace": WS + r"\新工程"}, "reuse", "session-main", "目标在其下 → 上级回落")

    print("\n③ 对话线绑定")
    S.bind_context("ctx-1", "session-old", "dsh", home, workspace=WS)
    decide({"context_id": "ctx-1", "to_workspace": WS}, "reuse", "session-old",
           "对话线已绑定 → 复用绑定（压过工作区匹配）")
    decide({"context_id": "ctx-1", "session_policy": "new"}, "new", None,
           "但 policy=new 仍可强开新对话")

    print("\n④ 空名册 / 名册过期")
    roster([])
    decide({"to_workspace": WS}, "new", None, "空名册 + auto → 开新对话")
    decide({"session_policy": "reuse", "to_workspace": WS}, "new", None, "空名册 + reuse → 只能开新对话")

    roster([{"sessionId": "session-main", "cwd": WS, "lastPromptAt": 1_789_900_000}])
    d = S.load(home)
    d["rosters"]["dsh"]["at"] = time.time() - 10_000
    S.save(d, home)
    decide({"to_workspace": WS}, "new", None, "名册过期 → 当作看不到会话")

    print("\n⑤ 只有一个会话时的兜底")
    roster([{"sessionId": "session-only", "cwd": r"F:\无关目录"}])
    decide({"to_workspace": WS}, "reuse", "session-only", "名册仅一个会话 → 直接用它")

    print("\n⑥ 缺路由信息时不再一律开新对话（实测踩过：连来两封就开两个会话）")
    roster([
        {"sessionId": "session-busy", "cwd": WS, "lastPromptAt": 1_789_900_000, "running": True},
        {"sessionId": "session-idle", "cwd": WS, "lastPromptAt": 1_789_800_000},
    ])
    decide({}, "reuse", "session-busy", "无路由信息 + 恰好 1 个在跑 → 认作后续，继续用它")
    roster([
        {"sessionId": "session-busy-a", "cwd": WS, "running": True},
        {"sessionId": "session-busy-b", "cwd": SUB, "running": True},
    ])
    decide({}, "new", None, "无路由信息 + 2 个在跑（不够明确）→ 开新对话")
    roster([{"sessionId": "session-idle-only", "cwd": WS, "lastPromptAt": 1_789_900_000}])
    decide({}, "new", None, "无路由信息 + 没有在跑的 → 开新对话")
    decide({"session_policy": "new"}, "new", None, "但署名方仍可强开新对话（policy=new 优先）")
    roster([
        {"sessionId": "session-busy", "cwd": WS, "running": True},
        {"sessionId": "session-idle", "cwd": WS, "lastPromptAt": 1_789_800_000},
    ])
    os.environ["AGENT_ANP_NEW_WHEN_NO_ROUTE"] = "1"
    try:
        decide({}, "new", None, "逃生阀：NEW_WHEN_NO_ROUTE=1 时退回老行为")
    finally:
        os.environ.pop("AGENT_ANP_NEW_WHEN_NO_ROUTE", None)

    print("\n⑦ 落点回报与流水")
    S.bind_context("ctx-x", "session-x", "dsh", home)
    view = S.roster_view(home)
    check("绑定写入 contexts", (view["contexts"].get("ctx-x") or {}).get("sessionId") == "session-x")
    S.record_decision("dsh", "m-1", {"mode": "reuse", "session_id": "session-only", "policy": "auto"}, True, home)
    view2 = S.roster_view(home)
    check("决策流水可读", len(view2["decisions"]) >= 1,
          str(view2["decisions"][-1].get("session_id") if view2["decisions"] else None))

    print(f"\n会话大脑自检：{passed} 项通过" + (f"，{failed} 项失败 ✗" if failed else "，全绿 ✓"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
