/* ANP WebUI - Modern email-style interface with i18n support */
(function () {
  "use strict";

  const TOKEN_KEY = "anp-admin-token";
  const HUB_KEY = "anp-hub-base";
  const AUTOLOGIN_KEY = "anp-auto-login";
  const PAGE_SIZE = 200;
  const ONLINE_TTL = 300;

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  // i18n helper
  const t = (key, params) => window.i18n ? window.i18n.t(key, params) : key;

  function getHub() {
    return (localStorage.getItem(HUB_KEY) || "http://127.0.0.1:8900").replace(/\/+$/, "");
  }
  // 顶栏是“消息搜索”而不是地址栏。用户从桌面启动时容易把 Hub 地址
  // 粘进这里；若不拦截，左侧 totals 仍有数字，正文列表却会被 q 精确过滤为空。
  function isHubAddressSearch(value) {
    const raw = String(value || "").trim();
    if (!raw) return false;
    const hub = getHub();
    if (raw === hub || raw === hub + "/") return true;
    try {
      const entered = new URL(raw);
      const configured = new URL(hub);
      return entered.origin === configured.origin
        && (entered.pathname === "" || entered.pathname === "/")
        && !entered.search && !entered.hash;
    } catch (_e) {
      return false;
    }
  }
  function messageSearchValue() {
    return isHubAddressSearch(search) ? "" : search.trim();
  }
  function clearAccidentalHubSearch() {
    const box = $("#globalSearch");
    if (box && isHubAddressSearch(box.value)) {
      box.value = "";
      search = "";
    }
  }
  function getToken() {
    return localStorage.getItem(TOKEN_KEY) || "";
  }
  // Keep in sync for saveSettings / display
  let ADMIN_TOKEN = getToken();
  let HUB = getHub();
  let STATE = {
    messages: [],
    total: 0,
    offset: 0,
    totals: {},
    agents: [],
    panels: [],
    policy: {},
    integrity: {},
    online: true,
  };
  let selectedId = null;
  let currentFolder = "inbox";
  let filterSender = "";
  let unreadOnly = false;
  let search = "";
  let searchTimer = null;
  let autoRefreshEnabled = true;
  // 自动登录：默认开；桌面启动器会通过 /_anp/session 下发本地 admin.token
  let autoLoginEnabled = localStorage.getItem(AUTOLOGIN_KEY) !== "0";
  // ★登录口只主动弹一次★：关掉后不因 3 秒轮询 401 再弹（用户反馈：反复打开）
  let authModalUserDismissed = false;
  let authModalAutoShown = false;
  let lastAutoLoginAt = 0;
  let lastAutoLoginToken = "";
  let lastTriedToken = "";
  let appInited = false;
  let lastAgentsSig = "";
  let lastListSig = "";
  // 仅允许最新一轮消息请求提交结果，避免切换文件夹时旧响应覆盖当前视图。
  let messageRequestSeq = 0;
  // health 探测是异步的；旧一轮完成后不能再拆掉新一轮已经画好的侧栏。
  let agentsRenderSeq = 0;
  // 公共 API 也会被 3s 轮询重叠触发；旧一轮不能回写新状态。
  let refreshSeq = 0;
  // 进程灯探测缓存：url → {ok, at}
  const PROBE_TTL_MS = 8000;
  const probeCache = new Map();

  /** 从 Agent 记录推出「进程健康」候选 URL。
   *
   * process_api.url 是 Adapter 自己的 ANP 面；platform_url（如果有）才是
   * 宿主平台的健康面。两者都要探测，但不能把 Adapter 活着误报成平台在线。
   */
  function healthUrlsOf(a) {
    const out = [];
    const add = (value) => {
      const u = String(value || "").trim();
      if (u && !out.includes(u)) out.push(u);
    };
    const pa = a && a.process_api;
    if (pa && pa.kind === "http") {
      add(pa.platform_url);
      add(pa.url);
    }
    const wake = a && a.wake;
    if (wake && wake.kind === "http" && wake.url) {
      try {
        const u = new URL(String(wake.url));
        if (u.pathname.endsWith("/anp/wake")) {
          u.pathname = u.pathname.replace(/\/anp\/wake\/?$/, "/anp/health");
          add(u.toString());
        }
      } catch (_e) { /* ignore */ }
    }
    return out;
  }

  function platformHealthUrlsOf(a) {
    const pa = a && a.process_api;
    if (!pa || pa.kind !== "http" || !pa.platform_url) return [];
    return [String(pa.platform_url).trim()].filter(Boolean);
  }

  function healthUrlOf(a) {
    return healthUrlsOf(a)[0] || "";
  }

  /** 经桌面壳同源探测（绕开浏览器 CORS）；无桌面壳则返回 null=未知。 */
  async function probeHealth(urls) {
    const unique = [...new Set(urls.filter(Boolean))];
    const now = Date.now();
    const need = unique.filter((u) => {
      const c = probeCache.get(u);
      return !c || now - c.at > PROBE_TTL_MS;
    });
    if (need.length) {
      try {
        const ctrl = new AbortController();
        // 批量探测要盖住多个 1s+ 超时端点，整体预算放宽
        const timer = setTimeout(() => ctrl.abort(), 8000);
        const r = await fetch("/_anp/probe", {
          method: "POST",
          headers: { "content-type": "application/json", accept: "application/json" },
          body: JSON.stringify({ urls: need }),
          signal: ctrl.signal,
          cache: "no-store",
        });
        clearTimeout(timer);
        if (r.ok) {
          const data = await r.json();
          const results = (data && data.results) || {};
          for (const u of need) {
            const hit = results[u];
            probeCache.set(u, { ok: !!(hit && hit.ok), at: Date.now(), raw: hit || null });
          }
        }
      } catch (_e) {
        // 无桌面壳：保持未知，走 last_seen 兜底
      }
    }
    const out = {};
    for (const u of unique) {
      const c = probeCache.get(u);
      out[u] = c ? c.ok : null;
    }
    return out;
  }

  /**
   * 心跳灯语义（对应协议里的 lease / liveness 分离）：
   *
   *   online   Adapter/平台进程健康探针可达；没有探针时才看新鲜 heartbeat
   *   wakeable 探针不可达，但仍有唤醒通道或旧心跳记录
   *   offline  没有可达探针、唤醒通道或新鲜心跳
   *
   * process_api 是 WebUI 判断“当前能不能接到”的主要事实；heartbeat 是没有
   * process_api 时的 lease 兜底。★probe 只 GET health，不 POST /anp/wake★。
   */
  function isPlatformHealthUrl(u) {
    try {
      const p = new URL(String(u)).pathname;
      return !/\/anp\/health\/?$/i.test(p);
    } catch (_e) {
      return false;
    }
  }

  function agentStatus(a, probes) {
    const urls = healthUrlsOf(a);
    const platformUrls = new Set(platformHealthUrlsOf(a));
    let platformOk = false;
    let adapterOk = false;
    for (const u of urls) {
      const v = probes ? probes[u] : undefined;
      // An explicit platform_url wins over URL-shape guessing. Older adapters
      // without that field retain the legacy /anp/health fallback semantics.
      const isPlat = platformUrls.has(u) ||
        (platformUrls.size === 0 && isPlatformHealthUrl(u));
      if (isPlat && v === true) platformOk = true;
      if (!isPlat && v === true) adapterOk = true;
    }
    const lastSeen = Number(a && a.last_seen || 0);
    const heartbeatFresh = lastSeen > 0 &&
      (Date.now() / 1000 - lastSeen) <= ONLINE_TTL;
    // ★进程探针优先★：Adapter 自己可达就代表当前 ANP 通道在线。
    // 对 ChenCode/MiMo，platform_url 是额外的平台事实；即使平台端点暂时
    // 不响应，Adapter 仍能收信、排队或唤醒，不应被显示成离线/仅可唤醒。
    if (platformOk || adapterOk) return "online";
    // 没有可达探针时，旧心跳仍可作为无探针 Agent 的在线租约。
    if (heartbeatFresh) return "online";
    if (a.wake) return "wakeable";
    return "offline";
  }

  function getAutoLogin() {
    return localStorage.getItem(AUTOLOGIN_KEY) !== "0";
  }
  function setAutoLogin(on) {
    autoLoginEnabled = !!on;
    localStorage.setItem(AUTOLOGIN_KEY, on ? "1" : "0");
    if (on) {
      lastAutoLoginAt = 0; // 重新允许拉一次
    }
  }

  /** 自动登录：向同源桌面壳取 hub + admin token；成功则写入本地并返回 true。 */
  async function tryAutoLogin(force) {
    if (!getAutoLogin()) return false;
    const now = Date.now();
    // 节流：3s 轮询里不再反复打 /_anp/session；force 用于启动/保存设置
    if (!force && lastAutoLoginAt && now - lastAutoLoginAt < 15000) {
      return !!getToken();
    }
    lastAutoLoginAt = now;
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 1500);
      const r = await fetch("/_anp/session", {
        headers: { accept: "application/json" },
        signal: ctrl.signal,
        cache: "no-store",
      });
      clearTimeout(timer);
      if (!r.ok) return false;
      const d = await r.json();
      const hubFromDesk = String((d && d.hub) || "").replace(/\/+$/, "");
      const previousHub = HUB;
      if (hubFromDesk) HUB = hubFromDesk;
      // 桌面壳明确表示当前 Hub 是远端且没有本地自动令牌时，不能把旧的
      // 本地 localStorage token 继续带到新 Hub；跨 Hub 切换必须重新手动登录。
      if (!d || !d.adminToken) {
        if (d && d.autologin === false && hubFromDesk && hubFromDesk !== previousHub) {
          ADMIN_TOKEN = "";
          localStorage.removeItem(TOKEN_KEY);
        }
        return false;
      }
      const tok = String(d.adminToken);
      // 同一个错 token 一直 401 时，别以为“自动登录成功”就继续弹登录口
      lastAutoLoginToken = tok;
      ADMIN_TOKEN = tok;
      localStorage.setItem(TOKEN_KEY, ADMIN_TOKEN);
      localStorage.setItem(HUB_KEY, HUB);
      return true;
    } catch (_e) {
      // 没有桌面壳 / 纯静态 http.server：退回手动填 token
      return false;
    }
  }

  /** 自动触发的登录口：整段会话最多弹一次；用户手动关掉后不再弹。 */
  function maybeShowAuthPrompt(message) {
    if (authModalUserDismissed || authModalAutoShown) return false;
    const modal = $("#settingsModal");
    if (modal && !modal.classList.contains("hidden")) return false;
    authModalAutoShown = true;
    showSettings(message);
    return true;
  }

  /** 401 收口：能自动续上就续，续不上只提示一次。 */
  function handleUnauthorized() {
    STATE.online = true;
    const finish = () => {
      maybeShowAuthPrompt(t("auth_failed"));
    };
    if (getAutoLogin()) {
      // 只有真的换了 token 才重试；节流里拿到“旧 token 仍有效”不重试
      tryAutoLogin(false).then((ok) => {
        const tok = getToken();
        if (ok && tok && tok === lastAutoLoginToken && tok !== (lastTriedToken || "")) {
          lastTriedToken = tok;
          loadMessages()
            .then((applied) => {
              if (!applied) return;
              renderMessageList();
              renderMessageDetail();
              renderFolders();
            })
            .catch(finish);
          return;
        }
        finish();
      });
      return;
    }
    finish();
  }

  // ---------- API ----------
  function headers(admin) {
    const h = { accept: "application/json" };
    const token = getToken();
    if (admin && token) h["x-anp-admin"] = token;
    return h;
  }

  async function apiGet(path, admin, timeoutMs = 3000) {
    const options = { headers: headers(admin) };
    const ms = timeoutMs > 0 ? timeoutMs : 3000;
    const controller = new AbortController();
    options.signal = controller.signal;
    const timer = setTimeout(() => controller.abort(), ms);
    let r;
    try {
      r = await fetch(getHub() + path, options);
    } finally {
      clearTimeout(timer);
    }
    if (r.status === 401) {
      const err = new Error("unauthorized");
      err.status = 401;
      throw err;
    }
    if (!r.ok) {
      const err = new Error("http " + r.status);
      err.status = r.status;
      throw err;
    }
    return r.json();
  }

  function qs(params) {
    const u = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
    });
    const s = u.toString();
    return s ? "?" + s : "";
  }

  // Admin calls always get a hard timeout so one hung endpoint cannot block the UI.
  const ADMIN_TIMEOUT_MS = 2000;

  async function loadMessages() {
    const requestSeq = ++messageRequestSeq;
    const folderAtStart = currentFolder;
    const inbox = getFolderInbox(currentFolder);
    const query = messageSearchValue();
    let data;
    try {
      data = await apiGet(
        "/admin/messages" +
          qs({
            inbox: inbox && !inbox.startsWith("__panel__") ? inbox : "",
            from: filterSender,
            unread: unreadOnly ? "1" : "",
            q: query,
            limit: PAGE_SIZE,
            offset: STATE.offset,
          }),
        true,
        ADMIN_TIMEOUT_MS
      );
    } catch (e) {
      // 旧请求的失败也不能把当前文件夹误报成离线。
      if (requestSeq !== messageRequestSeq || folderAtStart !== currentFolder) return false;
      throw e;
    }
    if (requestSeq !== messageRequestSeq || folderAtStart !== currentFolder) return false;
    // 管理面已经按收件箱/发件人/搜索条件过滤；这里必须完整展示返回页。
    // 旧逻辑按身份前缀和 kind 二次删信，会把合法 Adapter/联调信删掉，导致
    // 点击某个 Agent 后出现“收件箱为空”，而左侧计数仍显示有信。
    STATE.messages = Array.isArray(data.messages) ? data.messages : [];
    STATE.total = data.total || 0;
    STATE.totals = data.totals || STATE.totals;
    return true;
  }

  async function loadAgents(seq = refreshSeq) {
    const data = await apiGet("/agents", false);
    if (seq !== refreshSeq) return false;
    STATE.agents = data.agents || [];
    return true;
  }

  async function loadHealth(seq = refreshSeq) {
    const data = await apiGet("/health", false);
    if (seq !== refreshSeq) return false;
    STATE.policy = data.policy || {};
    STATE.integrity = data.integrity || {};
    if (data.mail && data.mail.totals) STATE.totals = { ...data.mail.totals, ...STATE.totals };
    return true;
  }

  async function loadPanels(seq = refreshSeq) {
    const nextPanels = [];
    let listing;
    try {
      listing = await apiGet("/admin/panel", true, ADMIN_TIMEOUT_MS);
    } catch (e) {
      // ★401 不往侧栏塞错误面板★：每 3 秒重建 DOM 会让界面一直闪
      if (e && e.status === 401) {
        if (seq === refreshSeq) STATE.panels = [];
        return false;
      }
      // Listing failed/timed out: keep UI alive with an error shell.
      if (seq === refreshSeq) STATE.panels = [{
        plugin_id: "_panel_error",
        title: t("panel_error", { error: e.name === "AbortError" ? "Timeout (2s)" : String(e.message || e) }),
        error: true,
      }];
      return false;
    }
    for (const item of listing.panels || []) {
      const pid = String(item.plugin_id || item.id || "");
      if (!pid) continue;
      const entry = Object.assign({ plugin_id: pid }, item);
      try {
        const body = await apiGet("/admin/panel/" + encodeURIComponent(pid), true, ADMIN_TIMEOUT_MS);
        Object.assign(entry, body);
      } catch (e) {
        entry.error = e.name === "AbortError" ? "Timeout (2s)" : String(e.message || e);
      }
      nextPanels.push(entry);
      if (seq !== refreshSeq) return false;
    }
    if (seq !== refreshSeq) return false;
    STATE.panels = nextPanels;
    return true;
  }

  function isUnauthorized(result) {
    return result.status === "rejected" && result.reason && result.reason.status === 401;
  }

  async function loadAll() {
    const seq = ++refreshSeq;
    // Public path: always load + render, never blocked by admin APIs.
    await Promise.allSettled([loadAgents(seq), loadHealth(seq)]);
    if (seq !== refreshSeq) return;
    renderAgents();
    renderFolders();

    if (!getToken()) {
      const box = $("#messageListContent");
      box.textContent = "";
      box.appendChild(el("div", "empty-state", t("auth_failed")));
      return;
    }

    // Admin path: messages and panels settle independently and each paints immediately.
    // Promise.allSettled still waits for both; so fire-and-render per stream instead.
    const messagesDone = loadMessages()
      .then((applied) => {
        if (!applied || seq !== refreshSeq) return;
        STATE.online = true;
        renderMessageList();
        renderMessageDetail();
        renderFolders();
      })
      .catch((e) => {
        if (seq !== refreshSeq) return;
        if (e && e.status === 401) {
          handleUnauthorized();
          renderMessageList();
          return;
        }
        STATE.online = false;
        renderMessageList();
      });

    const panelsDone = loadPanels(seq)
      .then(() => {
        if (seq !== refreshSeq) return;
        renderAgents();
        if (currentFolder.startsWith("panel:")) {
          renderMessageList();
          renderMessageDetail();
        }
      })
      .catch(() => {
        renderAgents();
      });

    await Promise.allSettled([messagesDone, panelsDone]);
  }

  // ---------- Folder mapping ----------
  function getFolderInbox(folder) {
    if (folder === "inbox") return "";
    if (folder.startsWith("agent:")) return folder.slice(6);
    if (folder.startsWith("panel:")) return "__panel__:" + folder.slice(6);
    return "";
  }

  function getFolderMessages() {
    if (currentFolder.startsWith("panel:")) {
      const pid = currentFolder.slice(6);
      const p = STATE.panels.find((x) => x.plugin_id === pid);
      return panelRows(p || {});
    }
    return STATE.messages;
  }

  function panelRows(p) {
    if (!p) return [];
    const rows = p.bulletins || p.items || p.rows || [];
    // 公告栏条目 → 统一成列表能画的形状（不改底层数据）
    return rows.map((r, i) => ({
      id: r.id || r.message_id || `${p.plugin_id || "panel"}-${i}`,
      from: r.from || r.sender || r.author || p.title || p.plugin_id,
      subject: r.subject || r.title || t("no_subject"),
      body: r.body || r.text || r.content || "",
      ts: r.ts || (r.time ? undefined : undefined),
      time_text: r.time || "",
      kind: r.kind || "announcement",
      read: true,
      delivered_at: r.ts || Date.now() / 1000,
      meta: r.meta || {},
    }));
  }

  // ---------- Status helpers ----------
  /** 侧栏主列表：只留真正的 ANP Agent（可被唤醒通信的对端）。 */
  function isSidebarAgent(a) {
    const name = String((a && a.name) || "");
    if (!name) return false;
    // 系统插件族：anp.adapter.* / anp.bulletin … 一律不进主列表（面板区单独展示）
    if (name.startsWith("anp.")) return false;
    if (name === "hub" || name === "webui") return false;
    const wake = a.wake;
    // 主列表 = 有 wake 的托管 Agent；测试发信方/历史身份不进主列表
    return !!(wake && (wake.kind || wake.url));
  }

  function getInitials(name) {
    if (!name) return "?";
    const parts = name.trim().split(/\s+/);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return name.slice(0, 2).toUpperCase();
  }

  function formatTime(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const msgDate = new Date(d.getFullYear(), d.getMonth(), d.getDate());

    if (msgDate.getTime() === today.getTime()) {
      return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
    }

    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    if (msgDate.getTime() === yesterday.getTime()) {
      return t("time_yesterday");
    }

    const weekAgo = new Date(today);
    weekAgo.setDate(weekAgo.getDate() - 7);
    if (msgDate >= weekAgo) {
      const days = t("time_days");
      return Array.isArray(days) ? days[d.getDay()] : ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()];
    }

    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }

  function formatFullTime(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleString("en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false
    });
  }

  // ---------- Render ----------
  function renderAll() {
    renderFolders();
    Promise.resolve(renderAgents()).catch(() => {});
    renderMessageList();
    renderMessageDetail();
  }

  function renderFolders() {
    const totals = STATE.totals || {};
    $("#inboxCount").textContent = totals.messages || STATE.total || 0;

    $$(".folder-item").forEach(item => {
      const folder = item.dataset.folder;
      item.classList.toggle("active", currentFolder === folder);
      item.onclick = () => {
        currentFolder = folder;
        STATE.offset = 0;
        selectedId = null;
        messageRequestSeq += 1;
        syncAgentActiveState();
        loadAll();
      };
    });
  }

  async function renderAgents() {
    const renderSeq = ++agentsRenderSeq;
    const box = $("#agentsList");
    const visible = (STATE.agents || []).filter(isSidebarAgent);
    // 未读优先，其次有信，最后按名字
    visible.sort((a, b) =>
      (Number(b.unread || 0) - Number(a.unread || 0)) ||
      (Number(b.messages || 0) - Number(a.messages || 0)) ||
      String(a.name).localeCompare(String(b.name)));

    // ★进程灯★：Hub 只透传 process_api，探测在 WebUI（见 agent_view 注释）
    const probeList = [];
    for (const a of visible) probeList.push(...healthUrlsOf(a));
    const probes = await probeHealth(probeList);

    // 只提交最后一轮探测结果；旧轮次直接丢弃，避免侧栏来回闪烁。
    if (renderSeq !== agentsRenderSeq) return;

    // ★防闪★：内容没变就不要每 3 秒拆掉重建侧栏
    const sig = JSON.stringify({
      panels: (STATE.panels || []).map((p) => [p.plugin_id, p.title || "", panelRows(p).length, !!p.error]),
      agents: visible.map((a) => [a.name, agentStatus(a, probes), a.messages || 0, a.unread || 0]),
    });
    if (sig === lastAgentsSig) {
      syncAgentActiveState();
      return;
    }
    lastAgentsSig = sig;
    const next = document.createDocumentFragment();

    // 插件面板（公告等）置顶：用「面板」角标，不用状态灯
    for (const p of STATE.panels) {
      const d = el("div", "agent-item panel-item");
      d.dataset.panel = p.plugin_id;

      const icon = el("span", "panel-badge", "板");
      icon.title = t("empty_panel");

      const nameSpan = el("span", "agent-name", (p.title || p.plugin_id));
      const countSpan = el("span", "count", String(panelRows(p).length || 0));

      d.appendChild(icon);
      d.appendChild(nameSpan);
      d.appendChild(countSpan);

      d.onclick = () => {
        currentFolder = "panel:" + p.plugin_id;
        STATE.offset = 0;
        selectedId = null;
        messageRequestSeq += 1;
        syncAgentActiveState();
        renderAll();
      };

      next.appendChild(d);
    }

    // 真正的 ANP Agent
    for (const a of visible) {
      const st = agentStatus(a, probes);
      const d = el("div", "agent-item");
      d.dataset.agent = a.name;

      // 三态都保留可见灯：心跳在线、可唤醒、离线分别用不同颜色。
      const statusDot = el("span", "status-indicator " + st);
      statusDot.title = t(
        st === "online" ? "status_online"
          : st === "wakeable" ? "status_wakeable"
            : "status_offline"
      );
      d.appendChild(statusDot);

      const nameSpan = el("span", "agent-name", a.name);
      const countSpan = el("span", "count", String(a.messages || 0));

      d.appendChild(nameSpan);
      d.appendChild(countSpan);

      d.onclick = () => {
        currentFolder = "agent:" + a.name;
        STATE.offset = 0;
        selectedId = null;
        messageRequestSeq += 1;
        syncAgentActiveState();
        loadAll();
      };

      next.appendChild(d);
    }

    // 一次性替换，避免先清空再逐项 append 造成可见空白帧。
    box.replaceChildren(next);
    syncAgentActiveState();
  }

  function syncAgentActiveState() {
    $$("#agentsList .agent-item").forEach((item) => {
      const folder = item.dataset.panel
        ? "panel:" + item.dataset.panel
        : item.dataset.agent
          ? "agent:" + item.dataset.agent
          : "";
      item.classList.toggle("active", folder === currentFolder);
    });
  }

  function renderMessageList() {
    let title = t("folder_inbox");
    const activeSearch = messageSearchValue();
    let emptyText = activeSearch ? t("no_messages_found") : t("no_messages");
    if (currentFolder.startsWith("agent:")) {
      title = currentFolder.slice(6);
    } else if (currentFolder.startsWith("panel:")) {
      const pid = currentFolder.slice(6);
      const p = STATE.panels.find((x) => x.plugin_id === pid);
      title = (p && (p.title || p.plugin_id)) || pid;
      emptyText = t("empty_panel");
    }

    $("#folderTitle").textContent = title;

    const messages = getFolderMessages();
    const count = messages.length;
    $("#messageCount").textContent = count === 1
      ? t("message_count_singular", { count })
      : t("messages_count", { count });

    // ★防闪★：数据与状态没变就不要重绘列表
    const listSig = JSON.stringify({
      folder: currentFolder,
      online: STATE.online,
      n: count,
      ids: messages.slice(0, 30).map((m) => m.id),
      selectedId,
      search: activeSearch,
    });
    if (listSig === lastListSig) return;
    lastListSig = listSig;

    const box = $("#messageListContent");
    box.textContent = "";

    if (!STATE.online) {
      box.appendChild(el("div", "empty-state", t("connection_failed")));
      return;
    }

    if (!messages.length) {
      box.appendChild(el("div", "empty-state", emptyText));
      return;
    }

    for (const m of messages) {
      const d = el("div", "message-item" + (selectedId === m.id ? " active" : "") + (!m.read ? " unread" : ""));

      // Add status badges
      const badges = el("div", "message-badges");
      if (!m.delivered_at) {
        const badge = el("span", "badge badge-warning", t("status_undelivered"));
        badges.appendChild(badge);
      }
      if (m.sig_ok === false) {
        const badge = el("span", "badge badge-error", "⚠ " + t("sig_tampered", { reason: m.sig_why || "tampered" }));
        badges.appendChild(badge);
      }
      if (m.wake_skipped) {
        const badge = el("span", "badge badge-info", t("status_cached"));
        badges.appendChild(badge);
      }
      if (m.kind === "announcement" || (m.meta && m.meta.broadcast)) {
        const badge = el("span", "badge badge-info", t("status_broadcast"));
        badges.appendChild(badge);
      }

      const header = el("div", "message-header");
      const avatar = el("div", "message-avatar", getInitials(m.from || "?"));
      const senderInfo = el("div", "message-sender-info");
      const sender = el("div", "message-sender", m.from || t("unknown_sender"));
      const isBul = m.kind === "announcement";
      const recipient = el("div", "message-recipient",
        isBul ? (t("status_broadcast") || "公告") : (t("to_prefix") + (m.to || t("unknown_sender"))));
      const time = el("div", "message-time", m.time_text || formatTime(m.ts));

      senderInfo.appendChild(sender);
      senderInfo.appendChild(recipient);
      header.appendChild(avatar);
      header.appendChild(senderInfo);
      header.appendChild(time);

      const subject = el("div", "message-subject", m.subject || t("no_subject"));
      const preview = el("div", "message-preview", String(m.body || "").slice(0, 150).replace(/\s+/g, " "));

      d.appendChild(badges);
      d.appendChild(header);
      d.appendChild(subject);
      d.appendChild(preview);

      d.onclick = () => {
        selectedId = m.id;
        renderMessageList();
        renderMessageDetail();
      };

      box.appendChild(d);
    }
  }

  function renderMessageDetail() {
    const box = $("#messageDetailContent");
    box.textContent = "";

    const messages = getFolderMessages();
    const m = messages.find(x => x.id === selectedId);

    if (!m) {
      box.className = "empty-state";
      box.textContent = t("select_message");
      return;
    }

    box.className = "";

    // Header
    const header = el("div", "detail-header");
    const subject = el("div", "detail-subject", m.subject || t("no_subject"));

    const meta = el("div", "detail-meta");
    const avatar = el("div", "detail-avatar", getInitials(m.from || "?"));
    const senderInfo = el("div", "detail-sender-info");
    const sender = el("div", "detail-sender", m.from || t("unknown_sender"));
    const isBulletin = m.kind === "announcement";
    const recipient = el("div", "detail-recipient",
      isBulletin ? (t("status_broadcast") || "公告") : (t("to_prefix") + (m.to || t("unknown_sender"))));
    const time = el("div", "detail-time", m.time_text || formatFullTime(m.ts || m.delivered_at));

    // Add metadata section
    const metaSection = el("div", "detail-metadata");
    if (m.id) {
      const row = el("div", "meta-row");
      row.appendChild(el("span", "meta-label", t("meta_id")));
      row.appendChild(el("span", "meta-value", m.id));
      metaSection.appendChild(row);
    }
    if (m.correlation_id) {
      const row = el("div", "meta-row");
      row.appendChild(el("span", "meta-label", t("meta_correlation_id")));
      row.appendChild(el("span", "meta-value", m.correlation_id));
      metaSection.appendChild(row);
    }
    if (m.delivered_at) {
      const row = el("div", "meta-row");
      row.appendChild(el("span", "meta-label", t("meta_delivered_at")));
      row.appendChild(el("span", "meta-value", formatFullTime(m.delivered_at)));
      metaSection.appendChild(row);
    }
    if (!m.delivered_at && m.next_retry_at) {
      const row = el("div", "meta-row");
      row.appendChild(el("span", "meta-label", t("meta_next_retry")));
      row.appendChild(el("span", "meta-value", formatFullTime(m.next_retry_at)));
      metaSection.appendChild(row);
    }
    if (m.attempts && m.attempts > 1) {
      const row = el("div", "meta-row");
      row.appendChild(el("span", "meta-label", t("meta_attempts")));
      row.appendChild(el("span", "meta-value", String(m.attempts)));
      metaSection.appendChild(row);
    }
    if (m.last_error) {
      const row = el("div", "meta-row");
      row.appendChild(el("span", "meta-label", t("meta_last_error")));
      row.appendChild(el("span", "meta-value", m.last_error));
      metaSection.appendChild(row);
    }
    if (m.sig_ok !== undefined) {
      const row = el("div", "meta-row");
      row.appendChild(el("span", "meta-label", t("meta_signature")));
      const sigText = m.sig_ok === false
        ? t("sig_tampered", { reason: m.sig_why || "tampered" })
        : m.sig_ok === true
          ? m.sig_why === "unsigned" ? t("sig_unsigned") : t("sig_ok")
          : t("sig_none");
      row.appendChild(el("span", "meta-value", sigText));
      metaSection.appendChild(row);
    }

    senderInfo.appendChild(sender);
    senderInfo.appendChild(recipient);
    if (metaSection.children.length > 0) {
      senderInfo.appendChild(metaSection);
    }
    meta.appendChild(avatar);
    meta.appendChild(senderInfo);
    meta.appendChild(time);

    header.appendChild(subject);
    header.appendChild(meta);
    box.appendChild(header);

    // Body - XSS FIX: use textContent only, no innerHTML
    const body = el("div", "detail-body");
    const content = el("div", "detail-content");

    // Safe plain text rendering
    content.textContent = m.body || "";
    body.appendChild(content);

    box.appendChild(body);

  }

  // ---------- Settings modal ----------
  function showSettings(message) {
    const modal = $("#settingsModal");
    modal.classList.remove("hidden");
    $("#hubBase").value = HUB;
    $("#adminToken").value = ADMIN_TOKEN;
    $("#autoRefresh").checked = autoRefreshEnabled;
    const autoLoginBox = $("#autoLogin");
    if (autoLoginBox) autoLoginBox.checked = getAutoLogin();
    const errBox = $("#settingsError");
    if (errBox) {
      errBox.textContent = message || "";
      errBox.hidden = !message;
    }
  }

  function hideSettings(opts) {
    const silent = !!(opts && opts.silent === true);
    $("#settingsModal").classList.add("hidden");
    if (!silent) {
      authModalUserDismissed = true;
    }
  }

  /** 保存前用真请求验一次管理员令牌，避免“保存了但其实是错的”。 */
  async function probeAdmin(hub, token) {
    const base = String(hub || "").replace(/\/+$/, "");
    if (!base) return { ok: false, reason: "network" };
    const url = base + "/admin/messages?limit=1";
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 4000);
    try {
      const headers = { accept: "application/json" };
      if (token) headers["x-anp-admin"] = token;
      const r = await fetch(url, { headers, signal: ctrl.signal, cache: "no-store" });
      clearTimeout(timer);
      if (r.status === 401) return { ok: false, reason: "token" };
      if (r.status === 403) return { ok: false, reason: "token" };
      if (!r.ok) return { ok: false, reason: "http", status: r.status };
      return { ok: true };
    } catch (_e) {
      clearTimeout(timer);
      return { ok: false, reason: "network" };
    }
  }

  async function saveSettings() {
    const btn = $("#saveSettings");
    const errBox = $("#settingsError");
    const setErr = (msg) => {
      if (!errBox) return;
      errBox.textContent = msg || "";
      errBox.hidden = !msg;
    };

    const hubIn = ($("#hubBase").value || "").trim().replace(/\/+$/, "") || HUB;
    let tokenIn = ($("#adminToken").value || "").trim();
    const autoOn = $("#autoLogin") ? $("#autoLogin").checked : getAutoLogin();
    setAutoLogin(autoOn);
    autoRefreshEnabled = $("#autoRefresh") ? $("#autoRefresh").checked : autoRefreshEnabled;

    if (btn) {
      btn.disabled = true;
      btn.dataset.prevText = btn.textContent;
      btn.textContent = t("btn_verifying");
    }
    setErr("");

    try {
      // 开着自动登录且输入框为空：向桌面壳要一份再验
      if (autoOn && !tokenIn) {
        await tryAutoLogin(true);
        tokenIn = getToken() || tokenIn;
      }

      const probe = await probeAdmin(hubIn, tokenIn);
      if (!probe.ok) {
        if (probe.reason === "token") setErr(t("err_token_invalid"));
        else if (probe.reason === "network") setErr(t("err_hub_unreachable", { url: hubIn }));
        else setErr(t("err_hub_http", { status: probe.status || "?" }));
        // ★失败不关窗★：让错误停在设置里，而不是关掉又闪回来
        return;
      }

      // 校验通过才落盘并关窗
      HUB = hubIn;
      ADMIN_TOKEN = tokenIn;
      localStorage.setItem(HUB_KEY, HUB);
      localStorage.setItem(TOKEN_KEY, ADMIN_TOKEN);
      lastTriedToken = "";
      hideSettings({ silent: true });
      STATE.offset = 0;
      loadAll();
    } finally {
      if (btn) {
        btn.disabled = false;
        if (btn.dataset.prevText) btn.textContent = btn.dataset.prevText;
      }
    }
  }

  // ---------- Event handlers ----------
  // 顶栏 + 侧栏都有设置按钮（历史上重复 id），全部绑上
  $$("#settingsBtn").forEach((btn) => {
    btn.onclick = () => showSettings();
  });
  $("#closeSettings").onclick = () => hideSettings();
  $("#saveSettings").onclick = saveSettings;

  $("#globalSearch").oninput = (e) => {
    search = e.target.value;
    if (isHubAddressSearch(search)) {
      e.target.value = "";
      search = "";
    }
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      STATE.offset = 0;
      messageRequestSeq += 1;
      loadAll();
    }, 300);
  };

  // Language change event
  window.addEventListener("languagechange", () => {
    renderAll();
  });

  // ---------- Auto-refresh ----------
  // ★设置弹窗打开时暂停轮询★：否则背后每 3 秒整页重绘，看起来就是“一直闪”
  setInterval(() => {
    const modal = $("#settingsModal");
    const modalOpen = modal && !modal.classList.contains("hidden");
    if (autoRefreshEnabled && !modalOpen) {
      loadAll();
    }
  }, 3000);

  // ---------- Initialize ----------
  async function init() {
    if (appInited) return;
    appInited = true;
    // 自动登录：先拿本地 admin token，再拉公共面（失败仍能看 agents/health）
    if (getAutoLogin()) {
      await tryAutoLogin(true);
    }
    // 浏览器可能恢复上次表单值；Hub 地址不是消息查询条件，启动时清掉。
    clearAccidentalHubSearch();

    if (!getToken()) {
      // 只在这里弹一次；后续 401 走 handleUnauthorized，不再反复开
      maybeShowAuthPrompt(t("auth_failed"));
    }
    loadAll();
  }

  // Wait for i18n to be ready
  if (window.i18n) {
    init();
  } else {
    window.addEventListener("DOMContentLoaded", init);
  }
})();
