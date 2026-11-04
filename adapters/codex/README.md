# Codex Adapter

`anp.adapter.codex` is an ANP Adapter Plugin for the Codex CLI.

The plugin owns the `codex` Agent identity and exposes an HTTP wake endpoint on
the port assigned by Core. Each accepted wake starts `codex exec` or resumes
the session named by Core in `meta.session.session_id`. When Codex finishes,
the final `agent_message` is sent back through `POST /messages/send` with the
original `reply_to` and `correlation_id`.

## Configuration

| Variable | Meaning |
| --- | --- |
| `ANP_CODEX_AGENT` | Managed Agent name; defaults to `codex`. |
| `ANP_CODEX_BIN` | Absolute Codex executable path. |
| `ANP_CODEX_WORKSPACE` | Default workspace for new sessions. |
| `CODEX_HOME` | Codex home used to read the local session index. |
| `ANP_CODEX_TIMEOUT_SEC` | Headless run timeout; defaults to `1800`. |
| `ANP_CODEX_DANGEROUSLY_BYPASS` | Defaults to `0` (safe). Set `1` only for explicit unattended execution. |
| `ANP_CODEX_ASK_APPROVAL` | Approval policy used when bypass is disabled. |
| `ANP_CODEX_EXTRA_ARGS` | Additional arguments appended to every Codex invocation. |

The managed Agent token is stored only in the plugin's own `data/` directory.
The plugin never reads Core key or ledger files.

## Verification

```powershell
python adapters/codex/verify.py
```
