"""ANP Adapter SDK —— 通信共性层。

定位（#3 的边界，用户 2026-09-22 定死）：

    Adapter SDK
        ├── ANP 通信共性        client.py / envelope.py / wake.py
        ├── Plugin 接入共性      plugin.py
        ├── Transport 共性       transport.py
        └── 寻址共性（可选）     resolve.py
              ↓
          Adapter 实现          平台特有语义（to_native / from_native / 会话注入…）

SDK **只做便捷层，不新增协议语义**。三条硬约束：

  ① 只 import 标准库 —— ★不 import 任何 src/ 下的 Core 模块★
     Adapter 与 Core 之间只有 HTTP 协议，没有代码依赖。
     （`tests/selftest_spec_conformance.py` 的 R1-R4 会验这一点）

  ② 不替 Adapter 做决定 —— 不决定平台语义、不决定怎么翻译、不决定会话怎么注入、
     不决定 Agent 怎么回复。那些永远留在 Adapter 实现里。

  ③ resolve.py 也不需要新的 Core API —— 它只读 `GET /agents` 这份公开名册，
     模糊匹配全在本地用普通字符串算法完成（无 AI）。第三方 Adapter 可以完全不
     支持模糊解析，跳过这个模块就是。

从哪来：`examples/adapter_demo/adapter.py` 与 `examples/adapter_plugin/plugin.py`
中已经反复出现、且属于 Adapter 共性的部分。抽取后两个 example 仍然完整通过原有验证。

`resolve.py` 的来源不同：它是 Agent Discovery / Address Resolution 适配任务
（2026-09-23）的产物——「用户说的那个谁到底是谁」属于 Adapter，Core 只认
canonical Agent ID。
"""
from __future__ import annotations

from .client import AnpClient, AnpError, post, get
from .envelope import (Envelope, from_wake_payload, RESPONSE_MODES,
                       DEFAULT_RESPONSE_MODE)
from .transport import (WakeTransport, HttpWake, ExecWake, QueueWake, free_port,
                        envelope_from_env, exec_identity, probe)
from .plugin import PluginServer, RegisterResult
from .resolve import (STATUSES, RESOLVED, AMBIGUOUS, NOT_FOUND, INVALID_TARGET,
                      DIRECTORY_UNAVAILABLE, NETWORK_ERROR, CONNECTION_FAILED,
                      ANP_OFFLINE, ANP_MAINTENANCE, RESERVED_STATUSES,
                      Candidate, Resolution, LocalDirectory, build_dir, resolve,
                      merge_hints, shape)

__all__ = [
    "AnpClient", "AnpError", "post", "get",
    "Envelope", "from_wake_payload", "RESPONSE_MODES", "DEFAULT_RESPONSE_MODE",
    "WakeTransport", "HttpWake", "ExecWake", "QueueWake", "free_port",
    "envelope_from_env", "exec_identity", "probe",
    "PluginServer", "RegisterResult",
    "STATUSES", "RESERVED_STATUSES", "RESOLVED", "AMBIGUOUS", "NOT_FOUND",
    "INVALID_TARGET", "DIRECTORY_UNAVAILABLE", "NETWORK_ERROR",
    "CONNECTION_FAILED", "ANP_OFFLINE", "ANP_MAINTENANCE",
    "Candidate", "Resolution", "LocalDirectory", "build_dir", "resolve",
    "merge_hints", "shape",
]

__version__ = "0.1.0"
