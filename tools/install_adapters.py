#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把仓库里的 Adapter 同步安装到当前 ANP home，并加载进 Hub。

为什么要有这一步（2026-09-25）：
  Adapter 写在 `adapters/<name>/` 只是**源码**；Hub 只认
  `<home>/plugins/<id>/`。以前要手动 copy + /plugins/<id>/load，
  容易漏装——WebUI 里看起来「没有 dsh/codex」。

用法：
  python tools/install_adapters.py
  python tools/install_adapters.py --home ~/.agentnetport
  python tools/install_adapters.py --only dsh,codex
  python tools/install_adapters.py --hub http://127.0.0.1:8900 --no-load

桌面壳（webui/desktop.py）启动时会调用本脚本（--quiet）做同步。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ADAPTERS = REPO / "adapters"
SDK = REPO / "sdk" / "anp_adapter"
IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "data", "verify.py", ".git", "node_modules")

# 复用 Core 的 manifest 校验：id 字符集、command/url 二选一、permissions 闭集……
# ★为什么不自己判"id 非空"★（2026-09-26 外部审计复现）：旧代码只查 pid 非空，
# 于是 `id="..\\victim"` 也能过——`<home>/plugins/` 与目标目录**拼在一起**后
# `shutil.rmtree` 落到 home 的**兄弟目录**上（实测：目标目录被清空并被覆盖成插件内容）。
# 校验规则必须只有一份实现（就在 Core 的 anp_plugin 里），这里调它，不抄它。
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
try:
    import anp_plugin as _plugin  # noqa: PLC0415 - 只为复用校验，装不上也能手动拷文件

    _HAVE_VALIDATOR = True
except Exception:  # noqa: BLE001 - Core 不在也能拷贝（只是少一道校验）
    _plugin = None
    _HAVE_VALIDATOR = False


def default_home() -> Path:
    env = (os.environ.get("AGENT_ANP_HOME") or "").strip()
    return Path(env) if env else (Path.home() / ".agentnetport")


def default_hub() -> str:
    return (os.environ.get("AGENT_ANP_HUB_URL") or "http://127.0.0.1:8900").rstrip("/")


def admin_token(home: Path) -> str:
    env = (os.environ.get("AGENT_ANP_ADMIN_TOKEN") or "").strip()
    if env:
        return env
    p = home / "admin.token"
    try:
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def http_json(url: str, data: dict | None = None, token: str = "",
              method: str = "") -> tuple[int, dict]:
    headers = {"accept": "application/json"}
    if token:
        headers["x-anp-admin"] = token
    body = None
    if data is not None:
        headers["content-type"] = "application/json; charset=utf-8"
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        method = method or "POST"
    req = urllib.request.Request(url, data=body, headers=headers, method=method or ("POST" if body else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception as exc:  # noqa: BLE001
        return -1, {"error": f"{type(exc).__name__}: {exc}"}


def discover(errors: list[str] | None = None) -> list[tuple[str, Path, dict]]:
    """返回 (plugin_id, 源目录, manifest)，并记录发现阶段的坏 manifest。

    兼容旧调用方：不传 ``errors`` 时仍只返回有效项；安装主流程传入列表，
    这样混合有效/无效 adapter 时不会把部分成功误报成整体成功。
    """
    out = []
    if not ADAPTERS.is_dir():
        return out
    for d in sorted(ADAPTERS.iterdir()):
        if not d.is_dir():
            continue
        man_path = d / "manifest.json"
        if not man_path.is_file():
            continue
        try:
            man = json.loads(man_path.read_text(encoding="utf-8"))
        except OSError as exc:
            msg = f"{d.name}: manifest.json 读取失败（{type(exc).__name__}: {exc}）"
            if errors is not None:
                errors.append(msg)
            print(f"跳过 {msg}", file=sys.stderr, flush=True)
            continue
        except json.JSONDecodeError as exc:
            msg = f"{d.name}: manifest.json 不是合法 JSON（{exc}）"
            if errors is not None:
                errors.append(msg)
            print(f"跳过 {msg}", file=sys.stderr, flush=True)
            continue
        if not isinstance(man, dict):
            msg = f"{d.name}: manifest.json 顶层必须是对象"
            if errors is not None:
                errors.append(msg)
            print(f"跳过 {msg}", file=sys.stderr, flush=True)
            continue
        pid = str(man.get("id") or "").strip()
        if not pid:
            msg = f"{d.name}: manifest 缺少 id"
            if errors is not None:
                errors.append(msg)
            print(f"跳过 {msg}", file=sys.stderr, flush=True)
            continue
        if _HAVE_VALIDATOR:
            # ★路径穿越就挡在这里★：`id` 会变成 `<home>/plugins/<id>` 的一部分，
            # 所以它必须是"一个目录名"（Core 的 manifest 校验里就有这条）。
            try:
                _plugin.validate_manifest(man, dir=d)
            except _plugin.ManifestError as e:
                msg = f"{d.name}：manifest 不合法（{e.detail}）"
                if errors is not None:
                    errors.append(msg)
                print(f"跳过 {msg}", file=sys.stderr, flush=True)
                continue
        out.append((pid, d, man))
    return out


def stop_plugin(hub: str, pid: str, token: str, log) -> int:
    """卸载再装，避免 Windows 文件占用。返回 HTTP 状态码（-1 = 连不上）。"""
    code, body = http_json(f"{hub}/plugins/{pid}/unload", {}, token)
    log(f"  unload {pid} → {code} {body.get('error') or body.get('ok')}")
    time.sleep(0.4)
    return code


def install_one(pid: str, src: Path, home: Path, log) -> Path:
    # ★纵深防御：更要紧的那道在 discover()（复用 Core 的 manifest 校验）★。
    # 这里再挡一次，是因为本函数直接 `rmtree(home/plugins/<pid>)`：pid 只要带一个
    # 路径分隔符，删除范围就跑到 plugins/ 外面去了（2026-09-26 审计实测过）。
    bad = [c for c in str(pid) if c in "/\\"]
    if bad or str(pid) in (".", "..") or not str(pid).strip():
        raise RuntimeError(f"plugin id 不是合法目录名：{pid!r}")
    dst = home / "plugins" / pid
    dst.parent.mkdir(parents=True, exist_ok=True)
    # ★data/ 必须保留★：里面有 agent_key / 会话账本。覆盖安装时清掉会导致
    # 代管身份失联、wake 停在旧端口（2026-09-25 实测踩到）。
    saved_data: Path | None = None
    saved_env: Path | None = None
    if dst.exists():
        data_dir = dst / "data"
        if data_dir.is_dir():
            saved_data = home / "plugins" / f".{pid}.data.bak"
            if saved_data.exists():
                shutil.rmtree(saved_data, ignore_errors=True)
            shutil.copytree(data_dir, saved_data)
        # plugin.env.json 是部署侧路径配置（例如 DSH/Codex CLI 位置），
        # 覆盖源码时必须保留，否则每次同步都会悄悄丢掉运行参数。
        env_file = dst / "plugin.env.json"
        if env_file.is_file():
            saved_env = home / "plugins" / f".{pid}.env.bak"
            saved_env.write_bytes(env_file.read_bytes())
        shutil.rmtree(dst, ignore_errors=True)
        if dst.exists():
            for child in dst.iterdir():
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
            shutil.rmtree(dst, ignore_errors=True)
        if dst.exists():
            raise RuntimeError(f"无法覆盖 {dst}（文件被占用）")
    shutil.copytree(src, dst, ignore=IGNORE)
    if SDK.is_dir():
        shutil.copytree(SDK, dst / "anp_adapter", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if saved_data is not None and saved_data.is_dir():
        if (dst / "data").exists():
            shutil.rmtree(dst / "data", ignore_errors=True)
        shutil.copytree(saved_data, dst / "data")
        shutil.rmtree(saved_data, ignore_errors=True)
        log("  preserved data/")
    if saved_env is not None and saved_env.is_file():
        shutil.copy2(saved_env, dst / "plugin.env.json")
        saved_env.unlink(missing_ok=True)
        log("  preserved plugin.env.json")
    if not (dst / "manifest.json").is_file():
        raise RuntimeError(f"{dst} 缺少 manifest.json")
    return dst


def load_one(hub: str, pid: str, token: str, log) -> bool:
    code, body = http_json(f"{hub}/plugins/{pid}/load", {}, token)
    pl = body.get("plugin") or {}
    ok = code == 200 and not pl.get("load_error")
    log(f"  load {pid} → {code} registered={pl.get('registered')} "
        f"err={pl.get('load_error')} base={pl.get('base_url')}")
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同步安装 ANP Adapter 插件")
    ap.add_argument("--home", default=str(default_home()))
    ap.add_argument("--hub", default=default_hub())
    ap.add_argument("--only", default="", help="逗号分隔 plugin id / 目录名")
    ap.add_argument("--no-load", action="store_true", help="只拷贝不加载")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    home = Path(args.home).expanduser()
    hub = args.hub.rstrip("/")
    token = admin_token(home)

    wanted = {x.strip() for x in args.only.split(",") if x.strip()}
    discover_errors: list[str] = []
    items = discover(discover_errors)
    if wanted:
        items = [it for it in items if it[0] in wanted or it[1].name in wanted]
        # `--only good` intentionally scopes the operation: a broken, unrelated
        # adapter elsewhere in the repository must not make this targeted install
        # fail.  Errors from a selected directory still propagate below.
        discover_errors = [e for e in discover_errors
                           if e.split(":", 1)[0].strip() in wanted]

    log(f"home={home}")
    log(f"hub={hub}")
    log(f"adapters={len(items)}")
    if not items:
        log("（adapters/ 下没有带 manifest 的插件）")
        return 1

    installed = 0
    failed: list[str] = []          # 拷贝失败（磁盘/占用）
    not_loaded: list[str] = []      # 拷贝成功但没能加载进 Hub
    hub_reachable = True
    for pid, src, _man in items:
        log(f"\n== {pid}  ← {src.name} ==")
        if not args.no_load and token:
            try:
                if stop_plugin(hub, pid, token, log) < 0:
                    hub_reachable = False
            except Exception as exc:  # noqa: BLE001
                log(f"  unload skip: {exc}")
        try:
            dst = install_one(pid, src, home, log)
        except Exception as exc:  # noqa: BLE001
            log(f"  INSTALL FAIL: {exc}")
            failed.append(pid)
            continue
        log(f"  installed {dst}")
        installed += 1
        if args.no_load:
            continue
        if not token:
            log("  skip load（没有 admin 令牌）")
            not_loaded.append(f"{pid}(无 admin 令牌)")
            continue
        if not load_one(hub, pid, token, log):
            not_loaded.append(pid)

    if not args.no_load and token:
        code, agents = http_json(f"{hub}/agents", None, token, method="GET")
        if code < 0:
            hub_reachable = False
        names = sorted(a.get("name") for a in (agents.get("agents") or []))
        log(f"\nagents now: {names}")

    # ★退出码必须讲实话★（2026-09-26 外部审计复现）：旧逻辑只看"拷贝成功几个"，
    # 于是 Hub 连不上、没有令牌、插件加载失败，全都照样返回 0——桌面壳与 WebUI 把它
    # 显示成"同步成功"，而看板里其实什么都没有。现在：
    #   0 = 全部装上**且**（不加载 或 全部加载成功）
    #   1 = 有任何一个环节没成（明细在 stdout，别让调用方猜）
    if discover_errors:
        log(f"发现阶段失败={discover_errors}")
    if discover_errors or failed or not_loaded or (not args.no_load and not hub_reachable):
        log(f"\nDone. installed={installed}/{len(items)}"
            + (f" 拷贝失败={failed}" if failed else "")
            + (f" 未加载={not_loaded}" if not_loaded else "")
            + ("" if hub_reachable or args.no_load else " hub 不可达"))
        log("（有项目没完成：本返回码是非零，调用方请把它当失败处理）")
        return 1
    log(f"\nDone. installed={installed}/{len(items)}")
    return 0 if installed else 1


if __name__ == "__main__":
    raise SystemExit(main())
