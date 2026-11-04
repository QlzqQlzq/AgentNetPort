"""MiMo Adapter work-journal states must reflect asynchronous completion."""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[1]
ADAPTER = REPO / "adapters" / "mimo"


def load_platform():
    spec = importlib.util.spec_from_file_location(
        "mimo_platform_work_state_test", ADAPTER / "platform.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load MiMo platform")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "mimo_plugin_work_state_test", ADAPTER / "plugin.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load MiMo plugin")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_platform_journal_has_non_terminal_state() -> None:
    module = load_platform()
    with tempfile.TemporaryDirectory() as tmp:
        platform = module.MiMoPlatform(data_dir=Path(tmp))
        platform.mark_work("work-1", state="watching", ok=True, detail="accepted")
        platform.finish_work("work-1", ok=True, detail="replied")
        rows = [
            __import__("json").loads(line)
            for line in (Path(tmp) / "mimo-work.jsonl").read_text(
                encoding="utf-8").splitlines()
        ]
        assert [row["state"] for row in rows] == ["watching", "done"]
        assert rows[0]["detail"] == "accepted"
        assert rows[1]["detail"] == "replied"


def test_async_wake_does_not_finish_before_watcher() -> None:
    module = load_plugin()
    adapter = module.MimoAdapter.__new__(module.MimoAdapter)
    events: list[tuple] = []

    class FakePlatform:
        def enqueue_work(self, envelope, native):
            return envelope.id, True

        def route_from_stamp(self, envelope):
            return {}

        def should_reply(self, envelope):
            return True

        def write_pending(self, messages, tier, raw):
            return None

        def mark_work(self, pending_id, **kwargs):
            events.append(("mark", pending_id, kwargs))

        def finish_work(self, pending_id, **kwargs):
            events.append(("finish", pending_id, kwargs))

    adapter.platform = FakePlatform()
    adapter.agent_key = "managed-key"
    adapter.client = object()
    adapter.tier_now = lambda: {"tier": "A"}
    adapter._peek_unread = lambda: []
    adapter._deliver_to_platform = lambda native, messages, raw: {
        "ok": True, "tier": "A", "deferred": True, "session_id": "session-1",
    }
    adapter.to_native = lambda envelope: {"text": envelope.body}
    adapter._watch_desktop_reply = lambda *args: None

    envelope = SimpleNamespace(
        id="work-async",
        body="hello",
        raw={"id": "work-async"},
        from_="codex",
        correlation_id="work-state-test",
        meta={"need_reply": True},
        is_complete=True,
        missing=lambda: [],
    )
    result = adapter.on_wake(envelope, {"agent_key": "managed-key"})

    assert result["ok"] is True
    assert events == [
        ("mark", "work-async", {"state": "watching", "ok": True,
                                 "detail": "accepted"}),
    ]


def test_queue_only_reply_is_accepted_not_done() -> None:
    module = load_plugin()
    adapter = module.MimoAdapter.__new__(module.MimoAdapter)
    events: list[tuple] = []

    class FakePlatform:
        def enqueue_work(self, envelope, native):
            return envelope.id, True

        def route_from_stamp(self, envelope):
            return {}

        def should_reply(self, envelope):
            return True

        def write_pending(self, messages, tier, raw):
            return None

        def mark_work(self, pending_id, **kwargs):
            events.append(("mark", pending_id, kwargs))

        def finish_work(self, pending_id, **kwargs):
            events.append(("finish", pending_id, kwargs))

    adapter.platform = FakePlatform()
    adapter.agent_key = "managed-key"
    adapter.client = object()
    adapter.tier_now = lambda: {"tier": "C"}
    adapter._peek_unread = lambda: []
    adapter._deliver_to_platform = lambda native, messages, raw: {
        "ok": True, "tier": "C", "deferred": True, "reason": "queue-poll",
    }
    adapter.to_native = lambda envelope: {"text": envelope.body}

    envelope = SimpleNamespace(
        id="work-queue",
        body="hello",
        raw={"id": "work-queue"},
        from_="codex",
        correlation_id="work-state-queue-test",
        meta={"need_reply": True},
        is_complete=True,
        missing=lambda: [],
    )
    result = adapter.on_wake(envelope, {"agent_key": "managed-key"})

    assert result["ok"] is True
    assert events == [
        ("mark", "work-queue", {"state": "accepted", "ok": True,
                                 "detail": "queued-for-session"}),
    ]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
