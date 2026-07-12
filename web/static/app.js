const state = {
  user: null,
  currentView: "chat",
  currentConversation: null,
  conversations: [],
  asking: false,
  codeLanguage: "curl",
  quotaUserId: null,
  quotaUser: null,
  quotaMode: "limit",
  feedbackItem: null,
  emailCodeTimer: null,
  registrationEnabled: true,
};

const viewMeta = {
  chat: ["在线问答", "矿产资源标准规范与相关政策"],
  standards: ["标准目录", "查询知识库当前收录范围"],
  developer: ["开发者", "API Key、调用示例与接口说明"],
  usage: ["配额与用量", "网页与 API 共用账号每日配额"],
  admin: ["管理后台", "邀请码、用户与每日配额"],
};

const viewPaths = {
  chat: "/",
  standards: "/standards",
  developer: "/developer",
  usage: "/usage",
  admin: "/admin",
};

const pathViews = Object.fromEntries(Object.entries(viewPaths).map(([key, value]) => [value, key]));

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function refreshIcons() {
  if (window.lucide?.createIcons) window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeUrl(value) {
  try {
    const url = new URL(value, window.location.origin);
    if (url.protocol === "http:" || url.protocol === "https:") return url.href;
  } catch {
    return "";
  }
  return "";
}

function formatDate(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function displayCount(value) {
  const count = Number(value);
  return Number.isFinite(count) ? String(Math.max(0, Math.trunc(count))) : "0";
}

function showToast(message, type = "success") {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  const icon = document.createElement("i");
  icon.dataset.lucide = type === "error" ? "circle-alert" : "circle-check";
  const text = document.createElement("span");
  text.textContent = message;
  toast.append(icon, text);
  $("#toastRegion").appendChild(toast);
  refreshIcons();
  window.setTimeout(() => toast.remove(), 3800);
}

function errorMessage(payload, fallback = "请求失败") {
  const detail = payload?.detail;
  if (typeof detail === "string") return detail;
  if (detail?.message) return detail.message;
  if (payload?.message) return payload.message;
  return fallback;
}

async function apiRequest(url, options = {}) {
  const { quiet401 = false, ...fetchOptions } = options;
  const headers = new Headers(fetchOptions.headers || {});
  if (fetchOptions.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(url, {
    credentials: "same-origin",
    ...fetchOptions,
    headers,
  });
  let data = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    data = await response.json();
  } else {
    data = await response.text();
  }
  if (!response.ok) {
    const error = new Error(errorMessage(data, `HTTP ${response.status}`));
    error.status = response.status;
    error.payload = data;
    if (response.status === 401 && !quiet401) showAuth("login");
    throw error;
  }
  return data;
}

function setBusy(button, busy, busyText = "处理中") {
  if (!button) return;
  if (!button.dataset.originalHtml) button.dataset.originalHtml = button.innerHTML;
  button.disabled = busy;
  if (busy) {
    button.textContent = busyText;
  } else {
    button.innerHTML = button.dataset.originalHtml;
    refreshIcons();
  }
}

function setAuthMode(mode) {
  const register = mode === "register" && state.registrationEnabled;
  const normalizedMode = register ? "register" : "login";
  $("#loginTab").classList.toggle("active", !register);
  $("#registerTab").classList.toggle("active", register);
  $("#loginTab").setAttribute("aria-selected", String(!register));
  $("#registerTab").setAttribute("aria-selected", String(register));
  $("#loginForm").classList.toggle("hidden", register);
  $("#registerForm").classList.toggle("hidden", !register);
  $("#authTitle").textContent = register ? "邀请码注册" : "登录";
  $("#authSubtitle").textContent = register ? "使用邀请码和邮箱验证码创建账号" : "进入专业问答与开发者控制台";
  $("#authError").textContent = "";
  if (window.location.pathname !== `/${normalizedMode}`) history.replaceState({}, "", `/${normalizedMode}`);
}

function setRegistrationAvailability(enabled) {
  state.registrationEnabled = Boolean(enabled);
  $("#registerTab").classList.toggle("hidden", !state.registrationEnabled);
  $("#authModeControl").classList.toggle("single-option", !state.registrationEnabled);
  $("#authFootnote").textContent = state.registrationEnabled
    ? "注册需同时验证邀请码与邮箱"
    : "当前仅限已有账号登录";
  if (!state.registrationEnabled && !$("#registerForm").classList.contains("hidden")) {
    setAuthMode("login");
  }
}

function showAuth(mode = "login") {
  state.user = null;
  $("#appShell").classList.add("hidden");
  $("#authScreen").classList.remove("hidden");
  setAuthMode(mode);
  closeSidebar();
}

async function showApp(user) {
  state.user = user;
  $("#authScreen").classList.add("hidden");
  $("#appShell").classList.remove("hidden");
  $("#accountName").textContent = user.display_name || user.account;
  $("#accountRole").textContent = user.role === "admin" ? "管理员" : "内测用户";
  $("#accountAvatar").textContent = (user.display_name || user.account || "U").slice(0, 1).toUpperCase();
  $("#adminNavItem").classList.toggle("hidden", user.role !== "admin");
  const requestedView = pathViews[window.location.pathname] || "chat";
  navigate(requestedView === "admin" && user.role !== "admin" ? "chat" : requestedView, false);
  await Promise.allSettled([loadAccountSummary(), loadConversations()]);
  refreshIcons();
}

async function handleLogin(event) {
  event.preventDefault();
  const button = $("#loginButton");
  setBusy(button, true, "登录中");
  $("#authError").textContent = "";
  try {
    const data = await apiRequest("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        account: $("#loginAccount").value.trim(),
        password: $("#loginPassword").value,
      }),
      quiet401: true,
    });
    history.replaceState({}, "", "/");
    await showApp(data.user);
  } catch (error) {
    $("#authError").textContent = error.message;
  } finally {
    setBusy(button, false);
  }
}

async function handleRegister(event) {
  event.preventDefault();
  const button = $("#registerButton");
  setBusy(button, true, "注册中");
  $("#authError").textContent = "";
  try {
    const data = await apiRequest("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({
        email: $("#registerEmail").value.trim(),
        display_name: $("#registerName").value.trim(),
        password: $("#registerPassword").value,
        invite_code: $("#registerInvite").value.trim(),
        email_code: $("#registerEmailCode").value.trim(),
      }),
      quiet401: true,
    });
    history.replaceState({}, "", "/");
    await showApp(data.user);
    showToast("注册成功");
  } catch (error) {
    $("#authError").textContent = error.message;
  } finally {
    setBusy(button, false);
  }
}

function startEmailCodeCooldown(seconds) {
  const button = $("#sendEmailCodeButton");
  if (state.emailCodeTimer) window.clearInterval(state.emailCodeTimer);
  let remaining = Math.max(1, Number(seconds) || 60);
  button.disabled = true;
  const render = () => {
    button.textContent = `${remaining} 秒后重发`;
    remaining -= 1;
    if (remaining < 0) {
      window.clearInterval(state.emailCodeTimer);
      state.emailCodeTimer = null;
      button.disabled = false;
      button.innerHTML = '<i data-lucide="send"></i><span>发送验证码</span>';
      refreshIcons();
    }
  };
  render();
  state.emailCodeTimer = window.setInterval(render, 1000);
}

async function sendEmailCode() {
  const email = $("#registerEmail").value.trim();
  const inviteCode = $("#registerInvite").value.trim();
  if (!email || !inviteCode) {
    $("#authError").textContent = "请先填写邮箱和邀请码。";
    return;
  }
  const button = $("#sendEmailCodeButton");
  setBusy(button, true, "发送中");
  $("#authError").textContent = "";
  try {
    const data = await apiRequest("/api/auth/email-code", {
      method: "POST",
      body: JSON.stringify({ email, invite_code: inviteCode }),
      quiet401: true,
    });
    showToast(data.message || "验证码已发送");
    startEmailCodeCooldown(data.cooldown_seconds || 60);
    $("#registerEmailCode").focus();
  } catch (error) {
    $("#authError").textContent = error.message;
    setBusy(button, false);
  }
}

function navigate(view, push = true) {
  if (!viewMeta[view]) view = "chat";
  if (view === "admin" && state.user?.role !== "admin") view = "chat";
  state.currentView = view;
  $$(".workspace-view").forEach((node) => node.classList.toggle("active", node.id === `${view}View`));
  $$(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.view === view));
  $("#viewTitle").textContent = viewMeta[view][0];
  $("#viewSubtitle").textContent = viewMeta[view][1];
  if (push && window.location.pathname !== viewPaths[view]) history.pushState({}, "", viewPaths[view]);
  closeSidebar();
  if (view === "standards") $("#standardQuery").focus({ preventScroll: true });
  if (view === "developer") loadApiKeys();
  if (view === "usage") loadAccountSummary(true);
  if (view === "admin") loadAdminData();
}

function openSidebar() {
  $("#sidebar").classList.add("open");
  $("#sidebarBackdrop").classList.remove("hidden");
}

function closeSidebar() {
  $("#sidebar").classList.remove("open");
  $("#sidebarBackdrop").classList.add("hidden");
}

function resetChat() {
  state.currentConversation = null;
  $("#messageList").innerHTML = "";
  $("#chatEmptyState").classList.remove("hidden");
  $$(".conversation-item").forEach((node) => node.classList.remove("active"));
  navigate("chat");
  $("#questionInput").focus();
}

async function loadConversations() {
  if (!state.user) return;
  try {
    const data = await apiRequest("/api/conversations");
    state.conversations = data.items || [];
    renderConversations();
  } catch (error) {
    if (error.status !== 401) showToast(error.message, "error");
  }
}

function renderConversations() {
  const container = $("#conversationList");
  container.innerHTML = "";
  if (!state.conversations.length) {
    const empty = document.createElement("p");
    empty.className = "sidebar-empty";
    empty.textContent = "暂无会话";
    container.appendChild(empty);
    return;
  }
  state.conversations.forEach((item) => {
    const row = document.createElement("div");
    row.className = "conversation-item";
    row.classList.toggle("active", item.conversation_id === state.currentConversation);
    const open = document.createElement("button");
    open.className = "conversation-open";
    open.type = "button";
    open.textContent = item.title || "未命名会话";
    open.title = item.title || "未命名会话";
    open.addEventListener("click", () => openConversation(item.conversation_id));
    const remove = document.createElement("button");
    remove.className = "tiny-icon-button conversation-delete";
    remove.type = "button";
    remove.title = "删除会话";
    remove.setAttribute("aria-label", "删除会话");
    remove.innerHTML = '<i data-lucide="trash-2"></i>';
    remove.addEventListener("click", () => deleteConversation(item.conversation_id));
    row.append(open, remove);
    container.appendChild(row);
  });
  refreshIcons();
}

async function openConversation(conversationId) {
  try {
    const data = await apiRequest(`/api/conversations/${encodeURIComponent(conversationId)}`);
    state.currentConversation = conversationId;
    $("#messageList").innerHTML = "";
    $("#chatEmptyState").classList.add("hidden");
    let latestQuestion = null;
    (data.messages || []).forEach((message) => {
      if (message.role === "user") {
        latestQuestion = message.content;
        appendUserMessage(message.content);
      }
      if (message.role === "assistant") {
        appendAssistantMessage({
          answer: message.content,
          request_id: message.request_id,
          session_id: conversationId,
          question: latestQuestion,
          ...(message.metadata || {}),
        });
      }
    });
    renderConversations();
    navigate("chat");
    scrollChatToBottom(false);
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function deleteConversation(conversationId) {
  if (!window.confirm("删除这条会话记录？")) return;
  try {
    await apiRequest(`/api/conversations/${encodeURIComponent(conversationId)}`, { method: "DELETE" });
    if (state.currentConversation === conversationId) resetChat();
    await loadConversations();
  } catch (error) {
    showToast(error.message, "error");
  }
}

function appendUserMessage(question) {
  const node = document.createElement("article");
  node.className = "message user-message";
  const bubble = document.createElement("div");
  bubble.className = "user-bubble";
  bubble.textContent = question;
  node.appendChild(bubble);
  $("#messageList").appendChild(node);
  return node;
}

function appendAssistantLoading() {
  const node = document.createElement("article");
  node.className = "message assistant-message";
  node.innerHTML = `
    <div class="assistant-avatar">矿</div>
    <div class="assistant-body"><div class="typing-indicator" aria-label="正在处理"><span></span><span></span><span></span></div></div>
  `;
  $("#messageList").appendChild(node);
  return node;
}

function renderMarkdown(value) {
  if (window.GeowikiMarkdown?.render) {
    return window.GeowikiMarkdown.render(value, { baseUrl: window.location.origin });
  }
  return `<p>${escapeHtml(value)}</p>`;
}

function renderSources(sources) {
  if (!sources?.length) return '<p class="retrieval-summary">本次回答没有可展示的引用来源。</p>';
  return `<div class="source-list">${sources.map((source) => {
    const href = safeUrl(source.url || "");
    const link = href
      ? `<a class="source-link" href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(source.source_platform || "查看原文")}</a>`
      : "";
    const metadata = [source.standard_no, source.chapter, source.page == null ? null : `第 ${source.page} 页`]
      .filter(Boolean)
      .map((item) => `<span>${escapeHtml(item)}</span>`)
      .join("");
    return `
      <article class="source-item">
        <div class="source-head"><div><div class="source-title">${escapeHtml(source.title || "未知文件")}</div><div class="source-meta">${metadata}</div></div>${link}</div>
        ${source.quote ? `<blockquote class="source-quote">${escapeHtml(source.quote)}</blockquote>` : ""}
      </article>
    `;
  }).join("")}</div>`;
}

function renderEvidence(data) {
  const retrieval = data.retrieval || {};
  const stats = `全文 ${retrieval.full_text_hits || 0} · 向量 ${retrieval.vector_hits || 0} · 图谱 ${retrieval.graph_hits || 0} · 联网 ${retrieval.web_hits || 0}`;
  const limitations = data.limitations?.notes || [];
  return `
    <details class="evidence-details">
      <summary>引用来源与检索信息</summary>
      ${renderSources(data.sources || [])}
      <div class="retrieval-summary">${escapeHtml(stats)}</div>
      ${limitations.length ? `<div class="limitation-list">${limitations.map((item) => `<div>${escapeHtml(item)}</div>`).join("")}</div>` : ""}
    </details>
  `;
}

function quotaLabel(quota) {
  if (!quota) return "";
  const action = quota.consumed ? "本次使用 1 次" : "本次未使用次数";
  return `${action} · 今日剩余 ${displayCount(quota.remaining)} 次`;
}

function appendAssistantMessage(data, existingNode = null) {
  const node = existingNode || document.createElement("article");
  node.className = "message assistant-message";
  node.innerHTML = `
    <div class="assistant-avatar">G</div>
    <div class="assistant-body">
      <div class="answer-content">${renderMarkdown(data.answer || "未返回答案。")}</div>
      <div class="answer-meta">
        <span class="status-chip ${escapeHtml(data.status || "")}">${escapeHtml(statusLabel(data.status))}</span>
        ${data.quota ? `<span>${escapeHtml(quotaLabel(data.quota))}</span>` : ""}
        ${data.request_id ? `<span>请求 ${escapeHtml(data.request_id.slice(0, 12))}</span>` : ""}
      </div>
      ${renderEvidence(data)}
      <div class="message-actions" aria-label="回答反馈">
        <button class="message-action-button feedback-positive" type="button" title="满意" aria-label="满意"><i data-lucide="thumbs-up"></i></button>
        <button class="message-action-button feedback-negative" type="button" title="不满意" aria-label="不满意"><i data-lucide="thumbs-down"></i></button>
      </div>
      <form class="feedback-form hidden">
        <select aria-label="不满意原因">
          <option value="answer_too_vague">回答太笼统</option>
          <option value="wrong_standard">引用标准不对</option>
          <option value="wrong_clause">引用条款不对</option>
          <option value="missing_evidence">证据不足</option>
          <option value="quote_too_long">引用太长</option>
          <option value="format_issue">格式问题</option>
          <option value="other">其他</option>
        </select>
        <input maxlength="500" placeholder="补充说明，可选" />
        <button class="primary-command" type="submit">提交</button>
      </form>
    </div>
  `;
  if (!existingNode) $("#messageList").appendChild(node);
  const positive = $(".feedback-positive", node);
  const negative = $(".feedback-negative", node);
  const form = $(".feedback-form", node);
  positive.addEventListener("click", async () => {
    await submitFeedback(data, "satisfied", null, "");
    positive.classList.add("selected");
    positive.disabled = true;
    negative.disabled = true;
  });
  negative.addEventListener("click", () => {
    form.classList.toggle("hidden");
    if (!form.classList.contains("hidden")) $("select", form).focus();
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = $("button", form);
    setBusy(submit, true, "提交中");
    try {
      await submitFeedback(data, "unsatisfied", $("select", form).value, $("input", form).value);
      negative.classList.add("selected");
      form.classList.add("hidden");
      positive.disabled = true;
      negative.disabled = true;
    } finally {
      setBusy(submit, false);
    }
  });
  refreshIcons();
  return node;
}

function statusLabel(status) {
  const labels = {
    answered: "已回答",
    out_of_scope: "领域外拒答",
    queued_for_enrichment: "已进入补库队列",
    insufficient_evidence: "证据不足",
  };
  return labels[status] || status || "已完成";
}

async function submitFeedback(data, rating, reason, comment) {
  try {
    await apiRequest("/api/feedback", {
      method: "POST",
      body: JSON.stringify({
        session_id: data.session_id || state.currentConversation,
        request_id: data.request_id || null,
        rating,
        reason,
        comment: comment.trim() || null,
        question: data.question || null,
      }),
    });
    showToast(rating === "satisfied" ? "已记录满意反馈" : "已记录问题反馈");
  } catch (error) {
    showToast(error.message, "error");
    throw error;
  }
}

function scrollChatToBottom(smooth = true) {
  const container = $("#chatScroll");
  container.scrollTo({ top: container.scrollHeight, behavior: smooth ? "smooth" : "auto" });
}

function resizeComposer() {
  const textarea = $("#questionInput");
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
}

async function submitQuestion(event) {
  event.preventDefault();
  if (state.asking) return;
  const input = $("#questionInput");
  const question = input.value.trim();
  if (!question) return;
  state.asking = true;
  $("#chatEmptyState").classList.add("hidden");
  appendUserMessage(question);
  const pending = appendAssistantLoading();
  input.value = "";
  resizeComposer();
  $("#askButton").disabled = true;
  scrollChatToBottom();
  try {
    const data = await apiRequest("/api/ask", {
      method: "POST",
      body: JSON.stringify({
        question,
        session_id: state.currentConversation,
      }),
    });
    data.question = question;
    state.currentConversation = data.session_id;
    appendAssistantMessage(data, pending);
    if (data.quota) updateQuota(data.quota);
    await Promise.allSettled([loadConversations(), loadAccountSummary()]);
  } catch (error) {
    appendAssistantMessage({
      answer: `请求失败：${error.message}`,
      status: "system_error",
      session_id: state.currentConversation,
      sources: [],
      limitations: { notes: [] },
      quota: error.payload?.detail?.quota || null,
    }, pending);
    showToast(error.message, "error");
  } finally {
    state.asking = false;
    $("#askButton").disabled = false;
    input.focus();
    scrollChatToBottom();
  }
}

function updateQuota(quota) {
  if (!quota) return;
  $("#topQuota").textContent = `${displayCount(quota.remaining)} 次`;
  if (state.user) state.user.quota = quota;
}

async function loadAccountSummary(renderUsage = false) {
  if (!state.user) return;
  try {
    const data = await apiRequest("/api/account/summary");
    updateQuota(data.quota);
    if (renderUsage || state.currentView === "usage") renderAccountSummary(data);
  } catch (error) {
    if (error.status !== 401) showToast(error.message, "error");
  }
}

function renderAccountSummary(data) {
  const quota = data.quota || {};
  $("#usageRemaining").textContent = displayCount(quota.remaining);
  $("#usageUsed").textContent = displayCount(quota.used);
  $("#usageEffectiveLimit").textContent = displayCount(quota.effective_limit);
  $("#usageTotalCalls").textContent = String(data.total_calls ?? 0);
  const body = $("#quotaAdjustmentTableBody");
  body.innerHTML = "";
  const entries = data.adjustments || [];
  if (!entries.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty-row">暂无配额调整记录。</td></tr>';
    return;
  }
  entries.forEach((entry) => {
    const row = document.createElement("tr");
    const positive = Number(entry.delta_count) >= 0;
    row.innerHTML = `
      <td>${escapeHtml(formatDate(entry.created_at))}</td>
      <td>${escapeHtml(quotaAdjustmentLabel(entry.adjustment_type))}</td>
      <td>${escapeHtml(entry.usage_date || "--")}</td>
      <td>${escapeHtml(entry.reason || "--")}</td>
      <td class="${positive ? "amount-positive" : "amount-negative"}">${positive ? "+" : ""}${escapeHtml(entry.delta_count)}</td>
    `;
    body.appendChild(row);
  });
}

function quotaAdjustmentLabel(type) {
  return {
    daily_limit_change: "每日上限",
    daily_bonus: "当日追加",
  }[type] || type;
}

async function searchStandards(event) {
  event.preventDefault();
  const params = new URLSearchParams();
  const q = $("#standardQuery").value.trim();
  const standardNo = $("#standardNo").value.trim();
  const textAccess = $("#textAccess").value;
  if (q) params.set("q", q);
  if (standardNo) params.set("standard_no", standardNo);
  if (textAccess) params.set("text_access", textAccess);
  params.set("page_size", "50");
  $("#standardsStatus").textContent = "正在查询...";
  $("#standardsList").innerHTML = "";
  try {
    const data = await apiRequest(`/api/standards?${params.toString()}`);
    renderStandards(data.items || [], data.pagination?.total);
  } catch (error) {
    $("#standardsStatus").textContent = error.message;
    showToast(error.message, "error");
  }
}

function renderStandards(items, total) {
  const container = $("#standardsList");
  container.innerHTML = "";
  $("#standardsStatus").textContent = items.length ? `查询到 ${total ?? items.length} 条记录` : "未查询到符合条件的标准。";
  items.forEach((item) => {
    const row = document.createElement("article");
    row.className = "standard-row";
    const href = safeUrl(item.url || "");
    row.innerHTML = `
      <div class="standard-main"><strong>${escapeHtml(item.title || "未知标准")}</strong><span>${escapeHtml(item.standard_no || "无标准号")}</span></div>
      <div class="standard-cell">${escapeHtml(item.document_type || "--")}</div>
      <div class="standard-cell">${escapeHtml(item.status || "状态未知")}</div>
      <div class="standard-cell">${escapeHtml(item.text_access || "--")}</div>
      <div class="standard-cell">${href ? `<a class="source-link" href="${escapeHtml(href)}" target="_blank" rel="noreferrer">官方原文</a>` : `<span class="availability ${item.can_answer ? "" : "unavailable"}">${item.can_answer ? "可问答" : "暂无链接"}</span>`}</div>
    `;
    container.appendChild(row);
  });
}

async function loadApiKeys() {
  if (!state.user) return;
  try {
    const data = await apiRequest("/api/account/api-keys");
    renderApiKeys(data.items || []);
  } catch (error) {
    if (error.status !== 401) showToast(error.message, "error");
  }
}

function renderApiKeys(items) {
  const container = $("#apiKeyList");
  container.innerHTML = "";
  if (!items.length) {
    container.innerHTML = '<p class="empty-row">尚未创建 API Key。</p>';
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("article");
    row.className = "key-item";
    const revoked = Boolean(item.revoked_at) || Number(item.enabled) === 0;
    row.innerHTML = `
      <div><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.key_prefix)}•••• · 创建于 ${escapeHtml(formatDate(item.created_at))}${item.last_used_at ? ` · 最近使用 ${escapeHtml(formatDate(item.last_used_at))}` : ""}</span></div>
      ${revoked ? '<span class="availability unavailable">已吊销</span>' : '<button class="danger-text-button" type="button">吊销</button>'}
    `;
    if (!revoked) {
      $("button", row).addEventListener("click", () => revokeKey(item.api_key_id, item.name));
    }
    container.appendChild(row);
  });
}

function openKeyDialog() {
  $("#keyDialogTitle").textContent = "创建 API Key";
  $("#keyCreateForm").classList.remove("hidden");
  $("#keySecretPanel").classList.add("hidden");
  $("#confirmCreateKeyButton").classList.remove("hidden");
  $("#keyNameInput").value = "";
  $("#newKeyValue").textContent = "";
  $("#keyDialog").showModal();
  $("#keyNameInput").focus();
}

async function createKey() {
  const name = $("#keyNameInput").value.trim();
  if (!name) {
    showToast("请输入密钥名称", "error");
    return;
  }
  const button = $("#confirmCreateKeyButton");
  setBusy(button, true, "创建中");
  try {
    const data = await apiRequest("/api/account/api-keys", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    $("#keyDialogTitle").textContent = "API Key 已创建";
    $("#keyCreateForm").classList.add("hidden");
    $("#keySecretPanel").classList.remove("hidden");
    $("#confirmCreateKeyButton").classList.add("hidden");
    $("#newKeyValue").textContent = data.api_key;
    await loadApiKeys();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function revokeKey(apiKeyId, name) {
  if (!window.confirm(`吊销 API Key“${name}”？吊销后立即失效。`)) return;
  try {
    await apiRequest(`/api/account/api-keys/${encodeURIComponent(apiKeyId)}`, { method: "DELETE" });
    showToast("API Key 已吊销");
    await loadApiKeys();
  } catch (error) {
    showToast(error.message, "error");
  }
}

function quickstartExamples() {
  const base = window.location.origin;
  return {
    curl: `curl -X POST "${base}/api/ask" \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: YOUR_API_KEY" \\
  -d '{
    "question": "金矿勘查Ⅰ类型的推荐工程间距是多少？"
  }'`,
    python: `import requests

response = requests.post(
    "${base}/api/ask",
    headers={"X-API-Key": "YOUR_API_KEY"},
    json={"question": "金矿勘查Ⅰ类型的推荐工程间距是多少？"},
    timeout=90,
)
response.raise_for_status()
data = response.json()

print(data["answer"])
print(data["quota"])`,
    javascript: `const response = await fetch("${base}/api/ask", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-API-Key": "YOUR_API_KEY",
  },
  body: JSON.stringify({
    question: "金矿勘查Ⅰ类型的推荐工程间距是多少？",
  }),
});

if (!response.ok) throw new Error(\`HTTP \${response.status}\`);
const data = await response.json();
console.log(data.answer, data.quota);`,
  };
}

function updateQuickstart(language) {
  state.codeLanguage = language;
  $$(".code-tab").forEach((button) => button.classList.toggle("active", button.dataset.language === language));
  $("#quickstartCode").textContent = quickstartExamples()[language];
}

function legacyCopyText(value, sourceElement = null) {
  const activeElement = document.activeElement;
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.position = "fixed";
  textarea.style.top = "0";
  textarea.style.left = "0";
  textarea.style.width = "2px";
  textarea.style.height = "2px";
  textarea.style.opacity = "0.01";
  textarea.style.zIndex = "-1";
  document.body.appendChild(textarea);

  let copied = false;
  const handleCopy = (event) => {
    if (!event.clipboardData) return;
    event.clipboardData.setData("text/plain", value);
    event.preventDefault();
  };
  document.addEventListener("copy", handleCopy);
  try {
    textarea.focus({ preventScroll: true });
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    copied = typeof document.execCommand === "function" && document.execCommand("copy") === true;
    if (!copied && sourceElement) {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(sourceElement);
      selection.removeAllRanges();
      selection.addRange(range);
      copied = document.execCommand("copy") === true;
      selection.removeAllRanges();
    }
  } catch {
    copied = false;
  } finally {
    document.removeEventListener("copy", handleCopy);
    textarea.remove();
    if (activeElement instanceof HTMLElement) activeElement.focus({ preventScroll: true });
  }
  return copied;
}

function markCopied(button) {
  if (!button) return;
  button.classList.add("copied");
  const previousTitle = button.title;
  const previousLabel = button.getAttribute("aria-label");
  button.title = "已复制";
  button.setAttribute("aria-label", "已复制");
  window.setTimeout(() => {
    button.classList.remove("copied");
    button.title = previousTitle;
    if (previousLabel === null) button.removeAttribute("aria-label");
    else button.setAttribute("aria-label", previousLabel);
  }, 1800);
}

async function copyText(value, options = {}) {
  const text = String(value ?? "").trim();
  if (!text) {
    showToast("没有可复制的内容", "error");
    return false;
  }

  let copied = false;
  let copyMethod = "none";
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
      copyMethod = "clipboard";
    } catch {
      copied = false;
    }
  }
  if (!copied) {
    copied = legacyCopyText(text, options.sourceElement || null);
    if (copied) copyMethod = "legacy";
  }
  if (copied) {
    if (options.button) options.button.dataset.copyMethod = copyMethod;
    markCopied(options.button || null);
    showToast("已复制到剪贴板");
    return true;
  }

  if (options.sourceElement) {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(options.sourceElement);
    selection.removeAllRanges();
    selection.addRange(range);
  }
  showToast("浏览器阻止了剪贴板访问，内容已选中，请按 Ctrl+C", "error");
  return false;
}

async function loadAdminData() {
  if (state.user?.role !== "admin") return;
  try {
    const [invitations, users, feedback] = await Promise.all([
      apiRequest("/api/admin/invitations"),
      apiRequest("/api/admin/users"),
      apiRequest("/api/admin/feedback"),
    ]);
    renderInvitations(invitations.items || []);
    renderUsers(users.items || []);
    renderFeedback(feedback.items || []);
  } catch (error) {
    showToast(error.message, "error");
  }
}

function feedbackReasonLabel(reason) {
  return {
    wrong_standard: "引用标准不对",
    wrong_clause: "引用条款不对",
    missing_evidence: "证据不足",
    quote_too_long: "引用太长",
    answer_too_vague: "回答太笼统",
    format_issue: "格式问题",
    other: "其他",
  }[reason] || (reason ? reason : "满意");
}

function feedbackLaneLabel(lane) {
  return {
    no_action: "无需处理",
    product: "程序优化",
    kb_review: "知识库审核",
    manual_review: "人工判断",
  }[lane] || lane || "--";
}

function feedbackStatusLabel(status) {
  return {
    open: "待处理",
    in_progress: "处理中",
    kb_review: "待 KB 审核",
    resolved: "已解决",
    dismissed: "已忽略",
    closed: "已关闭",
  }[status] || status || "--";
}

function renderFeedback(items) {
  const body = $("#feedbackTableBody");
  body.innerHTML = "";
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty-row">暂无用户反馈。</td></tr>';
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("tr");
    const open = ["open", "in_progress", "kb_review"].includes(item.status);
    row.innerHTML = `
      <td>${escapeHtml(formatDate(item.created_at))}</td>
      <td class="feedback-question-cell" title="${escapeHtml(item.question || "")}">${escapeHtml(item.question || "未关联问题")}</td>
      <td>${escapeHtml(feedbackReasonLabel(item.reason))}${item.comment ? `<span class="table-subtext">${escapeHtml(item.comment)}</span>` : ""}</td>
      <td>${escapeHtml(feedbackLaneLabel(item.review_lane))}</td>
      <td>${escapeHtml(item.display_name || item.account || "API 用户")}</td>
      <td><span class="status-chip ${open ? "queued_for_enrichment" : "answered"}">${escapeHtml(feedbackStatusLabel(item.status))}</span></td>
      <td><button class="table-action feedback-action" type="button">处理</button></td>
    `;
    $(".feedback-action", row).addEventListener("click", () => openFeedbackDialog(item));
    body.appendChild(row);
  });
}

function openFeedbackDialog(item) {
  state.feedbackItem = item;
  $("#feedbackDialogQuestion").textContent = item.question || "未关联原始问题";
  $("#feedbackStatusInput").value = item.status === "closed" ? "resolved" : item.status;
  $("#feedbackResolutionInput").value = item.resolution_note || "";
  $("#feedbackDialog").showModal();
  $("#feedbackStatusInput").focus();
}

async function updateFeedbackStatus() {
  if (!state.feedbackItem) return;
  const button = $("#confirmFeedbackButton");
  setBusy(button, true, "保存中");
  try {
    await apiRequest(`/api/admin/feedback/${encodeURIComponent(state.feedbackItem.feedback_id)}/status`, {
      method: "POST",
      body: JSON.stringify({
        status: $("#feedbackStatusInput").value,
        resolution_note: $("#feedbackResolutionInput").value.trim() || null,
      }),
    });
    $("#feedbackDialog").close();
    state.feedbackItem = null;
    showToast("反馈状态已更新");
    await loadAdminData();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

function renderInvitations(items) {
  const body = $("#invitationTableBody");
  body.innerHTML = "";
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty-row">尚未创建邀请码。</td></tr>';
    return;
  }
  items.forEach((item) => {
    const exhausted = item.used_count >= item.max_uses;
    const expired = item.expires_at && new Date(item.expires_at) <= new Date();
    const active = item.enabled && !exhausted && !expired;
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${escapeHtml(item.label)}</td>
      <td><code>${escapeHtml(item.code_prefix)}••••</code></td>
      <td>${item.used_count} / ${item.max_uses}</td>
      <td>${escapeHtml(formatDate(item.expires_at))}</td>
      <td><span class="status-chip ${active ? "answered" : "out_of_scope"}">${active ? "可用" : "不可用"}</span></td>
    `;
    body.appendChild(row);
  });
}

function renderUsers(items) {
  const body = $("#userTableBody");
  body.innerHTML = "";
  items.forEach((item) => {
    const row = document.createElement("tr");
    const isSelf = item.user_id === state.user.user_id;
    const quota = item.quota || {};
    row.innerHTML = `
      <td><div class="user-cell"><span class="user-cell-avatar">${escapeHtml((item.display_name || item.account).slice(0, 1).toUpperCase())}</span><div><strong>${escapeHtml(item.display_name)}</strong><span>${escapeHtml(item.account)} · ${item.role === "admin" ? "管理员" : "用户"}</span></div></div></td>
      <td>${displayCount(quota.daily_limit ?? item.daily_limit)}</td>
      <td>${displayCount(quota.used)}</td>
      <td>${displayCount(quota.bonus)}</td>
      <td>${displayCount(quota.remaining)}</td>
      <td><span class="status-chip ${item.status === "active" ? "answered" : "out_of_scope"}">${item.status === "active" ? "正常" : "已停用"}</span></td>
      <td><div class="table-actions"><button class="table-action quota-action" type="button">调配额</button>${isSelf ? "" : `<button class="table-action ${item.status === "active" ? "danger" : ""} status-action" type="button">${item.status === "active" ? "停用" : "恢复"}</button>`}</div></td>
    `;
    $(".quota-action", row).addEventListener("click", () => openQuotaDialog(item));
    const statusButton = $(".status-action", row);
    if (statusButton) statusButton.addEventListener("click", () => setUserStatus(item));
    body.appendChild(row);
  });
}

function openInviteDialog() {
  $("#inviteDialogTitle").textContent = "创建邀请码";
  $("#inviteCreateForm").classList.remove("hidden");
  $("#inviteSecretPanel").classList.add("hidden");
  $("#confirmCreateInviteButton").classList.remove("hidden");
  $("#inviteLabelInput").value = "";
  $("#inviteMaxUsesInput").value = "1";
  $("#inviteDaysInput").value = "30";
  $("#newInviteValue").textContent = "";
  $("#inviteDialog").showModal();
  $("#inviteLabelInput").focus();
}

async function createInvitation() {
  const label = $("#inviteLabelInput").value.trim();
  if (!label) {
    showToast("请输入邀请码标记", "error");
    return;
  }
  const button = $("#confirmCreateInviteButton");
  setBusy(button, true, "创建中");
  try {
    const data = await apiRequest("/api/admin/invitations", {
      method: "POST",
      body: JSON.stringify({
        label,
        max_uses: Number($("#inviteMaxUsesInput").value),
        expires_in_days: Number($("#inviteDaysInput").value),
      }),
    });
    $("#inviteDialogTitle").textContent = "邀请码已创建";
    $("#inviteCreateForm").classList.add("hidden");
    $("#inviteSecretPanel").classList.remove("hidden");
    $("#confirmCreateInviteButton").classList.add("hidden");
    $("#newInviteValue").textContent = data.invite_code;
    await loadAdminData();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

function setQuotaMode(mode, user = null) {
  state.quotaMode = mode === "bonus" ? "bonus" : "limit";
  $$(".quota-mode").forEach((button) => button.classList.toggle("active", button.dataset.mode === state.quotaMode));
  const input = $("#quotaValueInput");
  if (state.quotaMode === "limit") {
    $("#quotaValueLabel").textContent = "新的每日上限";
    input.placeholder = "10";
    input.value = user ? String(user.daily_limit ?? user.quota?.daily_limit ?? 10) : "";
  } else {
    $("#quotaValueLabel").textContent = "增加今日次数";
    input.placeholder = "5";
    input.value = "";
  }
  input.focus();
}

function openQuotaDialog(user) {
  state.quotaUserId = user.user_id;
  state.quotaUser = user;
  const quota = user.quota || {};
  $("#quotaTargetName").textContent = `${user.display_name}（${user.account}），今日已用 ${displayCount(quota.used)} 次，剩余 ${displayCount(quota.remaining)} 次`;
  $("#quotaReasonInput").value = "";
  $("#quotaDialog").showModal();
  setQuotaMode("limit", user);
}

async function adjustQuota() {
  const value = Number($("#quotaValueInput").value);
  const reason = $("#quotaReasonInput").value.trim();
  if (!Number.isInteger(value) || value <= 0 || !reason) {
    showToast("请输入正整数和调整原因", "error");
    return;
  }
  const button = $("#confirmQuotaButton");
  setBusy(button, true, "保存中");
  try {
    const path = state.quotaMode === "limit"
      ? `/api/admin/users/${encodeURIComponent(state.quotaUserId)}/daily-limit`
      : `/api/admin/users/${encodeURIComponent(state.quotaUserId)}/quota`;
    const payload = state.quotaMode === "limit"
      ? { daily_limit: value, reason }
      : { extra_requests: value, reason };
    await apiRequest(path, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    $("#quotaDialog").close();
    showToast(state.quotaMode === "limit" ? "每日上限已更新" : "今日次数已增加");
    await Promise.allSettled([loadAdminData(), loadAccountSummary()]);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function setUserStatus(user) {
  const nextStatus = user.status === "active" ? "suspended" : "active";
  const label = nextStatus === "active" ? "恢复" : "停用";
  if (!window.confirm(`${label}用户“${user.display_name}”？`)) return;
  try {
    await apiRequest(`/api/admin/users/${encodeURIComponent(user.user_id)}/status`, {
      method: "POST",
      body: JSON.stringify({ status: nextStatus }),
    });
    showToast(`用户已${label}`);
    await loadAdminData();
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function changePassword() {
  const currentPassword = $("#currentPasswordInput").value;
  const newPassword = $("#newPasswordInput").value;
  if (newPassword.length < 8) {
    showToast("新密码至少需要 8 位", "error");
    return;
  }
  const button = $("#confirmPasswordButton");
  setBusy(button, true, "保存中");
  try {
    await apiRequest("/api/account/password", {
      method: "POST",
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    });
    $("#passwordDialog").close();
    $("#currentPasswordInput").value = "";
    $("#newPasswordInput").value = "";
    showToast("密码已更新，其他登录会话已退出");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function logout() {
  try {
    await apiRequest("/api/auth/logout", { method: "POST" });
  } finally {
    history.replaceState({}, "", "/login");
    showAuth("login");
  }
}

function bindEvents() {
  $("#loginTab").addEventListener("click", () => setAuthMode("login"));
  $("#registerTab").addEventListener("click", () => setAuthMode("register"));
  $("#loginForm").addEventListener("submit", handleLogin);
  $("#registerForm").addEventListener("submit", handleRegister);
  $("#sendEmailCodeButton").addEventListener("click", sendEmailCode);
  $("#askForm").addEventListener("submit", submitQuestion);
  $("#questionInput").addEventListener("input", resizeComposer);
  $("#questionInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("#askForm").requestSubmit();
    }
  });
  $("#standardsForm").addEventListener("submit", searchStandards);
  $("#newChatButton").addEventListener("click", resetChat);
  $("#refreshConversationsButton").addEventListener("click", loadConversations);
  $("#quotaButton").addEventListener("click", () => navigate("usage"));
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
  $$(".suggestion").forEach((button) => button.addEventListener("click", () => {
    $("#questionInput").value = button.dataset.question;
    resizeComposer();
    $("#askForm").requestSubmit();
  }));
  $("#openSidebarButton").addEventListener("click", openSidebar);
  $("#closeSidebarButton").addEventListener("click", closeSidebar);
  $("#sidebarBackdrop").addEventListener("click", closeSidebar);

  $("#accountMenuButton").addEventListener("click", () => {
    const menu = $("#accountMenu");
    const expanded = !menu.classList.toggle("hidden");
    $("#accountMenuButton").setAttribute("aria-expanded", String(expanded));
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".sidebar-account")) {
      $("#accountMenu").classList.add("hidden");
      $("#accountMenuButton").setAttribute("aria-expanded", "false");
    }
  });
  $("#logoutButton").addEventListener("click", logout);
  $("#changePasswordButton").addEventListener("click", () => $("#passwordDialog").showModal());
  $("#confirmPasswordButton").addEventListener("click", changePassword);

  $("#createKeyButton").addEventListener("click", openKeyDialog);
  $("#confirmCreateKeyButton").addEventListener("click", createKey);
  $("#copyKeyButton").addEventListener("click", () => copyText($("#newKeyValue").textContent, {
    button: $("#copyKeyButton"),
    sourceElement: $("#newKeyValue"),
  }));
  $$(".code-tab").forEach((button) => button.addEventListener("click", () => updateQuickstart(button.dataset.language)));
  $("#copyCodeButton").addEventListener("click", () => copyText($("#quickstartCode").textContent, {
    button: $("#copyCodeButton"),
    sourceElement: $("#quickstartCode"),
  }));

  $("#createInviteButton").addEventListener("click", openInviteDialog);
  $("#confirmCreateInviteButton").addEventListener("click", createInvitation);
  $("#copyInviteButton").addEventListener("click", () => copyText($("#newInviteValue").textContent, {
    button: $("#copyInviteButton"),
    sourceElement: $("#newInviteValue"),
  }));
  $$(".quota-mode").forEach((button) => button.addEventListener("click", () => setQuotaMode(button.dataset.mode, state.quotaUser)));
  $("#confirmQuotaButton").addEventListener("click", adjustQuota);
  $("#confirmFeedbackButton").addEventListener("click", updateFeedbackStatus);

  window.addEventListener("popstate", () => {
    if (!state.user) {
      setAuthMode(window.location.pathname === "/register" ? "register" : "login");
      return;
    }
    navigate(pathViews[window.location.pathname] || "chat", false);
  });
}

async function initialize() {
  bindEvents();
  updateQuickstart("curl");
  refreshIcons();
  try {
    const data = await apiRequest("/api/auth/me", { quiet401: true });
    setRegistrationAvailability(data.registration_enabled !== false);
    if (data.authenticated && data.user) {
      await showApp(data.user);
    } else {
      showAuth(window.location.pathname === "/register" ? "register" : "login");
    }
  } catch {
    showAuth(window.location.pathname === "/register" ? "register" : "login");
  }
}

initialize();
