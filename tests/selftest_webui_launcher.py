"""Regression checks for desktop Hub startup routing."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "webui"))

import desktop  # noqa: E402
import anp_core as core  # noqa: E402


class WebUILauncherTests(unittest.TestCase):
    def test_documented_hub_environment_override_is_used(self) -> None:
        with patch.dict(os.environ, {"AGENT_ANP_HUB": "http://127.0.0.1:8899"},
                        clear=True):
            self.assertEqual(desktop.find_hub_url(), "http://127.0.0.1:8899")

    def test_unreachable_remote_hub_is_not_replaced_with_local_hub(self) -> None:
        with patch.object(desktop, "wait_for_hub_ready", return_value=False):
            with patch.object(core, "ensure_hub") as start:
                with patch.object(desktop, "_log"):
                    self.assertFalse(
                        desktop.ensure_hub_for_desktop("https://hub.example.test"))
                    start.assert_not_called()

    def test_existing_local_hub_is_reused(self) -> None:
        with patch.object(desktop, "wait_for_hub_ready", return_value=True):
            with patch.object(core, "ensure_hub") as start:
                self.assertTrue(desktop.ensure_hub_for_desktop("http://127.0.0.1:8900"))
                start.assert_not_called()

    def test_missing_local_hub_is_started_then_waited_for(self) -> None:
        with patch.object(desktop, "wait_for_hub_ready", side_effect=[False, True]):
            with patch.object(core, "ensure_hub", return_value={"ok": True}) as start:
                with patch.object(desktop, "_log"):
                    self.assertTrue(
                        desktop.ensure_hub_for_desktop("http://127.0.0.1:8900"))
                start.assert_called_once_with(
                    hub="http://127.0.0.1:8900",
                    home=Path.home() / ".agentnetport",
                    timeout=15.0,
                )

    def test_no_browser_mode_does_not_start_hub(self) -> None:
        with patch.object(desktop, "ensure_server", return_value="http://127.0.0.1:8998/"):
            with patch.object(desktop, "find_hub_url",
                              return_value="http://127.0.0.1:8900"):
                with patch.object(desktop, "wait_for_hub_ready", return_value=False):
                    with patch.object(desktop, "ensure_hub_for_desktop") as start:
                        with patch.object(desktop, "_log"):
                            with patch.object(desktop.time, "sleep",
                                              side_effect=KeyboardInterrupt):
                                self.assertEqual(
                                    desktop.main([
                                        "--no-browser", "--no-sync-adapters",
                                        "--port", "8998",
                                    ]),
                                    0,
                                )
                        start.assert_not_called()

    def test_pythonw_launcher_selects_console_sibling(self) -> None:
        pythonw = REPO.parent / "test-runtime" / "pythonw.exe"
        python = pythonw.with_name("python.exe")
        with patch.dict(os.environ, {"AGENT_ANP_PYTHON": str(pythonw)}):
            with patch("anp_core.Path.is_file", return_value=True):
                self.assertEqual(core._python(), str(python))


if __name__ == "__main__":
    unittest.main()
