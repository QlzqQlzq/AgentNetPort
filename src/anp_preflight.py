"""仓库根定位。零依赖，任何模块都能安全 import。

为什么要它（2026-09-21 目录重构）：
    以前每个入口都自己算"仓库根"：`Path(__file__).parent` 或 `.parent.parent`。
    代码一旦从根目录挪进 `src/`，这些算法全部失效（工作区会算成 `src/` 的上一级之外，
    或者 `chen.ps1` 找到不存在的位置）。
    现在统一成"从本文件向上找仓库根"，以后无论代码放多深都对。

判定方式（按优先级）：
    1. 设了 `AGENT_ANP_REPO` → 用它；
    2. 向上找第一个同时含 `MAILBOX-PROTOCOL.md` 与 `mailbox_agents.json` 的目录（仓库布局）；
    3. 向上找第一个含**包标记**（`MANIFEST.json` / `PACKAGE-NOTES.md` / `mailbox_agents.example.json`）
       的目录 —— **解包后的平铺安装**靠这组认出来，包里有清单但没有 mailbox_agents.json；
    4. 向上找第一个含 `AGENT_ANP_ROOT` 标记文件的目录（便于嵌入别处）；
    5. 都没有 → 返回本文件的爷爷目录（`src/` 的上一级）当兜底。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 新仓库的根标记：AgentNetPort 不再有接线表（那是 adapter 的东西），所以改用
# 「协议文档 + 默认插件目录」这一组；src/ 自己的标记也一并换名。
_MARKERS = ("ANP-PROTOCOL.md", "plugins")
# 平铺安装的标记：命中任一即认定"这里是根"。少了这组，刚解包还没建表的目录会被判成
# "不是根"，逐级上溯失败后落到 `_self_dir().parent`，报错就指向安装目录的**上一级**
# （用户从没碰过的目录，照 PACKAGE-NOTES 建表的人会被带偏）。
_PACKAGE_MARKERS = ("MANIFEST.json", "PACKAGE-NOTES.md")
_FLAG = "AGENT_ANP_ROOT"


def _self_dir() -> Path:
    """本文件所在目录（仓库里是 `src/`，平铺安装时就是安装目录）。"""
    return Path(__file__).resolve().parent


def bootstrap() -> Path:
    """把本文件所在目录放进 `sys.path`，让兄弟模块 import 得到。

    工具脚本（`tools/*.py`）与安装后的平铺布局都要先调它，再 `import anp_preflight`：
    多一层 `src/` 时插 `src/`，平铺时插脚本自己所在目录，一处代码两边都对。
    """
    here = _self_dir()
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    return repo_root()


def repo_root(start: Path | str | None = None) -> Path:
    """返回仓库根目录（绝对路径，不做存在性保证）。"""
    env = os.environ.get("AGENT_ANP_REPO")
    if env:
        return Path(env).expanduser().resolve()

    here = Path(start).resolve() if start else _self_dir()
    for base in (here if here.is_dir() else here.parent, *here.parents):
        try:
            if all((base / m).exists() for m in _MARKERS):
                return base
            if any((base / m).exists() for m in _PACKAGE_MARKERS):
                return base
            if (base / _FLAG).exists():
                return base
        except OSError:
            continue
    # 兜底：<repo>/src/anp_preflight.py → 爷爷目录
    return _self_dir().parent


def src_dir() -> Path:
    """`src/` 目录（模块所在地）。"""
    return _self_dir()


def ensure_importable() -> Path:
    """把 `src/` 放进 sys.path，返回仓库根。供根目录入口壳与工具脚本用。"""
    return bootstrap()
