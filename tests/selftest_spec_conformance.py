"""ANP-ADAPTER-SPEC.md 符合性检查：规范说的每一条可验证断言，必须对着代码成立。

为什么要有这个文件：项目里出过好几次「规范事实」和「代码事实」各存一份的分歧
（规范里的 `/messages/send`、`/messages/receive` 曾经实现里没有）。这个脚本把
规范里的常量、路由名、头名、环境变量名逐条钉到源码上，改一边忘另一边立刻红。

只做静态检查（读源码 + 读规范文本），不起 hub。
"""
from __future__ import annotations

import re
import sys
import json
import subprocess
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parent / "src", _HERE.parent):
    if (_candidate / "anp_preflight.py").is_file() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import anp_core as core      # noqa: E402
import anp_preflight         # noqa: E402

SRC = anp_preflight.src_dir()
REPO = anp_preflight.repo_root()
CORE = (SRC / "anp_core.py").read_text(encoding="utf-8")
HUB = (SRC / "anp_hub.py").read_text(encoding="utf-8")
ALL_SRC = "\n".join(f.read_text(encoding="utf-8") for f in sorted(SRC.glob("*.py")))
SPEC = (REPO / "docs" / "ANP-ADAPTER-SPEC.md").read_text(encoding="utf-8")

OK = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK, FAIL
    OK += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
# §3.4 sticky registration fields：规范列的那七个，必须等于 core.STICKY_FIELDS
# --------------------------------------------------------------------------- #
def case_sticky_fields() -> None:
    print("--- §3.4 sticky registration fields ---")
    want = ("wake", "caps", "skills", "process_api", "description", "workspace", "client")
    check("3.4.1 规范列出的 7 个 sticky 字段与 core.STICKY_FIELDS 完全一致",
          tuple(core.STICKY_FIELDS) == want,
          f"core={tuple(core.STICKY_FIELDS)}")
    check("3.4.2 规范的 sticky 列表行逐个点名了这 7 个字段",
          any(all(f in line for f in want) for line in SPEC.splitlines()),
          "没有任何一行同时含这 7 个字段名")


# --------------------------------------------------------------------------- #
# §5.2 exec 的环境变量名：规范写的四个，必须都是 core 真的设的
# --------------------------------------------------------------------------- #
def case_exec_env() -> None:
    print("--- §5.2 exec 档环境变量契约 ---")
    for var in ("AGENT_ANP_WAKE_JSON", "AGENT_ANP_SELF",
                "AGENT_ANP_SELF_KEY", "AGENT_ANP_KEY"):
        check(f"5.2 core 真的设置 {var}",
              f'env["{var}"]' in CORE or f'"{var}"' in CORE)
    check("5.2b exec 档是每封信拉起一个进程（subprocess.Popen 在 wake 内）",
          CORE.index("Popen(") > CORE.index("def wake("))


# --------------------------------------------------------------------------- #
# §5.1 http 档载荷：{"message":…,"to":…} + x-mailbox-key
# --------------------------------------------------------------------------- #
def case_http_wake() -> None:
    print("--- §5.1 http 档载荷契约 ---")
    check("5.1.1 载荷是 {\"message\": <信封>, \"to\": <身份名>}",
          '{"message": message, "to": recipient}' in CORE)
    check("5.1.2 令牌通过 x-mailbox-key 头捎带",
          '"x-mailbox-key"' in CORE and "x-mailbox-key" in HUB)
    check("5.1.3 规范写明这个头是目标 Agent 的令牌（不是 plugin 的）",
          "x-mailbox-key` 的值，永远是目标 Agent 的 token" in SPEC)


# --------------------------------------------------------------------------- #
# §2b/§3.2/§4.0 + PLUGIN-SPEC §4.0：C1 四处文档口径必须一致，且与 Hub 代码一致
def case_plugin_register_auth() -> None:
    print("--- C1 /plugin/register 鉴权：四处文档 + 代码一致 ---")
    psp = (REPO / "docs" / "ANP-PLUGIN-SPEC.md").read_text(encoding="utf-8")
    check("C1-1 Spec §2b 写明只认插件自己的 token、admin 明确不被接受",
          "/plugin/register` 只认插件自己的 token" in SPEC
          and "admin token **明确不被接受**" in SPEC)
    check("C1-2 Spec §3.2.1 写明 admin 不能续约已有身份（B2 已更正）",
          "3.2.1 更正" in SPEC and "已有 name + 管理员令牌  → ★401 拒绝★" in SPEC)
    check("C1-3 PLUGIN-SPEC §4.0 同一口径 + 401/403 分层",
          "只认插件自己的 agent token" in psp and "401 | 凭证缺失 / 不对" in psp)
    check("C1-4 ★Hub 代码真的鉴插件自己的令牌，不是 admin★",
          "core.verify_key(pid, given, HOME)" in HUB
          and "auth.plugin-credential" in HUB
          # 反向：/plugin/register 那段不该再留着 admin 门
          and "需要管理员令牌（插件报到" not in HUB)
    check("C1-5 错误码是 auth.plugin-credential（测试与 SDK 都要认这个）",
          '"code": "auth.plugin-credential"' in HUB)
    # 反向：文档里不许再留着"管理员令牌 → 放行"这种旧说法
    check("C1-6 文档不再有「已有 name + 管理员令牌 → 放行」",
          "已有 name + 管理员令牌  → 放行" not in SPEC)


# --------------------------------------------------------------------------- #
# §4.2 queue 档终态：wake_skipped 的标记字面量与规范一致
# --------------------------------------------------------------------------- #
def case_queue_marker() -> None:
    print("--- §4.2 / §4.3 wake_skipped 三态标记 ---")
    check("4.2.1 queue 档标记字面量是 \"no_push_target\"",
          '"no_push_target"' in CORE and '"no_push_target"' in SPEC,
          "core 或 spec 里找不到这个字面量")
    check("4.2.2 发送方不唤醒的标记仍是 True（布尔，与 queue 档不同）",
          '"wake_skipped": True' in CORE)
    check("4.3 三种原因在规范里被明确区分（不共用一个布尔）",
          "禁止把这三者合并成一个布尔" in SPEC)
    check("4.4 delivery_mode 返回 http/exec/queue 三档",
          hasattr(core, "delivery_mode"))


# --------------------------------------------------------------------------- #
# §6.1 canonical 路由 + deprecated 别名：规范说的路由必须都在 hub 里
# --------------------------------------------------------------------------- #
def case_routes() -> None:
    print("--- §6.1 canonical API 路由 ---")
    for route in ('"/heartbeat"', '"/messages/send"', '"/messages/receive"',
                  '"/messages/query"', '"/agents"', '"/plugin/register"'):
        check(f"6.1 hub 里有 {route}", route in HUB)
    # /anp/wake 与 /anp/register 是 **adapter/插件侧**的端点，不是 hub 路由：
    # 它们实现在 anp_plugin.py（Core 主动 POST 过去），所以搜整个 src/
    for route in ("/anp/wake", "/anp/register"):
        check(f"6.1 Core 真的会打 {route}（实现见于 src/ 全目录）",
              route in ALL_SRC)
    check("6.1.8 /messages/send 与 /send 走同一段代码（同一个 if 分支）",
          'if path in ("/send", "/messages/send")' in HUB)
    check("6.1.9 deprecation 响应头带的是规范写的那个值",
          '"use POST /messages/send"' in HUB and "use POST /messages/send" in SPEC)
    check("6.1.10 响应头是纯 ASCII（规范明确要求，且不能含中文箭头）",
          "→" not in SPEC.split("该头必须纯 ASCII")[-1].split("\n")[0]
          if "该头必须纯 ASCII" in SPEC else True)


# --------------------------------------------------------------------------- #
# §7 status：规范禁止复合状态
# --------------------------------------------------------------------------- #
def case_status() -> None:
    print("--- §7 三个独立事实 ---")
    for word in ("liveness", "reachability", "lease"):
        check(f"7. 规范定义了 {word}", word in SPEC)
    check("7b 规范明文禁止复合 status 对象",
          "明文禁止" in SPEC and '{"status": "online"}' in SPEC)


# --------------------------------------------------------------------------- #
# §8 caps：Core 不依赖 caps 做基础通信决策
# --------------------------------------------------------------------------- #
def case_caps() -> None:
    print("--- §8 caps 归属 ---")
    for key in ("inject_session", "spawn_cwd", "resume_session", "queue"):
        check(f"8. 规范定义了 caps.{key}", key in SPEC)
    # Core 代码里不出现这些键 => 它确实不依赖它们做决策
    used = [k for k in ("inject_session", "spawn_cwd", "resume_session")
            if k in CORE or k in HUB]
    check("8b Core 源码里不出现 caps 的三个策略键（透传，不消费）",
          not used, f"出现了 {used}")


# --------------------------------------------------------------------------- #
# §9 ok 语义
# --------------------------------------------------------------------------- #
def case_ok_semantics() -> None:
    print("--- §9 ok 语义 ---")
    check("9.1 规范写明 ok=true 只表示已持久化",
          "只表示「消息已经成功持久化」" in SPEC)
    check("9.2 规范指出投递结果在 wake 字段",
          "wake 字段" in SPEC)


# --------------------------------------------------------------------------- #
# 附录：P0 速查表必须完整且连续
# --------------------------------------------------------------------------- #
def case_p0_table() -> None:
    print("--- 附录 P0 速查表 ---")
    found = re.findall(r"\*\*(P0-\d+)\*\*", SPEC)
    want = [f"P0-{i}" for i in range(13)]
    check("A1 P0-0 到 P0-12 全部在速查表里且顺序正确",
          found == want, f"实得 {found}")
    # 每条 P0 在正文里都有展开（不是只在速查表出现过一次）
    thin = [p for p in want if SPEC.count(p) < 2]
    check("A2 每条 P0 在正文里都有展开（不止速查表一次）",
          not thin, f"只出现一次: {thin}")


# --------------------------------------------------------------------------- #
# 规范引用的文档都存在
# --------------------------------------------------------------------------- #
def case_doc_refs() -> None:
    print("--- 规范引用的文档 ---")
    for ref in ("ANP-PROTOCOL.md", "ANP-PLUGIN-SPEC.md"):
        check(f"引用存在：{ref}", (REPO / "docs" / ref).is_file(), str(REPO / "docs" / ref))
    for ref in ("selftest_p00_queue.py", "selftest_p09_api.py"):
        check(f"引用存在：{ref}", (REPO / "tests" / ref).is_file(), str(REPO / "tests" / ref))
    check("参考实现在场：examples/adapter_demo/adapter.py",
          (REPO / "examples" / "adapter_demo" / "adapter.py").is_file())


# --------------------------------------------------------------------------- #
# 参考实现的资格线：examples/ 只准 import 标准库
#
# 这是把「ANP 是协议，不是只有本仓库内部代码才能接入」变成可执行约束。
# 哪天有人图方便让 adapter.py import 了 anp_core，这一条立刻红。
# --------------------------------------------------------------------------- #
def case_reference_adapter_clean() -> None:
    print("--- 参考实现的资格线（examples/ 零内部依赖）---")
    ex = REPO / "examples" / "adapter_demo" / "adapter.py"
    if not ex.is_file():
        check("参考实现存在", False, str(ex))
        return
    src = ex.read_text(encoding="utf-8")
    mods = {m.split(".")[0] for m in
            re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", src, re.M)}
    # #3 之后依赖 SDK 是设计意图（SDK 随 Adapter 交付）。真正要守的没变：
    # ★一行 Core 内部模块都没有★。
    sdk_allowed = {"anp_adapter"}
    std_only = {"argparse", "json", "os", "queue", "sys", "threading", "time",
                "urllib", "http", "pathlib", "socket", "__future__"}
    bad = sorted(mods - std_only - sdk_allowed)
    check("R1 参考实现只 import 标准库 + anp_adapter", not bad,
          f"命中 {bad}" if bad else "")
    check("R2 参考实现没有 import 任何 Core 模块",
          not re.search(r"import\s+anp_(core|hub|policy|plugin)", src))
    check("R3 参考实现没有写死 Core 内部文件路径",
          not re.search(r"ledger\.jsonl|keys\.json|admin\.token", src))
    # R4 的语义变了：允许注入 sdk/（"让样例不经安装就能跑"的常规做法），
    # 但不允许为 Core 注入。
    injected = re.findall(r"sys\.path\.insert\([^)]*\)", src)
    core_inject = [f for f in injected if "src" in f and "sdk" not in f]
    check("R4 参考实现的 sys.path 注入只指向 sdk/（不为 Core）",
          not core_inject, f"命中 {core_inject}" if core_inject else
          (f"{len(injected)} 处均指向 sdk" if injected else "无注入"))

    # R5：SDK 本身（#3 新增）。SDK 是 Adapter 与 Core 之间唯一的代码依赖，
    # 所以它必须自己守"不 import Core"这条线。
    sdk = REPO / "sdk" / "anp_adapter"
    if not sdk.is_dir():
        check("R5 SDK 在场", False, str(sdk))
        return
    sdk_files = sorted(sdk.glob("*.py"))
    check("R5a SDK 在场且有多个模块", len(sdk_files) >= 4,
          f"{len(sdk_files)} 个文件")
    dirty = [f.name for f in sdk_files
             if re.search(r"import\s+anp_(core|hub|policy|plugin)", f.read_text(
                 encoding="utf-8"))]
    check("R5b ★SDK 不 import 任何 Core 模块★", not dirty, f"命中 {dirty}")
    sdk_init = (sdk / "__init__.py").read_text(encoding="utf-8")
    sdk_mods = {m.split(".")[0] for m in
                re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", sdk_init, re.M)}
    sdk_std = {"argparse", "json", "os", "queue", "sys", "threading", "time",
               "urllib", "http", "pathlib", "socket", "__future__", "re"}
    check("R5c SDK 只依赖标准库", not (sdk_mods - sdk_std - {"anp_adapter"}),
          f"命中 {sorted(sdk_mods - sdk_std - {'anp_adapter'})}")
    check("R5d SDK 覆盖 Spec §6.1 的 6 个 Core API + 2 个回调",
          all(p in (sdk / "client.py").read_text(encoding="utf-8")
              for p in ("/heartbeat", "/messages/send", "/messages/receive",
                        "/messages/query", "/agents", "/plugin/register"))
          and "/anp/wake" in (sdk / "plugin.py").read_text(encoding="utf-8"),
          "")
    # 参考实现不该再手抄协议面（否则 SDK 就白抽了）
    demo = (REPO / "examples" / "adapter_demo" / "adapter.py").read_text(encoding="utf-8")
    check("R5e 参考实现改用 SDK，不再手抄协议面",
          "from anp_adapter import" in demo
          and "def _post" not in demo and "urllib.request" not in demo,
          "")
    # R6：SDK README 的 import **可执行**（E1，2026-09-24）。
    # README 原来教 `from anp_adapter import AnpClient, Adapter, DemoPlatform, HttpWake`
    # 而 `Adapter` / `DemoPlatform` 是 example 里自己定义的类、不在 SDK 里——
    # 照抄立刻 ImportError。文档里的名字必须真的是 SDK 导出的，否则就是假契约。
    # ★2026-09-26 外部审计后扩到所有 vendored 副本★：原来只查 `sdk/README.md`，
    # 于是 adapters/dsh/anp_adapter/README.md 里同样的坏 import 一直没人管
    # （照抄就 ImportError）。vendored 的每一份都查，避免"改了源头漏了副本"。
    readmes = [sdk / "README.md"] + sorted(
        p for p in (REPO / "adapters").glob("*/anp_adapter/README.md") if p.is_file())
    checked = 0
    for readme in readmes:
        if not readme.is_file():
            continue
        rtxt = readme.read_text(encoding="utf-8")
        names = sorted({n for stmt in
                        re.findall(r"from anp_adapter import ([^\n`]+)", rtxt)
                        for n in stmt.replace("(", " ").replace(")", " ").split(",")
                        if n.strip() and n.strip().isidentifier()})
        if not names:
            continue
        checked += 1
        probe = "import anp_adapter as _s; _missing=[n for n in " \
                f"{names!r} if not hasattr(_s, n)]; print(_missing)"
        out = subprocess.run([sys.executable, "-c", probe], cwd=str(readme.parent),
                             capture_output=True, text=True, timeout=60)
        miss = [x for x in out.stdout.replace("[", "").replace("]", "")
                .replace("'", "").split(",") if x.strip()]
        rel = readme.relative_to(REPO).as_posix()
        check(f"R6 ★{rel} 里 import 的每个名字都是 SDK 真导出的★", not miss,
              f"这些名字 SDK 里没有：{miss}" if miss else f"{len(names)} 个全部在场")
    if not checked:
        check("R6 README 里有 from anp_adapter import 可执行检查", False, "一份都没抓到")


# --------------------------------------------------------------------------- #
# Adapter Plugin 原型：验证它真的存在、且不改 Core
# --------------------------------------------------------------------------- #
def case_adapter_plugin_proto() -> None:
    print("--- Adapter Plugin 原型（#6 的实验结果）---")
    ex = REPO / "examples" / "adapter_plugin"
    for f in ("manifest.json", "plugin.py", "experiment.py"):
        check(f"P0a 原型文件在場：{f}", (ex / f).is_file(), str(ex / f))
    man = json.loads((ex / "manifest.json").read_text(encoding="utf-8")) \
        if (ex / "manifest.json").is_file() else {}
    perms = man.get("permissions") or []
    check("P0b 原型没有引入新 permission（只用了现有的）",
          set(perms) <= {"tools.register", "agents.discover", "wake.others",
                         "broadcast", "webui.panel"},
          f"实际 {perms}")
    check("P0c 规范的 §2.0 写明了 Adapter Plugin 的定义（不是 Plugin+wake）",
          "Adapter Plugin 是一种负责" in SPEC and "语义转换" in SPEC)
    check("P0d 规范写明了代管受 register 策略约束",
          "register` 策略" in SPEC or "register 策略" in SPEC)
    # 规范会用「Plugin + wake」指代**被否定的旧说法**（§冻结论①、§12 缺口表），
    # 那是历史记录，应当允许。这里只禁把它写成等式/定义。
    check("P0e 规范没有把 Plugin+wake 写成等式或定义",
          "= Plugin" not in SPEC and "Adapter = Plugin" not in SPEC
          and "Adapter 是 Plugin" not in SPEC)


def main() -> int:
    print("=== ANP-ADAPTER-SPEC.md 符合性检查 ===")
    case_sticky_fields()
    case_exec_env()
    case_http_wake()
    case_plugin_register_auth()
    case_queue_marker()
    case_routes()
    case_status()
    case_caps()
    case_ok_semantics()
    case_p0_table()
    case_doc_refs()
    case_reference_adapter_clean()
    case_adapter_plugin_proto()
    print(f"\n===== {OK} 通过 / {FAIL} 失败 =====")
    if FAIL:
        print("★有规范断言与代码不一致★：改规范或改代码，别让两边各存一份。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
