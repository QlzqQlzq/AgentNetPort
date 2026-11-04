/* ANP WebUI - Internationalization (i18n) */
(function () {
  "use strict";

  const LANG_KEY = "anp-ui-lang";

  // Centralized language strings
  const translations = {
    "zh-CN": {
      // Navigation & Folders
      folder_inbox: "收件箱",
      folder_sent: "已发送",
      folder_drafts: "草稿箱",
      folder_archive: "归档",
      folder_trash: "废纸篓",
      agents_title: "AGENT 列表",

      // Toolbar & Actions
      search_messages: "搜索消息…",
      compose: "写信",
      nav_back: "后退",
      nav_forward: "前进",
      settings: "设置",
      btn_save: "保存",
      btn_close: "关闭",

      // Message List
      messages_count: "{{count}} 封消息",
      message_count_singular: "{{count}} 封消息",
      no_messages: "没有消息",
      select_message: "选择一封消息查看",
      loading: "加载中…",

      // Message Details
      to_prefix: "收件人：",
      from_prefix: "发件人：",
      no_subject: "（无主题）",
      unknown_sender: "未知发件人",
      attachments_label: "附件",

      // Status
      status_online: "在线",
      status_wakeable: "未在线（可唤醒）",
      status_offline: "离线",
      status_unread: "未读",
      status_read: "已读",
      status_delivered: "已投递",
      status_undelivered: "未投递",
      status_cached: "缓存消息（安静送达）",
      status_broadcast: "📢 广播副本",
      status_replied: "已回复",

      // Settings Modal
      settings_title: "设置",
      settings_hub_url: "Hub 基础地址",
      settings_admin_token: "管理员令牌",
      settings_token_placeholder: "粘贴 admin.token 内容",
      settings_token_hint: "查看所有消息需要管理员令牌，位置在 ~/.agentnetport/admin.token",
      settings_auto_refresh: "每 3 秒自动刷新",
      settings_auto_login: "自动登录",
      settings_auto_login_hint: "从桌面启动时自动读取本地 admin.token（~/.agentnetport/admin.token），无需手动粘贴",
      settings_language: "界面语言",
      btn_verifying: "验证中…",
      err_token_invalid: "管理员令牌不正确。请核对 ~/.agentnetport/admin.token 里的内容后重试。",
      err_hub_unreachable: "无法连接 Hub（{{url}}）。请确认 ANP Hub 已启动，地址与端口正确。",
      err_hub_http: "Hub 返回了异常状态 HTTP {{status}}。",

      // Time
      time_today: "今天",
      time_yesterday: "昨天",
      time_days: ["周日", "周一", "周二", "周三", "周四", "周五", "周六"],

      // Errors & Empty States
      connection_failed: "连接失败，点击设置重新连接",
      auth_failed: "认证失败，请检查管理员令牌",
      core_offline: "Core 离线",
      no_messages_found: "没有符合条件的消息",
      empty_panel: "面板是空的",
      panel_error: "⚠ 面板加载失败：{{error}}",

      // Message metadata
      meta_id: "消息 ID",
      meta_inbox: "收件箱",
      meta_from: "发件人",
      meta_to: "收件人",
      meta_time: "时间",
      meta_reply_to: "回复给",
      meta_correlation_id: "关联 ID",
      meta_delivered_at: "投递时间",
      meta_next_retry: "下次重试",
      meta_last_error: "最近错误",
      meta_signature: "信封签名",
      meta_attempts: "尝试次数",

      // Signature status
      sig_tampered: "⚠ 验签失败（{{reason}}）",
      sig_ok: "✓ 完好",
      sig_unsigned: "无签名（升级前的消息）",
      sig_none: "—"
    },

    "en": {
      // Navigation & Folders
      folder_inbox: "Inbox",
      folder_sent: "Sent",
      folder_drafts: "Drafts",
      folder_archive: "Archive",
      folder_trash: "Trash",
      agents_title: "AGENTS",

      // Toolbar & Actions
      search_messages: "Search messages...",
      compose: "Compose",
      nav_back: "Navigate back",
      nav_forward: "Navigate forward",
      settings: "Settings",
      btn_save: "Save",
      btn_close: "Close",

      // Message List
      messages_count: "{{count}} messages",
      message_count_singular: "{{count}} message",
      no_messages: "No messages",
      select_message: "Select a message to view",
      loading: "Loading...",

      // Message Details
      to_prefix: "to: ",
      from_prefix: "from: ",
      no_subject: "(No subject)",
      unknown_sender: "Unknown",
      attachments_label: "Attachments",

      // Status
      status_online: "Online",
      status_wakeable: "Wakeable (peer not confirmed)",
      status_offline: "Offline",
      status_unread: "Unread",
      status_read: "Read",
      status_delivered: "Delivered",
      status_undelivered: "Undelivered",
      status_cached: "Cached message (silent delivery)",
      status_broadcast: "📢 Broadcast copy",
      status_replied: "Replied",

      // Settings Modal
      settings_title: "Settings",
      settings_hub_url: "Hub Base URL",
      settings_admin_token: "Admin Token",
      settings_token_placeholder: "Paste admin.token content",
      settings_token_hint: "Required to view all messages. Token is in ~/.agentnetport/admin.token",
      settings_auto_refresh: "Auto-refresh every 3 seconds",
      settings_auto_login: "Auto sign-in",
      settings_auto_login_hint: "When launched from the desktop shortcut, load the token from ~/.agentnetport/admin.token automatically",
      settings_language: "Language",
      btn_verifying: "Verifying…",
      err_token_invalid: "Invalid admin token. Check ~/.agentnetport/admin.token and try again.",
      err_hub_unreachable: "Cannot reach hub ({{url}}). Is the ANP Hub running?",
      err_hub_http: "Hub returned unexpected HTTP {{status}}.",

      // Time
      time_today: "Today",
      time_yesterday: "Yesterday",
      time_days: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],

      // Errors & Empty States
      connection_failed: "Connection failed. Click Settings to reconnect.",
      auth_failed: "Authentication failed. Please check your admin token.",
      core_offline: "Core offline",
      no_messages_found: "No messages found",
      empty_panel: "Panel is empty",
      panel_error: "⚠ Panel error: {{error}}",

      // Message metadata
      meta_id: "Message ID",
      meta_inbox: "Inbox",
      meta_from: "From",
      meta_to: "To",
      meta_time: "Time",
      meta_reply_to: "Reply to",
      meta_correlation_id: "Correlation ID",
      meta_delivered_at: "Delivered at",
      meta_next_retry: "Next retry",
      meta_last_error: "Last error",
      meta_signature: "Signature",
      meta_attempts: "Attempts",

      // Signature status
      sig_tampered: "⚠ Verification failed ({{reason}})",
      sig_ok: "✓ Valid",
      sig_unsigned: "Unsigned (pre-upgrade message)",
      sig_none: "—"
    }
  };

  // Detect browser language
  function detectLanguage() {
    const saved = localStorage.getItem(LANG_KEY);
    if (saved && translations[saved]) return saved;

    const browserLang = navigator.language || navigator.userLanguage;
    if (browserLang.startsWith("zh")) return "zh-CN";
    return "en";
  }

  let currentLang = detectLanguage();

  // Get translation
  function t(key, params) {
    let text = translations[currentLang]?.[key] || translations["en"]?.[key] || key;

    // Replace {{variable}} placeholders
    if (params) {
      Object.keys(params).forEach(param => {
        text = text.replace(new RegExp(`{{${param}}}`, "g"), params[param]);
      });
    }

    return text;
  }

  // Update all i18n elements in the page
  function updateUI() {
    // Update text content
    document.querySelectorAll("[data-i18n]").forEach(el => {
      const key = el.getAttribute("data-i18n");
      el.textContent = t(key);
    });

    // Update placeholders
    document.querySelectorAll("[data-i18n-placeholder]").forEach(el => {
      const key = el.getAttribute("data-i18n-placeholder");
      el.placeholder = t(key);
    });

    // Update titles
    document.querySelectorAll("[data-i18n-title]").forEach(el => {
      const key = el.getAttribute("data-i18n-title");
      el.title = t(key);
    });

    // Update language button text
    const langBtn = document.getElementById("langBtnText");
    if (langBtn) {
      langBtn.textContent = currentLang === "zh-CN" ? "EN" : "中";
    }

    // Update HTML lang attribute
    document.documentElement.lang = currentLang;
  }

  // Switch language
  function setLanguage(lang) {
    if (!translations[lang]) return;
    currentLang = lang;
    localStorage.setItem(LANG_KEY, lang);
    updateUI();

    // Trigger custom event for app.js to update dynamic content
    window.dispatchEvent(new CustomEvent("languagechange", { detail: { lang } }));
  }

  // Initialize
  function init() {
    updateUI();

    // Setup language toggle button
    const langBtn = document.getElementById("langBtn");
    if (langBtn) {
      langBtn.onclick = () => {
        const newLang = currentLang === "zh-CN" ? "en" : "zh-CN";
        setLanguage(newLang);
      };
    }
  }

  // Wait for DOM ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Export for use in app.js
  window.i18n = {
    t,
    setLanguage,
    getCurrentLanguage: () => currentLang,
    updateUI
  };
})();
