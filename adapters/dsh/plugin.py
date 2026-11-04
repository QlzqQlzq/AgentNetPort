"""DSH Adapter Plugin —— **ANP 侧**。

一个 Adapter Plugin 代管 ANP 身份 `dsh`：Core 拉起本插件 → POST `/anp/register`
送来身份与令牌 → 本插件用**公开** `/heartbeat` 代 `dsh` 注册，wake 指向本插件
自己的 `/anp/wake` → 别的 Agent 给 `dsh` 发信 → Core 把完整信封推到 `/anp/wake`
→ 本插件翻译、递 DSH、回收回信。

## 两个身份，两枚令牌（P0-1，绝不互换）

```
anp.adapter.dsh   ← 本插件自己（Core 在 /anp/register 发给它的）
dsh               ← 被代管的 DSH Agent 身份（本插件通过公开 /heartbeat 铸造）
```

## 四个必须职责（Spec §1）

  ① 托管身份 —— `register_agent()` 走公开 `POST /heartbeat`
  ② 实现唤醒 —— `PluginServer` 的 `/anp/wake`
  ③ 语义翻译 —— `platform.to_native` / `platform.from_native`
  ④ 回收回信 —— `reply()` 发回 Core，带 `reply_to` + `correlation_id`

## 禁止做（越界即 bug，Spec §1）

  ✗ 做投递决策　　✗ 读别人的收件箱 / keys.json / ledger.jsonl
  ✗ 持有 hub signing secret　　✗ 代替 Core 签收
  ✗ 用自己身份冒充 Agent　　✗ import Core 的 src/

★本文件不 import 任何 Core 模块★——与 Core 之间只有 HTTP。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _find_sdk() -> Path | None:
    """定位 ANP Adapter SDK。

    ★部署纪律★：Adapter Plugin 由 Core 从 `<home>/plugins/<id>/` 拉起，
    那时"仓库根的 ../sdk"根本不在路径里。SDK 必须**随插件一起交付**
    （vendor 进插件目录，或装进站点目录），否则插件起来就 `ModuleNotFoundError` 秒退，
    而 Core 只会报「插件端点不可达」——非常难查。

    按优先级找：
      1. `<插件目录>/anp_adapter`             —— vendor 进来的（★部署形态，本 Adapter 用这个★）
      2. `<AGENT_ANP_HOME>/sdk/anp_adapter`   —— 装在 ANP home 下
      3. `<仓库>/sdk/anp_adapter`             —— 在仓库里原地跑（开发形态）
    """
    home = os.environ.get("AGENT_ANP_HOME") or ""
    for cand in (_HERE / "anp_adapter",
                 Path(home) / "sdk" / "anp_adapter" if home else None,
                 _HERE.parent.parent / "sdk" / "anp_adapter"):
        if cand is not None and (cand / "__init__.py").is_file():
            return cand.parent
    return None


_sdk = _find_sdk()
if _sdk is None:
    raise SystemExit(
        "找不到 ANP Adapter SDK。Adapter Plugin 必须把 sdk/anp_adapter/ vendor 进"
        "自己的目录（部署形态），或装到 AGENT_ANP_HOME/sdk/ 下。")
if str(_sdk) not in sys.path:
    sys.path.insert(0, str(_sdk))

import importlib.util as _ilu                       # noqa: E402

from anp_adapter import (AnpClient, Envelope, PluginServer,   # noqa: E402
                         DEFAULT_RESPONSE_MODE)


def _load_platform():
    """加载同目录的 `platform.py`（DSH 平台语义）。

    ★不能写 `import platform`★——那是 Python 标准库的同名模块，插件目录在 sys.path
    最前面时会把标准库顶掉，进而让 `urllib` 之类的导入炸掉。这里显式按文件加载，
    只取我们自己的那一个。
    """
    spec = _ilu.spec_from_file_location("dsh_platform", _HERE / "platform.py")
    if spec is None or spec.loader is None:  # pragma: no cover - 文件必在
        raise SystemExit("找不到 platform.py（DSH 平台语义层）")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dsh = _load_platform()

# --------------------------------------------------------------------------- #
# 本 Adapter 的常量
# --------------------------------------------------------------------------- #
MANAGED_AGENT = "dsh"                    # ★被代管的 Agent 身份名（一经注册不得更改）★
PLUGIN_ID = os.environ.get("ANP_PLUGIN_ID") or "anp.adapter.dsh"

# DSH 作为 ANP Agent 的自我介绍（Spec §3.3：description / skills / caps 都是自由词表，
# Core 只存只返回、**不解释**）。平台语义不塞进 Core。
AGENT_DECLARATION = {
    "description": "DeepSeek Harness（DSH）——本机异构 Agent 的会话宿主；经 ANP Adapter 接入。",
    "skills": ["coding", "shell", "file-edit", "web", "review"],
    "caps": {
        # ★caps 是 adapter 的能力广告，Core 不依赖它做基础通信决策（Spec §8）★
        # ★广告必须与实现相符★（复审 Medium 项）：本 Adapter 目前把信**落成待办**
        # （`data/dsh-pending.jsonl`），由 DSH 侧来取——**没有**把信推进某个
        # 运行中会话的通道，所以 inject_session / resume_session 如实报 False。
        # 报 True 会让上层会话规划器按"已注入"来规划，而实际语义是"等它自己取"。
        "inject_session": False,
        "spawn_cwd": False,
        "resume_session": False,
        "queue": True,              # 底线档：无论如何都保留"我自己来取"
    },
    "client": "dsh-adapter",
}


def agent_declaration_with_api() -> dict:
    """注册声明 + process_api（提供可达性提示，Hub 只透传、WebUI 负责探测）。"""
    port = os.environ.get("ANP_PLUGIN_PORT") or "0"
    decl = dict(AGENT_DECLARATION)
    decl["process_api"] = {
        "kind": "http",
        "url": f"http://127.0.0.1:{port}/anp/health",
        "timeout_sec": 1.5,
    }
    return decl

_log_lines: list[str] = []


def log(msg: str) -> None:
    """插件自己的日志。★只写插件目录，不碰 <home> 下任何信箱文件★。"""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    _log_lines.append(line)
    print(line, flush=True)


def fingerprint(token: str) -> str:
    """令牌的短指纹（sha256 前 16 位）。只用于**比对**，不可反推。"""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:16] if token else ""


class DshAdapter:
    """代管 ANP 身份 `dsh` 的 Adapter Plugin。"""

    def __init__(self) -> None:
        self.client: AnpClient | None = None    # 插件自己那枚令牌对应的 client
        self.agent_key = ""                     # ★代管的 agent 那枚，与上面不是同一枚★
        self.register_state: dict = {}
        self.book = dsh.SessionBook()
        self.stats = {"wakes": 0, "delivered": 0, "replied": 0,
                      "reply_failed": 0, "rejected_key": 0,
                      "consumer_claimed": 0, "consumer_replied": 0,
                      "consumer_failed": 0, "consumer_skipped": 0}
        self.consumer_state = {
            "enabled": False, "status": "waiting-for-registration",
            "started_at": None, "current": None, "last_error": None,
            "last_finished_at": None,
        }
        self._consumer_started_at = 0.0
        self._consumer_thread: threading.Thread | None = None
        self._consumer_stop = threading.Event()

    # ------------------------------------------------------------------ #
    # ① 托管身份：公开 POST /heartbeat
    # ------------------------------------------------------------------ #
    def wake_declaration(self) -> dict:
        """本插件给代管 agent 登记的唤醒方式。

        ★这是整个接入里最关键的一处形状★：Adapter Plugin 自己就在
        `ANP_PLUGIN_PORT` 上听着 `/anp/wake`，代管的 agent 直接指过去。

        为什么**不能**用 exec：真实部署下 exec 唤醒引用的脚本要过 Core 的信任校验
        （只认信箱安装目录），而插件按设计装在 `<home>/plugins/<id>/`，永远在外面——
        一律被拒。Plugin 形态的正解是 http。

        端口每次由 Core 随机分配（且通过环境变量给），所以这里**必须现读环境变量**，
        不能写死。
        """
        port = os.environ.get("ANP_PLUGIN_PORT") or "0"
        return {"kind": "http", "url": f"http://127.0.0.1:{port}/anp/wake"}

    def register_agent(self, client: AnpClient, retry: int = 20) -> dict:
        """用公开 `/heartbeat` 代 `dsh` 注册；wake 指向本插件的 /anp/wake。

        首次调用 = bootstrap（不需要密钥，响应里发令牌）；之后每次续约都必须带上
        那枚令牌，否则被 P0-2 的 401 挡掉。

        ★重启恢复（必测第 ⑦ 项）★：Core 每次拉起插件都分配**随机新端口**，
        所以插件重启后必须带**旧令牌**续约，才能把 wake 端点更新到新端口。
        令牌从 `platform.load_credentials()` 读回——不然会以"新身份"bootstrap，
        撞 401，**wake 永远停在死掉的旧端口上**（实测踩到，症状是
        "注册成功但信永远投不进来"）。
        """
        wake = self.wake_declaration()
        # 先试着从盘上恢复上次那枚令牌（进程重启后 self.agent_key 是空的）
        saved_key = False
        if not self.agent_key:
            saved = dsh.load_credentials()
            if saved.get("agent_key") and saved.get("manage_agent") == MANAGED_AGENT:
                self.agent_key = saved["agent_key"]
                saved_key = True
                log(f"从 disk 恢复 {MANAGED_AGENT} 的令牌"
                    f"（fp={fingerprint(self.agent_key)}），准备续约到新端口")
        last: dict = {}
        for i in range(retry):
            token = self.agent_key or ""       # 首轮为空 = bootstrap
            r = client.heartbeat(MANAGED_AGENT, token=token, wake=wake,
                                 **agent_declaration_with_api())
            last = r
            if r.get("ok"):
                self.agent_key = str(r.get("key") or self.agent_key)
                dsh.save_credentials(agent_key=self.agent_key,
                                     plugin_id=PLUGIN_ID,
                                     manage_agent=MANAGED_AGENT)
                self.register_state = {
                    "ok": True, "attempt": i + 1, "auth": r.get("auth"),
                    "wake": wake, "recovered_from_disk": bool(saved_key),
                    "agent_key_fp": fingerprint(self.agent_key),
                    "plugin_key_fp": fingerprint(client.token),
                    # ★自证 P0-1★：两枚令牌必须不同
                    "two_tokens_distinct": fingerprint(self.agent_key) !=
                                           fingerprint(client.token),
                }
                log(f"代管 {MANAGED_AGENT} 注册{'恢复' if saved_key else '成功'}"
                    f"（auth={r.get('auth')}, attempt={i + 1}），wake={wake['url']}")
                return r
            # 401 = 身份已存在而我们没有它的令牌。这一次是致命的：
            # 重试不会变，必须把它报出来（P0-2 不允许绕过）。
            if i == 0 and token == "":
                log(f"代管 {MANAGED_AGENT} 首次注册未通过：{r} "
                    f"（若为 401，说明身份已存在且本插件没有它的令牌）")
            time.sleep(0.3)
        self.register_state = {"ok": False, "last": last,
                               "hint": "若为 401：该身份已存在且本插件没有它的令牌；"
                                       "需清掉旧身份后重新铸造，不能绕过 P0-2"}
        log(f"代管 {MANAGED_AGENT} 注册失败：{last}")
        return last

    # ------------------------------------------------------------------ #
    # Core → 插件握手
    # ------------------------------------------------------------------ #
    def on_register(self, res) -> None:
        """Core 送来插件自己的身份 / 令牌 / 配置（Spec §6.2 接口 8）。

        ★这里必须**立刻返回**，绝不能在回包之前做任何要再回调 Core 的事★

        踩到的坑（实测）：原来直接在这里同步调 `register_agent()`，
        而它会 POST Core 的 `/heartbeat`。此刻 Core 正卡在 `register_out()`
        里等本插件回 `/anp/register` 的包 —— 两端互等，叠加冷启动开销后
        冲破 Core 的 **10 秒**启动预算，Core 只报：

            插件端点不可达：POST http://127.0.0.1:<port>/anp/register —— TimeoutError

        而插件进程其实完全健康（`/anp/health` 返回 ok:true）。
        实测 `on_register` 同步做完要 **6.3 秒**，逼近预算。

        修法：握手回包与代管注册**解耦**——回包立刻做，代管注册丢到后台线程。
        `/anp/health` 会暴露 `register_error` / `agent_ready`，所以"插件起来了"
        与"代管 agent 注册成功没有"仍然分得开，不会变成最难查的那种半身不遂。
        """
        self.client = res.client()
        log(f"/anp/register 收到：identity={res.identity} "
            f"key_fp={fingerprint(res.token)} hub={res.hub_base}")
        # ★异步化★：不在握手路径上回调 Core（见 docstring 的互等分析）
        import threading
        threading.Thread(target=self._register_later, daemon=True).start()

    def _register_later(self) -> None:
        """后台完成代管身份注册，不阻塞 `/anp/register` 的回包。"""
        try:
            result = self.register_agent(self.client)
            if result.get("ok"):
                self._start_consumer()
        except Exception as e:                                    # noqa: BLE001
            self.register_state = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            log(f"代管注册线程异常：{type(e).__name__}: {e}")

    def _start_consumer(self) -> None:
        """注册完成后启动单并发消费者；启动前遗留待办不自动重放。"""
        if self._consumer_thread and self._consumer_thread.is_alive():
            return
        self._consumer_started_at = time.time()
        self.consumer_state.update({
            "enabled": True, "status": "idle",
            "started_at": self._consumer_started_at,
        })
        self._consumer_stop.clear()
        self._consumer_thread = threading.Thread(
            target=self._consumer_loop, name="dsh-auto-consumer", daemon=True)
        self._consumer_thread.start()
        log("DSH 自动消费者已启动（单并发；旧待办不自动重放）")

    def _consumer_loop(self) -> None:
        while not self._consumer_stop.wait(1.0):
            try:
                rows = dsh.read_pending(limit=500, peek=True)
                for row in rows:
                    self._consume_row(row)
            except Exception as exc:  # noqa: BLE001
                self.consumer_state.update({
                    "status": "error",
                    "last_error": f"{type(exc).__name__}: {exc}",
                })
                log(f"DSH 消费循环异常：{type(exc).__name__}: {exc}")

    def _consume_row(self, row: dict) -> None:
        eid = str(row.get("anp_id") or "").strip()
        if not eid:
            return
        state = dsh.work_state().get(eid) or {}
        status = str(state.get("status") or "")
        received_at = float(row.get("received_at") or 0)
        is_current = received_at >= self._consumer_started_at
        is_recovery = status == "running" and (
            time.time() - float(state.get("updated_at") or 0) >= 600.0)
        if not is_current and not is_recovery:
            return
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        if meta.get("need_reply") is not True:
            if status not in {"notice", "replied"}:
                dsh.finish_work(eid, "notice", detail="need_reply=false-or-absent")
                self.stats["consumer_skipped"] += 1
            return
        claimed = dsh.claim_work(
            eid, current_after=self._consumer_started_at)
        if not claimed:
            return
        self.stats["consumer_claimed"] += 1
        self.consumer_state.update({
            "status": "running", "current": eid, "last_error": None})
        try:
            text = self._run_headless(row)
            result = self.reply(
                to=str(row.get("sender") or row.get("anp_from") or ""),
                text=text,
                anp_id=eid,
                correlation_id=str(row.get("correlation_id") or ""),
                subject=str(row.get("subject") or ""),
            )
            if not result.get("ok"):
                raise RuntimeError(f"reply failed: {result}")
            dsh.finish_work(eid, "replied", detail="platform-output",
                            output_preview=text[:240],
                            reply_ok=True)
            self.stats["consumer_replied"] += 1
            self.consumer_state.update({
                "status": "idle", "current": None,
                "last_finished_at": time.time(),
            })
            log(f"DSH 平台真实产出已回信：{eid}（非 ACK）")
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            self.stats["consumer_failed"] += 1
            try:
                compensation = self.reply(
                    to=str(row.get("sender") or row.get("anp_from") or ""),
                    text=f"[DSH] 平台处理失败，未能生成正式回答。\n错误：{error}\n"
                         f"原信：{eid}",
                    anp_id=eid,
                    correlation_id=str(row.get("correlation_id") or ""),
                    subject=str(row.get("subject") or ""),
                )
            except Exception as reply_exc:  # noqa: BLE001
                compensation = {
                    "ok": False,
                    "error": f"{type(reply_exc).__name__}: {reply_exc}",
                }
            dsh.finish_work(
                eid, "failed", error=error,
                compensation_ok=bool(compensation.get("ok")),
                retry_after=time.time() + 30,
            )
            self.consumer_state.update({
                "status": "failed", "current": None,
                "last_error": error, "last_finished_at": time.time(),
            })
            log(f"DSH 平台处理失败：{eid}；补偿回信 ok={compensation.get('ok')}")

    @staticmethod
    def _resolve_node() -> str:
        configured = os.environ.get("ANP_DSH_NODE") or ""
        if configured and Path(configured).is_file():
            return configured
        found = shutil.which("node")
        if found:
            return found
        raise FileNotFoundError("Node.js not found; set ANP_DSH_NODE")

    @staticmethod
    def _dsh_cli_candidates() -> list[Path]:
        """DSH CLI 的所有候选位置，按优先级排（★平台知识，只住在本 Adapter★）。

        新版 DSH 是官方 Electron 安装版，CLI 从 npm 包的 `lib/bin.js`（要 node）
        换成了安装目录里的 `dsh.cmd`（自带 Electron 运行时，直接可执行）。
        旧版路径保留，是为了同一份 Adapter 在两种布局的机器上都能跑。
        """
        out: list[Path] = []

        def add(p: Path | None) -> None:
            if p is not None and str(p).strip() and p not in out:
                out.append(p)

        # ① 显式指定（部署侧首选入口）
        configured = (os.environ.get("ANP_DSH_CLI")
                      or os.environ.get("DSH_CLI") or "").strip()
        if configured:
            add(Path(configured))

        # ② 新版：官方安装版的 dsh.cmd（ANP_DSH_DESKTOP 或默认安装位置）
        desktop = (os.environ.get("ANP_DSH_DESKTOP")
                   or os.environ.get("DEEPSEEK_HARNESS_DESKTOP") or "").strip()
        if desktop:
            add(Path(desktop) / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd")
        local = os.environ.get("LOCALAPPDATA") or ""
        if local:
            add(Path(local) / "Programs" / "DeepSeek Harness" / "resources"
                / "runtime" / "cli" / "bin" / "dsh.cmd")

        # ③ 旧版：自建壳 + npm runtime（root/runtime/node_modules/@deepseek-ai/dsh/lib/bin.js）
        root = (os.environ.get("ANP_DSH_ROOT")
                or os.environ.get("DEEPSEEK_HARNESS_ROOT") or "").strip()
        if root:
            add(Path(root) / "runtime" / "node_modules" / "@deepseek-ai"
                / "dsh" / "lib" / "bin.js")
        add(_HERE.parents[2] / "DeepSeek-Harness" / "runtime"
            / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js")
        add(_HERE.parent.parent.parent / "DeepSeek-Harness" / "runtime"
            / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js")
        return out

    @classmethod
    def _resolve_cli(cls) -> tuple[str, bool]:
        """返回 (CLI 路径, 是否需要用 node 前置)。

        `is_js` 只对旧版 npm 包的 `.js` 为真——那会儿要靠 node 拉起。
        新版的 `dsh.cmd` 自带 Electron 运行时，直接可执行。
        """
        for candidate in cls._dsh_cli_candidates():
            if candidate.is_file():
                return str(candidate), candidate.suffix.lower() == ".js"
        found = shutil.which("dsh")
        if found:
            return found, False
        raise FileNotFoundError(
            "DSH CLI not found; set ANP_DSH_CLI (新版 dsh.cmd) "
            "or ANP_DSH_DESKTOP (安装目录) or ANP_DSH_ROOT (旧版)")

    def _run_headless(self, row: dict) -> str:
        cli, is_js = self._resolve_cli()
        prompt = (
            "你是 DSH 平台里的 ANP Agent。请处理下面收到的消息，并只输出给发信人的"
            "最终自然语言回答。不要输出 ACK、调试信息或命令日志；不要使用工具、不要读写文件，"
            "除非消息明确要求且本测试没有要求。若消息要求包含固定短语，请原样包含。\n\n"
            f"发信人：{row.get('sender') or row.get('anp_from')}\n"
            f"主题：{row.get('subject') or '(无)'}\n"
            f"消息：\n{row.get('text') or ''}"
        )
        cmd = [cli] if not is_js else [self._resolve_node(), cli]
        cmd += ["--profile",
                os.environ.get("ANP_DSH_HEADLESS_PROFILE") or "headless",
                "-"]
        # ★多行 prompt 必须走 stdin，不能走 argv★（2026-10-12 实测新版 DSH）：
        # headless 的 `task...` 是"多个词按空格 join"，多行文本当 argv 传会被压成
        # 一行（实测单行 OK、两行起就丢正文，模型收到的是没有 `消息：` 后面内容的
        # 残句，于是回"信封里没有正文"）。`-` 读 stdin 才完整保留换行。
        # `ANP_DSH_PROMPT_VIA_ARGV=1` 可强制回退到 argv（旧版 DSH 没有 `-`）。
        via_argv = (os.environ.get("ANP_DSH_PROMPT_VIA_ARGV") or "").strip() == "1"
        if via_argv:
            cmd[-1] = prompt
        env = dict(os.environ)
        env.setdefault("DSH_HOME", str(Path.home() / ".dsh"))
        cwd = str(row.get("workspace") or "").strip()
        if not cwd or not Path(cwd).is_dir():
            cwd = os.environ.get("ANP_DSH_CWD") or os.getcwd()
        timeout = float(os.environ.get("ANP_DSH_TIMEOUT") or 180)
        log(f"调用 DSH headless：{row.get('anp_id')} timeout={timeout:.0f}s "
            f"prompt={'argv' if via_argv else 'stdin'}")
        completed = subprocess.run(
            cmd, cwd=cwd, env=env,
            input=None if via_argv else prompt,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            check=False,
        )
        output = (completed.stdout or "").strip()
        if completed.returncode != 0:
            detail = (completed.stderr or output or "").strip()
            raise RuntimeError(
                f"DSH CLI exit={completed.returncode}: {detail[-500:]}")
        if not output:
            raise RuntimeError("DSH CLI returned empty final output")
        return output

    # ------------------------------------------------------------------ #
    # ② 唤醒入口 + ③ 翻译 + ④ 回收回信
    # ------------------------------------------------------------------ #
    def on_wake(self, env: Envelope, side: dict) -> dict:
        """POST /anp/wake —— 每个 adapter 唯一必须实现的回调。

        ★响应必须带合法 `mode`★（Spec §5.1.1）：只回 `{"ok": true}` Core **不签收**，
        信会保持未读并被反复重投。响应体的 `mode` 是**处理方式确认**
        （immediate/batched/session/inject/followup），**不是** delivery mode
        （http/exec/queue）——两个 mode 不是一回事。
        """
        self.stats["wakes"] += 1

        # ---- 入站鉴权（Spec §5.1 / P0-4）--------------------------------
        # 头里的值**永远是目标 Agent 的令牌**，不是本插件的、更不是 hub secret。
        # 令牌由 Core 投递时捎带，所以这里只需比对，**不需要去盘上翻 keys.json**。
        got = str(side.get("agent_key") or "")
        # ★默认拒绝★：期望键为空（代管注册尚未完成）也一律拒绝。
        #
        # 原来写成 `if self.agent_key and got != self.agent_key:` —— 期望键为空时
        # **整个条件为假**，于是任意/空 key 都会放行，落到下面回 `ok:true` +
        # `mode:session`，**Core 会当成已签收**。这把 P0-4 与"禁止代 Core 签收"
        # 同时破了（复审 CR-1）。
        #
        # 空窗期不是"还没设防"，而是"必须拒绝"：代管注册在后台线程里跑
        # （见 on_register docstring），从插件起来到注册完成之间确实存在这个窗口。
        if not self.agent_key or not got or got != self.agent_key:
            self.stats["rejected_key"] += 1
            why = ("代管身份尚未就绪（agent_key 为空）" if not self.agent_key
                   else "x-mailbox-key 为空" if not got
                   else "x-mailbox-key 与目标 Agent 令牌不符")
            log(f"拒绝唤醒：{why}（收到 {fingerprint(got) or '(空)'}，"
                f"期望 {fingerprint(self.agent_key) or '(未就绪)'}）")
            # ★拒绝时不许回 ok:true★——那会让 Core 误以为已签收，信就悄悄丢了。
            return {"ok": False, "error": why, "mode": DEFAULT_RESPONSE_MODE}

        # ---- ③ 语义翻译：ANP 信封 → DSH 载荷 ---------------------------
        native = dsh.to_native(env)
        _meta = native.get("meta") or {}
        _stamp = _meta.get("session") or {}
        stamp_mode = str(_stamp.get("mode") or _stamp.get("action") or "") if isinstance(_stamp, dict) else ""

        # ★H-6 幂等★：同一 env.id 只注入一次、只回一封回执（并发重投不双跑）
        if not dsh.mark_seen(env.id):
            return {"ok": True, "mode": DEFAULT_RESPONSE_MODE,
                    "envelope_id": env.id, "deduped": True}

        # ---- 会话 / 工作区归属（只执行 Core 的 meta.session 章，Errata S1）----
        route = self.book.route(native)

        # ---- 递平台 ----------------------------------------------------
        try:
            delivered = dsh.deliver_to_platform(native, route)
        except Exception as exc:  # noqa: BLE001
            dsh.unmark_seen(env.id)   # 失败释放认领，Core 重投才能再进
            log(f"递平台失败 {env.id}: {type(exc).__name__}: {exc}")
            return {"ok": False, "mode": DEFAULT_RESPONSE_MODE,
                    "envelope_id": env.id,
                    "error": f"deliver failed: {type(exc).__name__}: {exc}"}
        self.stats["delivered"] += 1
        dsh.queue_work(native, route)
        log(f"收到信封 {env.id} ← {env.from_}（kind={env.kind}）"
            f"→ 会话 {route['session']}（{route['reason']}）")

        # ---- ④ 回收回信（DSH 侧尚未产出时的**收信回执**）----------------
        # 真实部署里 DSH 的正式回复由 DSH 自己产出后经 `reply()` 发回；
        # 这里发的是"信已进平台"的回执，让发信方立刻拿到一条闭环证据。
        ack = self._reply_ack(env, native, route)

        # ★P0-4 的旁证必须是**真比较**，不能是常量★
        # 原来这里写死 `"key_is_agent_not_plugin": True` —— 那是自我满足的假证据：
        # 它由"插件自己声明"，验的人读它等于没验（复审 CR-2）。
        # 本项目参考实现（examples/adapter_plugin/plugin.py:74-85）的做法才对：
        # **存下投递头里那枚令牌的真指纹**，让外部核验方能拿自己算的指纹去比。
        # 走到这里 `got == self.agent_key` 已由上方的默认拒绝分支保证；
        # `agent_key` 与 `client.token`（插件自己那枚）不同则由 C1 独立核验。
        delivered_fp = fingerprint(got)
        plugin_fp = fingerprint(self.client.token if self.client else "")
        return {
            "ok": True,
            "mode": "batched",              # ← 信已落入 DSH 待办队列
            "envelope_id": env.id,
            "session": route["session"],
            "reused": route["reused"],
            "route_reason": route.get("reason") or "",
            "stamp_mode": stamp_mode,
            "delivered_to": delivered["jsonl"],
            "ack": ack,
            # 核验材料（不是自我肯定）：投递头令牌的真指纹 + 插件自己那枚的指纹
            "delivered_key_fp": delivered_fp,
            "plugin_key_fp": plugin_fp,
            "delivered_key_is_agent": delivered_fp == fingerprint(self.agent_key),
            "delivered_key_differs_from_plugin": bool(delivered_fp)
                                                 and delivered_fp != plugin_fp,
        }

    def _reply_ack(self, env: Envelope, native: dict, route: dict) -> dict:
        """把"已进平台"包成 ANP 信封发回。

        ★`reply_to` + `correlation_id` 是对话线的全部依据★——不填 = 对方那封往来断线。
        回信失败的责任**在 Adapter 侧**：Core 不会因为回信没发出去而重投
        （重投只看 ack / D1 的三事实），所以这里必须自己察觉并记账。
        """
        if self.client is None or not self.agent_key or not env.from_:
            return {"ok": False, "error": "no-client-or-sender"}
        fields = dsh.from_native({
            "text": f"[DSH] 已收到并注入会话 {route['session']}：{env.body[:80]}",
            "anp_id": env.id,
            "correlation_id": env.correlation_id,
        })
        try:
            # ★用 agent 那枚令牌发，不是插件自己那枚★（P0-1）
            r = {"ok": False}
            for attempt in range(2):
                r = self.client.as_identity(self.agent_key, MANAGED_AGENT).send(
                    to=env.from_,
                    body=fields["body"],
                    reply_to=fields["reply_to"],            # ★必须带回★
                    correlation_id=fields["correlation_id"],  # ★原样带回★
                    kind=fields["kind"],
                    wake=False,          # 回执不打扰对端；要不要唤醒是发信方的事
                )
                if r.get("ok"):
                    break
                if attempt == 0:
                    time.sleep(0.2)
            dsh.record_outbox({"at": time.time(), "to": env.from_,
                               "reply_to": fields["reply_to"],
                               "correlation_id": fields["correlation_id"],
                               "ok": bool(r.get("ok")), "wake": r.get("wake")})
            if r.get("ok"):
                self.stats["replied"] += 1
            else:
                self.stats["reply_failed"] += 1
                log(f"回信持久化失败：{r}")
            return r
        except Exception as e:                                    # noqa: BLE001
            self.stats["reply_failed"] += 1
            log(f"回信失败：{type(e).__name__}: {e}")
            dsh.record_outbox({"at": time.time(), "to": env.from_,
                               "reply_to": fields["reply_to"], "ok": False,
                               "error": f"{type(e).__name__}: {e}"})
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------ #
    # 反向链入口：DSH 侧产出的回复 → ANP 信封 → Core
    # ------------------------------------------------------------------ #
    def reply(self, *, to: str, text: str, anp_id: str = "",
              correlation_id: str = "", subject: str = "") -> dict:
        """把 DSH 侧的一段产出包成 ANP 信封发回 Core。

        这是**反向链**（DSH 会话产出 → Adapter → Envelope → Core）的唯一出口。
        """
        if self.client is None or not self.agent_key:
            return {"ok": False, "error": "尚未完成 /anp/register，无法回信"}
        fields = dsh.from_native({"text": text, "anp_id": anp_id,
                                  "correlation_id": correlation_id,
                                  "subject": subject})
        r = self.client.as_identity(self.agent_key, MANAGED_AGENT).send(
            to=to, body=fields["body"], reply_to=fields["reply_to"],
            correlation_id=fields["correlation_id"], kind=fields["kind"],
            subject=fields["subject"], wake=False)
        dsh.record_outbox({"at": time.time(), "to": to, "text": text,
                           "reply_to": fields["reply_to"],
                           "correlation_id": fields["correlation_id"],
                           "ok": bool(r.get("ok"))})
        return r

    # ------------------------------------------------------------------ #
    # 寻址（可选能力，Spec §8.5）：模糊目标 → 候选 → canonical Agent ID
    # ------------------------------------------------------------------ #
    def resolve(self, target: str, **kw):
        """把模糊目标解析成 canonical Agent ID。

        ★只调 `GET /agents`，加 Adapter 自己持有的平台提示，全在本地用字符串算法算，
        不引入任何模型★。Core 完全不知道有人在做模糊解析。

        `last_used` 来自**本 Adapter 的 DSH 会话账本**（平台记录），
        与名册的 `last_seen`（心跳活跃度）严格分开，互不代填（Spec §8.6）。
        """
        if self.client is None:
            return {"status": "DIRECTORY_UNAVAILABLE",
                    "reason": "尚未完成 /anp/register"}
        local = _make_local_directory(self.book.last_used())
        return self.client.resolve(target, hints=local.hints(), **kw)

    def discover(self) -> dict:
        """给上层用的候选视图：公开名册 + 本 Adapter 的平台提示。"""
        if self.client is None:
            return {"ok": False, "error": "尚未完成 /anp/register"}
        roster = list((self.client.agents() or {}).get("agents") or [])
        return {"ok": True, "agents": roster,
                "last_used": self.book.last_used(),
                "last_used_source": "platform"}


def _make_local_directory(last_used: dict[str, float]):
    """构造 SDK 的 LocalDirectory：**只有 Adapter 知道**的平台提示。

    ★这是平台语义唯一被允许进入寻址的地方★——DSH 的工作区标题/别名绝不放 Core。
    """
    from anp_adapter import LocalDirectory
    local = LocalDirectory()
    used = last_used or {}
    for ws, ts in used.items():
        # 工作区标题：DSH 侧的坐标，用它做用户侧解析（"<某工作区> 那个"）
        local.add(ws, aliases=[ws], workspace=ws, tags=["dsh", "workspace"],
                  last_used=float(ts))
    for name in ("dsh",):
        local.add(name, aliases=["猫娘D酱", "DeepSeek Harness", "harness"],
                  workspace="", tags=["dsh", "host"],
                  last_used=used.get(name))
    return local


class DshPluginServer(PluginServer):
    """在 SDK 的 `PluginServer` 基础上，把代管状态**周期性落盘**。

    ★"插件起来了" ≠ "代管 agent 注册成功"★——两者必须分得开，否则会变成
    「adapter 启动正常但代管的 agent 不存在」这种最难查的半身不遂。
    SDK 的 `/anp/health` 只报握手期错误；这里把代管身份、令牌指纹、wake 声明与
    统计写进 `data/dsh-status.json`，验收脚本与排障都读它。

    踩到的坑（实测，两种写法都失败过）：
      ① 另开一个端口（`port+1`）挂探针 → `ANP_PLUGIN_PORT` 与 Core 记的
         `base_url` 实测会错位一格，探针端口对不上；
      ② 覆写 `serve_forever()` 想复用 SDK 的 Handler → SDK 的 `start()` 在
         **内部**定义并绑定 Handler 类，外部覆写拿不到它，新路径仍然 404。
    落盘写法与端口、与 SDK 内部结构都无关，是这里唯一稳的形状。
    """

    def __init__(self, adapter: "DshAdapter", **kw):
        super().__init__(**kw)
        self.adapter = adapter

    def status_body(self) -> dict:
        a = self.adapter
        plugin_tok = a.client.token if a.client else ""
        return {
            "ok": True,
            "plugin_id": PLUGIN_ID,
            "managed_agent": MANAGED_AGENT,
            "port": int(os.environ.get("ANP_PLUGIN_PORT") or 0),
            # ★只暴露指纹，绝不暴露令牌本身★
            "agent_key_fp": fingerprint(a.agent_key),
            "plugin_key_fp": fingerprint(plugin_tok),
            "two_tokens_distinct": bool(a.agent_key and plugin_tok
                                        and a.agent_key != plugin_tok),
            "agent_ready": bool(a.agent_key),
            "register_state": a.register_state,
            "wake": a.wake_declaration(),
            "stats": a.stats,
            "consumer": a.consumer_state,
            "work": dsh.work_state(),
            "sessions": len(a.book.rows),
            "last_used_source": "platform",
            "register_error": getattr(self, "_register_error", None),
            "updated_at": time.time(),
            "log": _log_lines[-20:],
        }

    def dump_status(self) -> None:
        dsh.write_status(self.status_body())

    def start(self) -> int:
        port = super().start()
        self.dump_status()
        return port

    def serve_forever(self) -> None:
        """阻塞服务；同时周期刷新状态文件（Core 不管保活，我们自己记账）。"""
        import threading

        def ticker():
            while True:
                time.sleep(2.0)
                try:
                    self.dump_status()
                except Exception:                                  # noqa: BLE001
                    pass

        self._status_path = "/anp/adapter/status"
        threading.Thread(target=ticker, daemon=True).start()
        super().serve_forever()


def main() -> int:
    if not os.environ.get("ANP_PLUGIN_PORT"):
        raise SystemExit("没有 ANP_PLUGIN_PORT：本插件必须由 Core 拉起，"
                         "不要手工跑（端口是 Core 注入的唯一入口）")
    adapter = DshAdapter()
    srv = DshPluginServer(adapter, on_register=adapter.on_register,
                          on_wake=adapter.on_wake)
    log(f"DSH Adapter 启动：plugin_id={PLUGIN_ID} "
        f"port={os.environ.get('ANP_PLUGIN_PORT')} "
        f"home={os.environ.get('AGENT_ANP_HOME')}")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
