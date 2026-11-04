#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ANP WebUI 独立桌面窗口启动器。

把 `webui/` 里的管理 SPA 以**桌面窗口**跑起来，而不是浏览器标签页：

  1. 在 127.0.0.1 上起静态站（复用已有端口则不再起第二个）
  2. 桌面窗口模式下确保本机 Hub 已就绪
  3. 用本机 Edge / Chrome 的 `--app=` 应用模式弹出无地址栏窗口
  4. 独立 user-data-dir，不和日常浏览器配置混在一起

静态壳不实现业务 API；Hub 仍由 `src/anp_hub.py` 提供。Hub 伴生启动的
`--no-browser` 实例只负责静态服务，不反过来启动 Hub。

用法：
  python webui/desktop.py              # 起服务 + 开窗口（默认）
  python webui/desktop.py --no-browser # 只起服务（自测 / 无头）
  python webui/desktop.py --port 8898
  pythonw webui/desktop.py             # 无控制台（推荐做快捷方式）
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import anp_core as core  # noqa: E402

DEFAULT_PORT = int(os.environ.get("AGENT_ANP_WEBUI_PORT") or 8898)
DEFAULT_HOST = os.environ.get("AGENT_ANP_WEBUI_HOST") or "127.0.0.1"
# 窗口尺寸按邮件客户端布局给一档常用的；可用环境变量覆盖
WIN_W = int(os.environ.get("ANP_WEBUI_WIN_W") or 1280)
WIN_H = int(os.environ.get("ANP_WEBUI_WIN_H") or 820)

# The static server is intentionally local-only, but it still needs a browser-bound
# capability for the private helper endpoints.  A process-local signing key means a
# copied cookie cannot be replayed after this desktop instance is replaced.
SESSION_COOKIE = "anp-webui-session"
_SESSION_KEY = secrets.token_bytes(32)


def _log(msg: str) -> None:
    print(f"[anp-webui] {msg}", flush=True)


def _hidden_console_kwargs() -> dict:
    """让后台 console 工具真正无窗口启动（Windows 还需 SW_HIDE）。"""
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startup,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def free_port(host: str) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def alive(url: str, timeout: float = 1.2) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def who_serves(url: str) -> str:
    """问清端口上是谁。静态站返回 HTML；hub 返回 JSON service 名。"""
    try:
        with urllib.request.urlopen(url, timeout=1.2) as r:
            head = r.read(200).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    if "AgentNetPort-hub" in head or '"service"' in head:
        return "hub"
    if "<html" in head.lower() or "<!doctype" in head.lower():
        return "webui"
    return "other"


def has_autologin_endpoint(base: str, bootstrap_nonce: str = "") -> bool:
    """这个静态站是不是**本桌面壳**（带 /_anp/session 自动登录面）。

    ★历史坑★：8898 上可能是 `python -m http.server`，它只发文件、没有
    /_anp/session，也没有 no-cache。若误复用，自动登录永远失败，还会吃到旧 JS。
    """
    try:
        # The session endpoint requires the browser-bound cookie issued by the
        # static index response.  Keep the two requests in one cookie jar.
        jar = CookieJar()
        opener = urllib.request.build_opener(_NoRedirectHandler(), urllib.request.HTTPCookieProcessor(jar))
        root = base.rstrip("/") + "/"
        if bootstrap_nonce:
            root += "?anp_session=" + urllib.parse.quote(bootstrap_nonce, safe="")
        with opener.open(root, timeout=1.2) as r:
            # Detect the desktop shell without calling /_anp/session.  The latter
            # may spend several seconds resolving a token when the caller's env
            # points at a different Hub; port-reuse detection must stay cheap.
            return r.status == 200 and r.headers.get("X-AgentNetPort-WebUI") == "1"
    except Exception:  # noqa: BLE001
        return False


def find_admin_token() -> str:
    """自动登录：本地 admin 令牌（文件/环境变量，不打网络）。"""
    env = (os.environ.get("AGENT_ANP_ADMIN_TOKEN") or "").strip()
    if env:
        return env
    homes: list[Path] = []
    home_env = (os.environ.get("AGENT_ANP_HOME") or "").strip()
    if home_env:
        homes.append(Path(home_env))
    homes.append(Path.home() / ".agentnetport")
    for home in homes:
        p = home / "admin.token"
        try:
            if p.is_file():
                token = p.read_text(encoding="utf-8").strip()
                if token:
                    return token
        except OSError:
            continue
    return ""


def _admin_token_cache_path() -> Path:
    home = (os.environ.get("AGENT_ANP_HOME") or "").strip()
    base = Path(home) if home else (Path.home() / ".agentnetport")
    base.mkdir(parents=True, exist_ok=True)
    return base / "admin.token"


def _tighten_cached_secret(path: Path) -> None:
    """Keep the desktop-created token cache as private as the hub's secret files."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name != "nt":
        return
    try:
        who = subprocess.check_output(
            ["whoami"], stderr=subprocess.DEVNULL, timeout=5,
            **_hidden_console_kwargs(),
        ).decode("utf-8", "replace").strip()
        if not who:
            return
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{who}:F"],
            capture_output=True, timeout=10,
            **_hidden_console_kwargs(),
        )
        if result.returncode != 0:
            # Never leave a cache file unreadable by its owner after a failed ACL edit.
            subprocess.run(
                ["icacls", str(path), "/inheritance:e"], capture_output=True,
                timeout=10, **_hidden_console_kwargs(),
            )
    except Exception:  # noqa: BLE001
        pass


def _write_cached_secret(path: Path, value: str) -> None:
    """Create the cache with a restrictive mode before writing its contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value + "\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    _tighten_cached_secret(path)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not carry credentials/capabilities across an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirectHandler())


def _session_nonce_value(cookie_header: str | None) -> str:
    try:
        cookies = SimpleCookie()
        cookies.load(cookie_header or "")
        morsel = cookies.get(SESSION_COOKIE)
        return morsel.value if morsel else ""
    except Exception:  # noqa: BLE001
        return ""


def _new_session_nonce() -> str:
    nonce = secrets.token_urlsafe(32)
    sig = hmac.new(_SESSION_KEY, nonce.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{nonce}.{sig}"


def _bootstrap_nonce_path(host: str, port: int) -> Path:
    safe_host = str(host or "127.0.0.1").replace(":", "_").replace("/", "_")
    return Path(tempfile.gettempdir()) / f"anp-webui-{safe_host}-{int(port)}.nonce"


def _write_bootstrap_nonce(host: str, port: int, nonce: str) -> Path:
    path = _bootstrap_nonce_path(host, port)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fp:
            fp.write(str(nonce))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    _tighten_cached_secret(path)
    return path


def _valid_session_nonce(value: str) -> bool:
    try:
        nonce, sig = str(value or "").split(".", 1)
    except ValueError:
        return False
    if not nonce or not sig:
        return False
    expected = hmac.new(_SESSION_KEY, nonce.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def admin_token_works(hub: str, token: str) -> bool:
    if not token:
        return False
    # ★令牌只许发往回环 hub★（2026-09-26 外部审计复现）：`AGENT_ANP_HUB_URL` 可以指向
    # 任意远程地址，而这个函数会把**本地管理员令牌**原样放进 `x-anp-admin` 头发过去——
    # 一个错误的环境变量就足以把"能看全部正文"的钥匙送到网上去。非回环一律不发。
    if not _is_local_url(hub):
        _log(f"hub {hub} 不是回环地址：不把本地 admin 令牌发过去（若要连远程 hub，"
             f"请在那个 hub 本机上启动它自己的看板）")
        return False
    url = hub.rstrip("/") + "/admin/messages?limit=1"
    req = urllib.request.Request(
        url, headers={"x-anp-admin": token, "accept": "application/json"})
    try:
        # Do not let urllib carry x-anp-admin to a redirect target.  A local helper
        # can otherwise redirect to an external collector while still passing the
        # initial loopback check above.
        with _no_redirect_opener().open(req, timeout=2.5) as r:
            return r.status == 200 and _is_local_url(r.geturl())
    except Exception:  # noqa: BLE001
        return False


def _is_local_url(url: str) -> bool:
    """进程灯探测只许打本机，防 SSRF。"""
    try:
        u = urllib.parse.urlparse(str(url or ""))
    except Exception:  # noqa: BLE001
        return False
    if u.scheme not in ("http", "https"):
        return False
    host = (u.hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def probe_urls(urls: list[str]) -> dict[str, dict]:
    """批量探测 process_api / 健康端点（并行）。Hub 不探测，WebUI 的进程灯靠这里。"""
    out: dict[str, dict] = {}
    uniq = []
    for u in urls:
        u = str(u or "").strip()
        if u and u not in uniq:
            uniq.append(u)

    def _one(key: str) -> tuple[str, dict]:
        if not _is_local_url(key):
            return key, {"ok": False, "error": "only localhost allowed"}
        t0 = time.time()
        try:
            req = urllib.request.Request(key, headers={"accept": "application/json"})
            with _no_redirect_opener().open(req, timeout=1.2) as r:
                raw = r.read(400).decode("utf-8", "replace")
                ok = 200 <= r.status < 300
                try:
                    j = json.loads(raw or "{}")
                    if isinstance(j, dict) and "ok" in j:
                        ok = bool(j.get("ok"))
                except json.JSONDecodeError:
                    pass
                return key, {"ok": ok, "status": r.status,
                             "ms": int((time.time() - t0) * 1000)}
        except Exception as exc:  # noqa: BLE001
            return key, {"ok": False, "ms": int((time.time() - t0) * 1000),
                         "error": f"{type(exc).__name__}: {exc}"}

    # ★并行★：串行会让后面的平台健康面被前端整体超时掐掉，灯全变成 wakeable
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if uniq:
        with ThreadPoolExecutor(max_workers=min(16, len(uniq))) as ex:
            futs = [ex.submit(_one, u) for u in uniq]
            for f in as_completed(futs):
                k, v = f.result()
                out[k] = v
    return out


def resolve_admin_token(hub: str) -> str:
    """拿到**真正能过 Hub 鉴权**的 admin 令牌。

    1. 环境变量 / admin.token
    2. 用常见缺省去探 Hub（本项目 `_start_webui_hub.py` 默认 `local-admin-token`）
    3. 探到就写回 admin.token，下次不用再猜
    """
    # Never hand a local admin token to a remotely configured hub, even as a
    # fallback value for the SPA.  The browser would otherwise persist it and
    # send it in x-anp-admin on the next API call.
    if not _is_local_url(hub):
        _log(f"hub {hub} 不是回环地址：不向页面下发本地 admin 令牌")
        return ""

    tok = find_admin_token()
    if tok and admin_token_works(hub, tok):
        return tok
    candidates = []
    extra = (os.environ.get("ANP_WEBUI_ADMIN_CANDIDATES") or "").strip()
    if extra:
        candidates.extend([x.strip() for x in extra.split(",") if x.strip()])
    # 与仓库启动脚本一致的缺省；用户自己的令牌仍优先
    candidates.extend(["local-admin-token", "admin-token", "anp-admin"])
    if tok:
        candidates.insert(0, tok)
    seen: set[str] = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        if admin_token_works(hub, c):
            # 用**猜出来的缺省令牌**能进，说明这个 hub 的管理员令牌是一个公开常数：
            # 任何能连上这个端口的人都能想到它。提醒一次，不替用户改（改了会让他掉线）。
            if c != tok:
                _log("hub 的管理员令牌是常见缺省值（可被猜到）：建议把它换成一串随机值"
                     "——编辑 <home>/admin.token 后重启 hub，然后刷新本窗口")
            try:
                p = _admin_token_cache_path()
                _write_cached_secret(p, c)
                _log(f"已解析 admin 令牌并缓存到 {p}")
            except OSError as exc:
                _log(f"admin 令牌缓存失败（不影响使用）：{exc}")
            return c
    return tok  # 可能是错的/空的，交给 SPA 报错


def find_hub_url() -> str:
    """自动登录：hub 基础地址（探活本机常用口）。"""
    env = (os.environ.get("AGENT_ANP_HUB_URL")
           or os.environ.get("AGENT_ANP_HUB") or "").strip().rstrip("/")
    if env:
        return env
    for port in (8900, 8899):
        probe = f"http://127.0.0.1:{port}/health"
        try:
            with urllib.request.urlopen(probe, timeout=0.8) as r:
                body = r.read(200).decode("utf-8", "replace")
                if "AgentNetPort" in body or r.status == 200:
                    return f"http://127.0.0.1:{port}"
        except Exception:  # noqa: BLE001
            continue
    return os.environ.get("ANP_WEBUI_DEFAULT_HUB") or "http://127.0.0.1:8900"


def _hub_preload_done(hub: str) -> bool | None:
    """Return None for an unreachable/non-ANP endpoint, otherwise plugin preload state."""
    try:
        with urllib.request.urlopen(hub.rstrip("/") + "/health", timeout=1.0) as r:
            if r.status != 200:
                return None
            payload = json.loads(r.read(4096).decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            return None
        if "preload_done" not in payload:
            return True
        return bool(payload["preload_done"])
    except Exception:  # noqa: BLE001
        return None


def wait_for_hub_ready(hub: str, timeout: float = 0.0) -> bool:
    """Wait for the Hub API and its initial plugin load before syncing adapters."""
    deadline = time.time() + max(0.0, timeout)
    while True:
        if _hub_preload_done(hub) is True:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def ensure_hub_for_desktop(hub: str) -> bool:
    """Start an unavailable loopback Hub for the interactive desktop launcher only."""
    if wait_for_hub_ready(hub):
        return True
    if not _is_local_url(hub):
        _log(f"配置的 Hub 不可达且不是本机地址，不尝试启动本地 Hub：{hub}")
        return False

    home = Path(os.environ.get("AGENT_ANP_HOME") or Path.home() / ".agentnetport")
    result = core.ensure_hub(hub=hub, home=home, timeout=15.0)
    if not result.get("ok"):
        _log(f"Hub 启动失败：{result.get('error') or result.get('spawn')}; "
             f"日志={home / 'logs' / 'hub.log'}")
        return False

    if not wait_for_hub_ready(hub, timeout=45.0):
        _log(f"Hub API 已启动但尚未完成插件预加载：{hub}；"
             f"Adapter 同步跳过，日志={home / 'logs' / 'hub.log'}")
        return False
    _log(f"本机 Hub 已就绪：{hub}")
    return True


class QuietHandler(SimpleHTTPRequestHandler):
    """静态站：只听本机，日志安静一点（错误仍打到 stderr）。"""

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
    }

    def __init__(self, *args, bootstrap_nonce: str = "", **kwargs):
        self._bootstrap_nonce = str(bootstrap_nonce or "")
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 访问日志默认不刷屏；要看细节设 ANP_WEBUI_ACCESS_LOG=1
        if os.environ.get("ANP_WEBUI_ACCESS_LOG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self) -> None:
        # SPA 开发期别缓存 JS/CSS，改完刷新即生效
        path = self.path.split("?", 1)[0].lower()
        if path.endswith((".js", ".css", ".html", ".mjs")):
            self.send_header("Cache-Control", "no-cache")
        self.send_header("X-AgentNetPort-WebUI", "1")
        # The browser receives a signed, HttpOnly capability while loading the
        # page.  /_anp/session and /_anp/probe require this cookie below.
        query_nonce = ""
        try:
            query_nonce = (urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("anp_session") or [""])[0]
        except Exception:  # noqa: BLE001
            pass
        bootstrap_ok = (not getattr(self, "_bootstrap_nonce", "")
                        or hmac.compare_digest(query_nonce, self._bootstrap_nonce))
        if (path in ("", "/", "/index.html") and bootstrap_ok
                and not _valid_session_nonce(_session_nonce_value(self.headers.get("Cookie")))):
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={_new_session_nonce()}; Path=/; HttpOnly; SameSite=Strict",
            )
        super().end_headers()

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 64 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return {}

    def _client_is_loopback(self) -> bool:
        """这个连接的**对端**是不是本机（只有它能拿 `/_anp/session` 里的令牌）。

        必须看 socket 对端地址：`Host` 头是客户端随口填的，`X-Forwarded-For` 更是。
        对端取不到时（异常路径）按"不是本机"处理——安全判断默认收紧。
        """
        try:
            host = str(self.client_address[0] or "").strip("[]").lower()
        except Exception:  # noqa: BLE001
            return False
        return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")

    def _session_is_valid(self) -> bool:
        return _valid_session_nonce(_session_nonce_value(self.headers.get("Cookie")))

    def do_GET(self) -> None:  # noqa: N802
        """本机自动登录面 + 进程灯探测。只在本静态站暴露（默认 127.0.0.1）。"""
        path = self.path.split("?", 1)[0]
        if path == "/health":
            # ★hub 的伴生链路靠这个端点认出"看板在不在"★（`core.webui_alive()`）。
            # 没有它，hub 每次启动都会把"端口被别的东西占着"误判成空端口，然后拉起一个
            # 必然绑定失败的伴生看板（2026-09-26 实测：hub 日志里
            # "看板伴生：pid=… → http://127.0.0.1:8898/" 而 8898 上明明已经有看板）。
            # ★只报服务名，绝不回令牌★——这个端点免令牌，令牌只在 /_anp/session 发。
            self._send_json({"ok": True, "service": "AgentNetPort-webui",
                             "hub": find_hub_url()})
            return
        if path == "/_anp/session":
            # ★只给"本机浏览器"发令牌★（2026-09-26 外部审计复现）：
            # 这个端点原本**完全无鉴权**——它存在的意义是"桌面壳打开的就是本机，
            # 让页面不必让主人手抄令牌"。但只要静态站不是只听回环（`--host 0.0.0.0`），
            # 同一个端点就会把能看全部正文的管理员令牌发给整个局域网。
            # 判据是**连接的对端地址**（不是 Host 头——那个客户端可以随便填）：
            # 非回环客户端一律当它不存在，返回与静态站整体一致的 404。
            if not self._client_is_loopback():
                self._send_json({"ok": False, "error": "not found"}, 404)
                return
            if not self._session_is_valid():
                # Do not reveal whether this is the session route to callers
                # that have not loaded the local page first.
                self._send_json({"ok": False, "error": "not found"}, 404)
                return
            hub = find_hub_url()
            token = resolve_admin_token(hub)
            self._send_json({
                "ok": True,
                "service": "AgentNetPort-webui",
                "hub": hub,
                "adminToken": token,
                "autologin": bool(token),
                "source": "desktop",
            })
            return
        if path == "/_anp/probe":
            # GET /_anp/probe?url=http://127.0.0.1:PORT/anp/health
            if not self._session_is_valid():
                self._send_json({"ok": False, "error": "not found"}, 404)
                return
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            url = (q.get("url") or [""])[0]
            self._send_json(probe_urls([url]).get(url) or {"ok": False, "error": "bad url"})
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/_anp/probe":
            if not self._session_is_valid():
                self._send_json({"ok": False, "error": "not found"}, 404)
                return
            body = self._read_json_body()
            urls = body.get("urls") if isinstance(body.get("urls"), list) else []
            self._send_json({"ok": True, "results": probe_urls([str(u) for u in urls][:32])})
            return
        self._send_json({"ok": False, "error": "not found"}, 404)


def _hub_watchdog(hub: str) -> threading.Thread:
    """hub 不在了就自己退：信箱的伴生视图，不能比信箱活得久。

    ★为什么必须有★（2026-09-26 接线时实测踩到）：hub 的 `start_webui_companion()`
    注释里写着"看板会周期性探活，hub 不在了它自己退"，但 `desktop.py` 从来没有这段——
    于是每个被 hub 拉起的看板在 hub 死后都变成**孤儿进程**（实测：跑一轮回归留下 100+
    个 idle 的 desktop.py，各自占着一个随机端口）。"hub 停它就停"不能只靠
    Windows Job 对象（强杀路径之外还有各种死法），得有自己的判据。

    只对**被 hub 拉起**的实例启用（`ANP_WEBUI_COMPANION=1` 由 hub 注入）：
    主人自己双击起的看板不该因为 hub 重启一下就消失。

    退出判据要同时满足"连续 miss"和"持续 miss"：hub 重启通常只要几秒，
    只判连续次数会把它误杀；只判持续时间会让孤儿多活半分钟。两者相交才退。
    """
    interval = 5.0
    need_miss = 3
    need_sec = 30.0

    def _run() -> None:
        miss = 0
        down_since = 0.0
        while True:
            time.sleep(interval)
            if alive(hub.rstrip("/") + "/health"):
                miss, down_since = 0, 0.0
                continue
            miss += 1
            down_since = down_since or time.time()
            if miss >= need_miss and (time.time() - down_since) >= need_sec:
                _log(f"hub {hub} 已不在（连续 {miss} 次探活失败，"
                     f"持续 {time.time() - down_since:.0f} 秒）：看板随之退出")
                os._exit(0)  # noqa: SLF001 - 伴生进程：主线程睡在 sleep 里，直接退最干净

    th = threading.Thread(target=_run, name="anp-webui-hub-watchdog", daemon=True)
    th.start()
    return th


def start_static_server(host: str, port: int, bootstrap_nonce: str = "") -> ThreadingHTTPServer | None:
    """在 host:port 起静态站。端口已被别人占用时返回 None（调用方决定复用或换口）。"""
    handler = partial(QuietHandler, directory=str(HERE), bootstrap_nonce=bootstrap_nonce)
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        _log(f"端口 {host}:{port} 起不来：{exc}")
        return None
    nonce_path = _write_bootstrap_nonce(host, httpd.server_address[1], bootstrap_nonce)
    httpd._anp_nonce_path = nonce_path  # noqa: SLF001 - 生命周期清理用
    atexit.register(lambda: nonce_path.unlink(missing_ok=True))
    th = threading.Thread(target=httpd.serve_forever, daemon=True,
                          name="anp-webui-static")
    th.start()
    atexit.register(httpd.shutdown)
    return httpd


def find_browser() -> list[str] | None:
    """返回可执行的浏览器启动命令前缀；找不到返回 None。"""
    env_bin = (os.environ.get("ANP_WEBUI_BROWSER") or "").strip()
    if env_bin and Path(env_bin).is_file():
        return [env_bin]
    which = shutil.which("msedge") or shutil.which("chrome") or shutil.which("chromium")
    if which:
        return [which]
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path.home() / "AppData/Local/Microsoft/Edge/Application/msedge.exe"),
        str(Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"),
    ]
    for c in candidates:
        if Path(c).is_file():
            return [c]
    return None


def profile_dir() -> Path:
    """独立浏览器配置目录：窗口可复用，且不污染日常浏览器。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(base) / "AgentNetPort" / "webui-profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def open_desktop_window(url: str, *, browser: list[str] | None = None) -> subprocess.Popen | None:
    """用 Edge/Chrome 应用模式弹出桌面窗口（无地址栏 / 无标签页）。"""
    cmd_prefix = browser if browser is not None else find_browser()
    if not cmd_prefix:
        return None
    profile = profile_dir()
    # --app=：真正的应用窗口；同 profile 再开会聚焦已有窗口而不是新标签
    argv = [
        *cmd_prefix,
        f"--app={url}",
        f"--user-data-dir={profile}",
        f"--window-size={WIN_W},{WIN_H}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=CalculateNativeWinOcclusion",
        "--new-window",
    ]
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc
    except OSError as exc:
        _log(f"拉起浏览器失败：{exc}")
        return None


def ensure_server(host: str, port: int, bootstrap_nonce: str = "") -> str:
    """保证**本桌面壳**的静态站在 host:port 可用，返回最终 URL。"""
    bootstrap_nonce = bootstrap_nonce or _new_session_nonce()
    url = f"http://{host}:{port}/"
    probe = url + "index.html"
    if alive(probe):
        who = who_serves(probe)
        nonce = ""
        try:
            nonce = _bootstrap_nonce_path(host, port).read_text(encoding="ascii").strip()
        except OSError:
            pass
        if who in ("webui", "other") and nonce and has_autologin_endpoint(url, nonce):
            _log(f"桌面壳静态站已在 {url}，复用")
            return url + "?anp_session=" + urllib.parse.quote(nonce, safe="")
        _log(f"{url} 已被占用或不是本桌面壳（{who or 'unknown'}），改用临时端口")
        port = free_port(host)
        url = f"http://{host}:{port}/"

    httpd = start_static_server(host, port, bootstrap_nonce)
    if httpd is None:
        port = free_port(host)
        url = f"http://{host}:{port}/"
        httpd = start_static_server(host, port, bootstrap_nonce)
        if httpd is None:
            raise SystemExit(f"无法在 {host} 上绑定静态站端口")
    for _ in range(40):
        if alive(url + "index.html") and has_autologin_endpoint(url, bootstrap_nonce):
            launch_url = url + "?anp_session=" + urllib.parse.quote(bootstrap_nonce, safe="")
            _log(f"静态站就绪 {url}  root={HERE}")
            return launch_url
        time.sleep(0.05)
    raise SystemExit(f"静态站未就绪：{url}")


def sync_adapters(hub: str = "") -> None:
    """启动时把 `adapters/` 同步进 `<home>/plugins/` 并加载。

    ★为什么要自动★：Adapter 写完只在仓库 `adapters/`，Hub 只认
    `<home>/plugins/`。漏装会在 WebUI 里表现为「没有 dsh/codex」。
    可用 `ANP_WEBUI_SYNC_ADAPTERS=0` 关掉。
    """
    if str(os.environ.get("ANP_WEBUI_SYNC_ADAPTERS") or "1").strip().lower() in (
            "0", "false", "no", "off"):
        _log("adapter 同步已关闭（ANP_WEBUI_SYNC_ADAPTERS=0）")
        return
    script = HERE.parent / "tools" / "install_adapters.py"
    if not script.is_file():
        return
    try:
        r = subprocess.run(
            [sys.executable, str(script), "--quiet", "--home",
             os.environ.get("AGENT_ANP_HOME") or str(Path.home() / ".agentnetport"),
             "--hub", hub or find_hub_url()],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
            **_hidden_console_kwargs(),
        )
        if r.returncode == 0:
            _log("adapters 已同步到 home/plugins 并加载")
        else:
            # ★把非零当失败说出来★（2026-09-26 外部审计）：安装器以前"拷贝成功就回 0"，
            # 连 Hub 不可达/没令牌/插件没加载上都显示"同步成功"，看板里却什么都没有。
            _log(f"adapter 同步未完成（install_adapters 返回 {r.returncode}）："
                 f"{(r.stdout or '').strip()[-400:]} {(r.stderr or '').strip()[-200:]}")
    except Exception as exc:  # noqa: BLE001
        _log(f"adapter 同步失败：{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ANP WebUI 桌面窗口")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true",
                    help="只起静态站，不弹窗（自测 / 无头）")
    ap.add_argument("--url", default="",
                    help="强制打开这个 URL（默认 <host>:<port>/）")
    ap.add_argument("--print-url", action="store_true",
                    help="打印 URL 后仍继续（给脚本抓）")
    ap.add_argument("--no-sync-adapters", action="store_true",
                    help="启动时不安装/加载 adapters/")
    ap.add_argument("--no-start-hub", action="store_true",
                    help="不自动启动未运行的本机 Hub")
    args = ap.parse_args(argv)

    url = ensure_server(args.host, args.port, _new_session_nonce())
    companion = str(os.environ.get("ANP_WEBUI_COMPANION") or "").strip().lower() not in (
        "", "0", "false", "no")
    hub_url = find_hub_url()
    hub_ready = wait_for_hub_ready(hub_url)
    if not companion and not args.no_browser and not args.no_start_hub:
        hub_ready = ensure_hub_for_desktop(hub_url)
    if not args.no_sync_adapters:
        if hub_ready:
            sync_adapters(hub_url)
        else:
            _log(f"Hub 不可用，跳过 Adapter 同步：{hub_url}")
    # ★被 hub 拉起时挂上看门狗★（hub 不在就退，别变成孤儿进程）
    if companion:
        _hub_watchdog(find_hub_url())
    if args.print_url:
        print(url, flush=True)

    if args.no_browser:
        _log("no-browser 模式，静态站保持运行（Ctrl+C 退出）")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    browser = find_browser()
    if browser is None:
        _log("找不到 Edge/Chrome。可设 ANP_WEBUI_BROWSER=完整路径，")
        _log(f"或手动打开 {url}")
        # 仍然把服务挂着，方便手动开
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    proc = open_desktop_window(url if not args.url else args.url, browser=browser)
    if proc is None:
        _log(f"窗口没弹出来，请手动打开 {url}")
    else:
        _log(f"桌面窗口已打开（pid={proc.pid}）  url={url}")
        _log(f"浏览器：{browser[0]}")

    # 服务线程是 daemon，这里挂住保持进程存活；窗口关掉后仍可 Ctrl+C 退
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
