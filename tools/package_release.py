#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""做/验 ANP 发行包（确定性 ZIP：MANIFEST + SHA256SUMS，可重复、可核对）。

★为什么重写★（2026-09-26 外部审计）：`tools/package_release.py` 在仓库里只剩一个
`__pycache__/package_release.cpython-313.pyc`，源码没跟过来——"生成发行包"这一步
命令直接退出 2。旧版是**平铺**的老信箱布局（src/tools/tests/docs → 同一个目录），
而现在的仓库是 `src/ + adapters/ + plugins/ + webui/ + sdk/ + examples/ + skills/`，
Adapters 与 WebUI 恰恰是这次的交付重点，旧逻辑一个都不覆盖。所以按新布局重写，
保留旧版的三个要点：**确定性**（同源码同字节）、**可验证**（manifest + sha256）、
**边界可验证**（排除项写死，打包后反过来查一遍）。

用法：
  python tools/package_release.py build                     # → dist/AgentNetPort-<版本>.zip
  python tools/package_release.py build -o out.zip
  python tools/package_release.py verify dist/xxx.zip        # 核对清单与排除项
  python tools/package_release.py stale dist/xxx.zip         # 哪些入包文件比包新

排除的不是"可有可无"，而是**机器本地状态与构建残留**：密钥、账本、收件箱、
data/、日志、.pyc、测试输出。它们进了包 = 把 runtime 状态当成交付物，
也让人无法验证"这个包里到底有什么"。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 入包的顶层项（**白名单**：不在这个表里的顶层内容一律不进包，
# 这样"发布边界"是显式的，新增顶层目录时有意识地决定要不要发布）。
# `VERSION` 也跟着走：版本号只有这一个真出处，收包方应当能读到它。
# `.gitignore` 刻意不入包——它是仓库卫生文件，不是交付物。
INCLUDE_TOP = (
    "README.md", "VERSION", "src", "tools", "tests", "docs", "sdk",
    "adapters", "plugins", "webui", "examples", "skills",
)

# 目录名排除（任何层级）
EXCLUDE_DIRS = frozenset({
    "__pycache__", "data", "inbox", "journals", "logs", "dist", "release",
    ".git", "node_modules", "webui-profile", "verify", "webui/dev",
})

# 文件名/后缀排除
EXCLUDE_NAMES = frozenset({
    "admin.token", "hub.secret", "keys.json", "plugins.json", "sessions.json",
    "permissions.json", "verify-result.txt", "regression-run.txt",
    "package_release.cpython-313.pyc",
})
# 前缀排除：设计参考图（webui/assets/webui-ref-*.png，4.5 MB×3）不是运行时资源，
# 随包发只会把发行包撑大四倍；开发期留在仓库里，不进包。
EXCLUDE_PREFIXES = ("ledger.", ".ledger.", "hub.log", "webui-ref-")
EXCLUDE_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".pyd", ".zip", ".log", ".token", ".secret",
    ".compact-bak", ".tmp", ".lock",
})

# 包内固定时间戳（确定性：同源码 → 同字节）
ZIP_DATE = (2026, 1, 1, 0, 0, 0)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _excluded(path: Path, source: Path = ROOT) -> bool:
    """这个文件不该进包吗？（排除项对 `source` 的相对路径判定）"""
    try:
        rel = path.relative_to(source)
    except ValueError:
        return True
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    name = path.name
    if name in EXCLUDE_NAMES or name.startswith(EXCLUDE_PREFIXES):
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def source_files(source: Path = ROOT) -> list[Path]:
    """入包文件（相对 `source` 排序）。白名单 + 排除项，两头都显式。"""
    src = Path(source)
    out: list[Path] = []
    for top in INCLUDE_TOP:
        p = src / top
        if p.is_file():
            if not _excluded(p, src):
                out.append(p)
            continue
        if not p.is_dir():
            continue
        for f in sorted(p.rglob("*")):
            if f.is_file() and not _excluded(f, src):
                out.append(f)
    return sorted(set(out), key=lambda p: p.relative_to(src).as_posix())


def package_version(source: Path = ROOT) -> str:
    """版本号：仓库根的 `VERSION` 文件（有就用），否则退回 Core 的 API 代次。

    ★为什么读文件而不是在代码里再写一份常量★：版本只有一个真相处。
    现在仓库里唯一权威的代次是 `src/anp_core.py` 的 `CORE_API_LEVEL`，而发布名
    需要人能读的版本号，所以约定：要发版就在仓库根写一个 `VERSION`（一行一个号）。
    """
    vf = Path(source) / "VERSION"
    try:
        text = vf.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return text.splitlines()[0].strip()
    try:
        core_text = (Path(source) / "src" / "anp_core.py").read_text(encoding="utf-8")
        for line in core_text.splitlines():
            if line.startswith("CORE_API_LEVEL"):
                return f"0.{line.split('=', 1)[1].strip()}.0"
    except OSError:
        pass
    return "0.0.0"


def manifest_for(source: Path, files: list[Path]) -> dict:
    stamps = {}
    entries = []
    for f in files:
        rel = f.relative_to(source).as_posix()
        data = f.read_bytes()
        entries.append({"path": rel, "size": len(data), "sha256": sha256_bytes(data)})
        try:
            stamps[rel] = int(f.stat().st_mtime)
        except OSError:
            stamps[rel] = 0
    return {
        "name": "AgentNetPort",
        "version": package_version(source),
        "generated_from": ".",
        "generated_at_note": "包内时间戳固定为 ZIP_DATE，字节可复现；源码新旧看 source_stamps",
        "file_count": len(entries),
        "files": entries,
        "source_stamps": stamps,
    }


def _zip_info(name: str) -> zipfile.ZipInfo:
    return zipfile.ZipInfo(name, date_time=ZIP_DATE)


def build(output: str, source: Path = ROOT) -> int:
    src = Path(source)
    files = source_files(src)
    if not files:
        print(f"no release files found under {src}", file=sys.stderr)
        return 2
    man = manifest_for(src, files)
    # 摊平前的最后一道：同一成员名出现两次 = 静默丢文件，这里拦死
    seen: set[str] = set()
    dup = sorted({e["path"] for e in man["files"] if e["path"] in seen or seen.add(e["path"])})
    if dup:
        print("duplicate member names: " + ", ".join(dup), file=sys.stderr)
        return 2

    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in man["files"]:
            zf.writestr(_zip_info(f"AgentNetPort/{entry['path']}"),
                        (src / entry["path"]).read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr(_zip_info("AgentNetPort/MANIFEST.json"),
                    json.dumps(man, ensure_ascii=False, indent=2) + "\n")
        zf.writestr(_zip_info("AgentNetPort/PACKAGE-NOTES.md"), notes_bytes())
        zf.writestr(_zip_info("AgentNetPort/SHA256SUMS.txt"),
                    "".join(f"{e['sha256']}  {e['path']}\n" for e in man["files"]))
    os.replace(tmp, out)
    print(f"built {out}  ({len(man['files'])} files, "
          f"{out.stat().st_size} bytes, version={man['version']})")
    return 0


def notes_bytes() -> str:
    return (
        "# AgentNetPort package\n\n"
        "内容：信箱本体（src/）、工具（tools/）、自测（tests/）、文档（docs/）、\n"
        "Adapter SDK（sdk/）、随包 Adapters（adapters/）、默认插件（plugins/）、\n"
        "管理看板（webui/）、参考实现（examples/）与技能（skills/）。\n\n"
        "**不含**机器本地状态：admin.token / hub.secret / keys.json / 账本 / 收件箱 /\n"
        "data/ / 日志 / *.pyc。这些由目标机器首次运行时自己生成。\n\n"
        "启动：`python src/anp_hub.py --home ~/.agentnetport`（默认端口 8900）。\n"
        "校验：`python tools/package_release.py verify <这个 zip>`。\n"
    )


def _read_manifest(zf: zipfile.ZipFile) -> dict:
    raw = zf.read("AgentNetPort/MANIFEST.json").decode("utf-8")
    return json.loads(raw)


def verify(archive_path: str) -> int:
    p = Path(archive_path)
    if not p.is_file():
        print(f"no such archive: {p}", file=sys.stderr)
        return 2
    problems: list[str] = []
    with zipfile.ZipFile(p) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        bad_names = [n for n in names if n.startswith("/") or ".." in n.split("/")]
        if bad_names:
            problems.append(f"unsafe member path: {bad_names[:3]}")
        # ① 机器本地状态/构建残留不许进包
        leaked = [n for n in names
                  if any(part in EXCLUDE_DIRS for part in n.split("/"))
                  or n.rsplit("/", 1)[-1] in EXCLUDE_NAMES
                  or n.rsplit("/", 1)[-1].lower().endswith(tuple(EXCLUDE_SUFFIXES))
                  or n.rsplit("/", 1)[-1].startswith(EXCLUDE_PREFIXES)]
        if leaked:
            problems.append(f"包里有排除项（不该发布的内容）: {sorted(set(leaked))[:5]}")
        try:
            man = _read_manifest(zf)
        except KeyError:
            print("archive has no MANIFEST.json", file=sys.stderr)
            return 2
        expected = {e["path"]: e for e in (man.get("files") or []) if isinstance(e, dict)}
        if len(expected) != len(man.get("files") or []):
            problems.append("MANIFEST.files 里有非对象条目")
        present = {n[len("AgentNetPort/"):] for n in names if n.startswith("AgentNetPort/")}
        present.discard("MANIFEST.json")
        present.discard("PACKAGE-NOTES.md")
        present.discard("SHA256SUMS.txt")
        if present != set(expected):
            missing = sorted(set(expected) - present)
            extra = sorted(present - set(expected))
            problems.append(f"manifest set mismatch; missing={missing[:5]} extra={extra[:5]}")
        for rel, entry in sorted(expected.items()):
            try:
                data = zf.read(f"AgentNetPort/{rel}")
            except KeyError:
                problems.append(f"manifest member missing: {rel}")
                continue
            if len(data) != int(entry.get("size") or -1):
                problems.append(f"size mismatch: {rel}")
            elif sha256_bytes(data) != entry.get("sha256"):
                problems.append(f"sha256 mismatch: {rel}")
        if "AgentNetPort/README.md" not in zf.namelist():
            problems.append("README.md is missing")
    if problems:
        print("VERIFY FAILED:")
        for x in problems[:20]:
            print("  -", x)
        return 1
    print(f"OK: {p} 与清单一致（{len(expected)} 个文件，version={man.get('version')}），"
          f"且不含密钥/账本/缓存")
    return 0


def stale(archive_path: str, source: Path = ROOT) -> int:
    """哪些入包文件比这个包还新（"包落后于源码"必须看得见）。"""
    with zipfile.ZipFile(Path(archive_path)) as zf:
        stamps = _read_manifest(zf).get("source_stamps") or {}
    behind = []
    for rel, mtime in sorted(stamps.items()):
        f = Path(source) / rel
        if not f.is_file():
            behind.append(f"{rel} (missing)")
        elif int(f.stat().st_mtime) > int(mtime):
            behind.append(f"{rel} (source newer)")
    if behind:
        print(f"stale: {len(behind)} 个入包文件比包新")
        for x in behind[:20]:
            print("  -", x)
        return 1
    print(f"fresh: {len(stamps)} 个入包文件都不比包新")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="build/verify AgentNetPort release ZIP")
    ap.add_argument("action", choices=["build", "verify", "stale"])
    ap.add_argument("path", nargs="?", default="", help="verify/stale 的 zip；build 的输出")
    ap.add_argument("-o", "--output", default="", help="build 输出路径（默认 dist/ 下）")
    ap.add_argument("--source", default=str(ROOT), help="源码根（默认本仓库）")
    args = ap.parse_args(argv)
    if args.action == "build":
        out = args.output or args.path or str(
            ROOT / "dist" / f"AgentNetPort-{package_version(Path(args.source))}.zip")
        return build(out, Path(args.source))
    if not args.path:
        print(f"{args.action} 需要一个 zip 路径", file=sys.stderr)
        return 2
    if args.action == "verify":
        return verify(args.path)
    return stale(args.path, Path(args.source))


if __name__ == "__main__":
    raise SystemExit(main())
