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
  qaMode: "basic",
  activeResearchTask: null,
  pendingClarification: null,
  lexiconData: null,
  lexiconCandidate: null,
  lexiconStatusEntry: null,
};

const qaModes = {
  basic: {
    title: "基本模式 · 快速查证",
    description: "查询明确条款、定义、数值、材料、办理依据和官方来源。",
    cost: "本次 1 次",
    note: "回答仅引用必要条款片段；正式业务决策请核验官方原文。",
    placeholder: "输入需要快速查证的标准条款、定义、数值或政策问题",
  },
  deep: {
    title: "深度模式 · 综合研究",
    description: "跨标准汇总、逐项对比、差异检查和复杂条件分析；任务会逐份检索候选文件。",
    cost: "本次 3 次",
    note: "预计耗时较长；知识库正文不足时会明确说明证据边界。",
    placeholder: "输入需要跨文件研究、完整性核验或复杂比较的问题",
  },
};

const viewMeta = {
  chat: ["在线问答", "矿产资源标准规范与相关政策"],
  standards: ["标准目录", "查询知识库当前收录范围"],
  developer: ["开发者", "API Key、调用示例与接口说明"],
  usage: ["配额与用量", "网页与 API 共用账号每日配额"],
  admin: ["管理后台", "邀请码、用户与每日配额"],
  lexicon: ["领域词典", "候选审核、运行时规则与版本记录"],
};

const viewPaths = {
  chat: "/",
  standards: "/standards",
  developer: "/developer",
  usage: "/usage",
  admin: "/admin",
  lexicon: "/admin/lexicon",
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

function setQAMode(mode, persist = true) {
  const normalized = mode === "deep" ? "deep" : "basic";
  state.qaMode = normalized;
  const config = qaModes[normalized];
  $$("#qaModeControl .segment").forEach((button) => {
    const active = button.dataset.mode === normalized;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $("#modeTitle").textContent = config.title;
  $("#modeDescription").textContent = config.description;
  $("#modeCost").textContent = config.cost;
  $("#composerNote").textContent = config.note;
  $("#questionInput").placeholder = config.placeholder;
  if (persist) {
    try {
      window.localStorage.setItem("geowiki.qaMode", normalized);
    } catch {
      // Mode selection still works when storage is unavailable.
    }
  }
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
  $("#lexiconNavItem").classList.toggle("hidden", user.role !== "admin");
  const requestedView = pathViews[window.location.pathname] || "chat";
  navigate(["admin", "lexicon"].includes(requestedView) && user.role !== "admin" ? "chat" : requestedView, false);
  await Promise.allSettled([loadAccountSummary(), loadConversations()]);
  void resumeStoredResearchTask();
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
  if (["admin", "lexicon"].includes(view) && state.user?.role !== "admin") view = "chat";
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
  if (view === "lexicon") loadLexiconData();
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

function appendResearchProgress() {
  const node = document.createElement("article");
  node.className = "message assistant-message research-message";
  node.innerHTML = `
    <div class="assistant-avatar">G</div>
    <div class="assistant-body">
      <div class="research-progress-head"><strong>深度研究已进入队列</strong><span>0%</span></div>
      <div class="research-progress-track" aria-label="深度研究进度"><span style="width:0%"></span></div>
      <p class="research-progress-message">正在等待研究工作器。</p>
      <div class="research-coverage"><span>已审查 0/0</span><span>证据覆盖 0</span></div>
    </div>
  `;
  $("#messageList").appendChild(node);
  return node;
}

function updateResearchProgress(node, task) {
  const progress = task.progress || {};
  const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
  $(".research-progress-head strong", node).textContent = researchStageLabel(task.status || progress.stage);
  $(".research-progress-head span", node).textContent = `${percent}%`;
  $(".research-progress-track span", node).style.width = `${percent}%`;
  $(".research-progress-message", node).textContent = progress.message || "正在处理深度研究任务。";
  const coverage = $(".research-coverage", node);
  coverage.innerHTML = `<span>已审查 ${displayCount(progress.examined_documents)}/${displayCount(progress.total_documents)}</span><span>证据覆盖 ${displayCount(progress.evidence_documents)}</span>`;
}

function researchStageLabel(status) {
  return {
    queued: "深度研究已进入队列",
    planning: "正在制定研究计划",
    retrieving: "正在逐份检索候选文件",
    analyzing: "正在提取事实并比较",
    completed: "深度研究已完成",
    partial: "深度研究已完成，覆盖不完整",
    insufficient_evidence: "深度研究证据不足",
    failed: "深度研究执行失败",
    cancelled: "深度研究已取消",
  }[status] || "深度研究处理中";
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
  const coverage = data.coverage
    ? `已审查 ${displayCount(data.coverage.examined_documents)}/${displayCount(data.coverage.total_documents)} 份 · 证据覆盖 ${displayCount(data.coverage.evidence_documents)} 份${data.coverage.knowledge_snapshot ? ` · 快照 ${escapeHtml(data.coverage.knowledge_snapshot)}` : ""}`
    : "";
  return `
    <details class="evidence-details">
      <summary>引用来源与检索信息</summary>
      ${renderSources(data.sources || [])}
      <div class="retrieval-summary">${escapeHtml(stats)}</div>
      ${coverage ? `<div class="retrieval-summary">${coverage}</div>` : ""}
      ${limitations.length ? `<div class="limitation-list">${limitations.map((item) => `<div>${escapeHtml(item)}</div>`).join("")}</div>` : ""}
    </details>
  `;
}

function quotaLabel(quota) {
  if (!quota) return "";
  const units = Number(quota.consumed_units ?? (quota.consumed ? 1 : 0));
  const action = units > 0 ? `本次使用 ${displayCount(units)} 次` : "本次未使用次数";
  return `${action} · 今日剩余 ${displayCount(quota.remaining)} 次`;
}

function renderClarification(data) {
  const clarification = data.clarification;
  if (data.status !== "clarification_required" || !clarification?.options?.length) return "";
  return `
    <div class="clarification-panel">
      <div class="clarification-options">
        ${clarification.options.map((option) => `
          <button class="clarification-option" type="button" data-option-id="${escapeHtml(option.option_id)}" data-question="${escapeHtml(option.question)}">
            <strong>${escapeHtml(option.label)}</strong>
            ${option.description ? `<span>${escapeHtml(option.description)}</span>` : ""}
          </button>
        `).join("")}
      </div>
      ${clarification.allow_free_text ? '<p>以上方向都不准确时，可以在输入框中直接补充你的实际需求。</p>' : ""}
    </div>
  `;
}

function appendAssistantMessage(data, existingNode = null) {
  const isClarification = data.status === "clarification_required";
  const node = existingNode || document.createElement("article");
  node.className = "message assistant-message";
  node.innerHTML = `
    <div class="assistant-avatar">G</div>
    <div class="assistant-body">
      <div class="answer-content">${renderMarkdown(data.answer || "未返回答案。")}</div>
      ${renderClarification(data)}
      <div class="answer-meta">
        <span class="status-chip ${escapeHtml(data.status || "")}">${escapeHtml(statusLabel(data.status))}</span>
        <span class="mode-chip">${data.mode === "deep" ? "深度模式" : "基本模式"}</span>
        ${data.quota ? `<span>${escapeHtml(quotaLabel(data.quota))}</span>` : ""}
        ${data.request_id ? `<span>请求 ${escapeHtml(data.request_id.slice(0, 12))}</span>` : ""}
      </div>
      ${isClarification ? "" : renderEvidence(data)}
      ${isClarification ? "" : `<div class="message-actions" aria-label="回答反馈">
        <button class="message-action-button feedback-positive" type="button" title="满意" aria-label="满意"><i data-lucide="thumbs-up"></i></button>
        <button class="message-action-button feedback-negative" type="button" title="不满意" aria-label="不满意"><i data-lucide="thumbs-down"></i></button>
        ${data.mode !== "deep" && data.mode_recommendation === "deep" && data.question ? '<button class="deep-upgrade-button" type="button"><i data-lucide="microscope"></i><span>转深度研究 · 追加 2 次</span></button>' : ""}
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
      </form>`}
    </div>
  `;
  if (!existingNode) $("#messageList").appendChild(node);
  if (isClarification) {
    $$(".clarification-option", node).forEach((button) => {
      button.addEventListener("click", () => {
        $$(".clarification-option", node).forEach((item) => { item.disabled = true; });
        setQAMode(data.mode === "deep" ? "deep" : "basic");
        state.pendingClarification = {
          clarificationId: data.clarification?.clarification_id || null,
          optionId: button.dataset.optionId || null,
          mode: data.mode === "deep" ? "deep" : "basic",
        };
        const input = $("#questionInput");
        input.value = button.dataset.question || "";
        resizeComposer();
        $("#askForm").requestSubmit();
      });
    });
    refreshIcons();
    return node;
  }
  const positive = $(".feedback-positive", node);
  const negative = $(".feedback-negative", node);
  const form = $(".feedback-form", node);
  const deepUpgrade = $(".deep-upgrade-button", node);
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
  if (deepUpgrade) {
    deepUpgrade.addEventListener("click", async () => {
      if (!window.confirm("将按同一问题创建深度研究任务，并追加消耗 2 次配额。继续？")) return;
      deepUpgrade.disabled = true;
      const started = await startDeepResearch(data.question, {
        sessionId: data.session_id || state.currentConversation,
        sourceRequestId: data.request_id || null,
        appendUser: false,
      });
      if (!started) deepUpgrade.disabled = false;
    });
  }
  refreshIcons();
  return node;
}

function statusLabel(status) {
  const labels = {
    answered: "已回答",
    out_of_scope: "领域外拒答",
    queued_for_enrichment: "已进入补库队列",
    clarification_required: "等待确认",
    insufficient_evidence: "证据不足",
    completed: "研究完成",
    partial: "研究部分完成",
    failed: "系统错误",
    cancelled: "已取消",
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
  if (state.asking || (state.qaMode === "deep" && state.activeResearchTask)) return;
  const input = $("#questionInput");
  const question = input.value.trim();
  if (!question) return;
  const clarificationSelection = state.pendingClarification;
  state.pendingClarification = null;
  if (state.qaMode === "deep" && !clarificationSelection && !window.confirm("深度模式将消耗 3 次配额，并以异步任务逐份审查候选文件。继续？")) {
    return;
  }
  state.asking = true;
  $("#chatEmptyState").classList.add("hidden");
  appendUserMessage(question);
  const pending = state.qaMode === "deep" ? appendResearchProgress() : appendAssistantLoading();
  input.value = "";
  resizeComposer();
  $("#askButton").disabled = true;
  scrollChatToBottom();
  try {
    if (state.qaMode === "deep") {
      await startDeepResearch(question, {
        sessionId: state.currentConversation,
        existingNode: pending,
        appendUser: false,
        clarificationId: clarificationSelection?.clarificationId || null,
        optionId: clarificationSelection?.optionId || null,
      });
    } else {
      const data = await apiRequest("/api/ask", {
        method: "POST",
        body: JSON.stringify({
          question,
          session_id: state.currentConversation,
          clarification_id: clarificationSelection?.clarificationId || null,
          option_id: clarificationSelection?.optionId || null,
        }),
      });
      data.question = question;
      state.currentConversation = data.session_id;
      appendAssistantMessage(data, pending);
      if (data.quota) updateQuota(data.quota);
      await Promise.allSettled([loadConversations(), loadAccountSummary()]);
    }
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

function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function startDeepResearch(question, options = {}) {
  if (state.activeResearchTask) {
    showToast("当前已有深度研究任务在运行", "error");
    return false;
  }
  const pending = options.existingNode || appendResearchProgress();
  if (options.appendUser !== false) {
    $("#chatEmptyState").classList.add("hidden");
    appendUserMessage(question);
  }
  state.activeResearchTask = "creating";
  scrollChatToBottom();
  try {
    const task = await apiRequest("/api/research/tasks", {
      method: "POST",
      body: JSON.stringify({
        question,
        session_id: options.sessionId || state.currentConversation,
        source_request_id: options.sourceRequestId || null,
        clarification_id: options.clarificationId || null,
        option_id: options.optionId || null,
      }),
    });
    if (task.status === "clarification_required") {
      task.question = question;
      task.mode = "deep";
      state.currentConversation = task.session_id || state.currentConversation;
      appendAssistantMessage(task, pending);
      if (task.quota) updateQuota(task.quota);
      await Promise.allSettled([loadConversations(), loadAccountSummary()]);
      return true;
    }
    state.activeResearchTask = task.task_id;
    state.currentConversation = task.session_id;
    storeActiveResearchTask(task, question);
    updateResearchProgress(pending, task);
    if (task.quota) updateQuota(task.quota);
    await monitorResearchTask(task, question, pending);
    await Promise.allSettled([loadConversations(), loadAccountSummary()]);
    return true;
  } catch (error) {
    appendAssistantMessage({
      answer: `深度研究创建或执行失败：${error.message}`,
      status: "failed",
      mode: "deep",
      session_id: options.sessionId || state.currentConversation,
      question,
      sources: [],
      limitations: { notes: [] },
      quota: error.payload?.detail?.quota || null,
    }, pending);
    showToast(error.message, "error");
    return false;
  } finally {
    state.activeResearchTask = null;
    scrollChatToBottom();
  }
}

function storeActiveResearchTask(task, question) {
  try {
    window.localStorage.setItem("geowiki.activeResearchTask", JSON.stringify({
      taskId: task.task_id,
      question,
      sessionId: task.session_id,
    }));
  } catch {
    // The server task remains persistent even when browser storage is unavailable.
  }
}

function clearStoredResearchTask(taskId) {
  try {
    const raw = window.localStorage.getItem("geowiki.activeResearchTask");
    const stored = raw ? JSON.parse(raw) : null;
    if (!taskId || stored?.taskId === taskId) window.localStorage.removeItem("geowiki.activeResearchTask");
  } catch {
    // Ignore unavailable or malformed browser storage.
  }
}

async function monitorResearchTask(initialTask, question, pending) {
  let current = initialTask;
  const terminal = new Set(["completed", "partial", "insufficient_evidence", "failed", "cancelled"]);
  while (!terminal.has(current.status)) {
    await wait(1200);
    current = await apiRequest(`/api/research/tasks/${encodeURIComponent(initialTask.task_id)}`);
    updateResearchProgress(pending, current);
    if (current.quota) updateQuota(current.quota);
    scrollChatToBottom(false);
  }

  clearStoredResearchTask(initialTask.task_id);
  if (current.result_available) {
    const result = await apiRequest(`/api/research/tasks/${encodeURIComponent(initialTask.task_id)}/result`);
    result.question = question;
    result.mode = "deep";
    appendAssistantMessage(result, pending);
    if (result.quota) updateQuota(result.quota);
    return;
  }
  appendAssistantMessage({
    answer: current.status === "cancelled"
      ? "深度研究任务已在排队阶段取消，本次预留次数已退回。"
      : "深度研究任务执行失败，本次预留次数已退回。",
    status: current.status,
    mode: "deep",
    session_id: current.session_id,
    request_id: current.request_id,
    question,
    sources: [],
    limitations: { notes: [current.progress?.message || "任务未形成可用结果。"] },
    quota: current.quota || null,
  }, pending);
}

async function resumeStoredResearchTask() {
  if (state.activeResearchTask || !state.user) return;
  let stored = null;
  try {
    const raw = window.localStorage.getItem("geowiki.activeResearchTask");
    stored = raw ? JSON.parse(raw) : null;
  } catch {
    return;
  }
  if (!stored?.taskId || !stored?.question) return;
  try {
    const task = await apiRequest(`/api/research/tasks/${encodeURIComponent(stored.taskId)}`);
    state.activeResearchTask = task.task_id;
    state.currentConversation = task.session_id || stored.sessionId || null;
    navigate("chat");
    $("#chatEmptyState").classList.add("hidden");
    if (!$("#messageList").children.length) appendUserMessage(stored.question);
    const pending = appendResearchProgress();
    updateResearchProgress(pending, task);
    await monitorResearchTask(task, stored.question, pending);
    await Promise.allSettled([loadConversations(), loadAccountSummary()]);
  } catch (error) {
    if (error.status === 404 || error.status === 403) clearStoredResearchTask(stored.taskId);
  } finally {
    state.activeResearchTask = null;
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

function splitLexiconList(value) {
  return [...new Set(String(value || "").split(/[\n,，;；]+/).map((item) => item.trim()).filter(Boolean))];
}

function joinLexiconList(value) {
  return Array.isArray(value) ? value.join("\n") : "";
}

function lexiconStatusLabel(status) {
  return {
    active: "已启用",
    disabled: "已停用",
    draft: "草稿",
    pending: "待审核",
    approved: "已批准",
    rejected: "已驳回",
  }[status] || status || "--";
}

function lexiconRiskLabel(risk) {
  return { low: "低", medium: "中", high: "高" }[risk] || risk || "--";
}

function lexiconSourceLabel(source) {
  return {
    manual: "管理员录入",
    user_feedback: "用户反馈",
    query_mining: "问题挖掘",
    kb_schema: "知识库 Schema",
  }[source] || source || "--";
}

function lexiconActionLabel(action) {
  return {
    candidate_created: "创建候选",
    candidate_updated: "修改候选",
    candidate_previewed: "上线前预览",
    candidate_approved: "批准生效",
    candidate_rejected: "驳回候选",
    entry_active: "恢复词条",
    entry_disabled: "停用词条",
  }[action] || action || "--";
}

function lexiconStatusClass(status) {
  if (["active", "approved"].includes(status)) return "answered";
  if (["draft", "pending"].includes(status)) return "queued_for_enrichment";
  return "out_of_scope";
}

async function loadLexiconData() {
  if (state.user?.role !== "admin") return;
  try {
    const data = await apiRequest("/api/admin/lexicon");
    state.lexiconData = data;
    renderLexiconSummary(data.summary || {});
    renderLexiconCandidates(data.candidates || []);
    renderLexiconEntries(data.entries || []);
    renderLexiconAudit(data.audit || []);
  } catch (error) {
    showToast(error.message, "error");
  }
}

function renderLexiconSummary(summary) {
  $("#lexiconActiveCount").textContent = displayCount(summary.active_entries);
  $("#lexiconPendingCount").textContent = displayCount(
    Number(summary.pending_candidates || 0) + Number(summary.draft_candidates || 0),
  );
  $("#lexiconDisabledCount").textContent = displayCount(summary.disabled_entries);
  $("#lexiconHighRiskCount").textContent = displayCount(summary.high_risk_candidates);
}

function renderLexiconCandidates(items) {
  const body = $("#lexiconCandidateTableBody");
  body.innerHTML = "";
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty-row">暂无候选词条。</td></tr>';
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><strong>${escapeHtml(item.user_expression)}</strong>${item.aliases?.length ? `<span class="table-subtext">${escapeHtml(item.aliases.join("、"))}</span>` : ""}</td>
      <td>${escapeHtml(item.canonical_term)}</td>
      <td><code>${escapeHtml(item.intent_label)}</code><span class="table-subtext">${escapeHtml(item.domain)}</span></td>
      <td>${item.domain_gate_enabled ? "领域 + 意图" : item.intent_trigger_enabled ? "仅意图" : "仅扩展"}</td>
      <td>${escapeHtml(lexiconRiskLabel(item.risk_level))}<span class="table-subtext">${escapeHtml(item.preview_ready ? "预览已通过" : lexiconSourceLabel(item.source_type))}</span></td>
      <td><span class="status-chip ${lexiconStatusClass(item.status)}">${escapeHtml(lexiconStatusLabel(item.status))}</span></td>
      <td>${escapeHtml(formatDate(item.updated_at))}</td>
      <td><button class="table-action lexicon-review-action" type="button">${item.status === "approved" ? "查看" : "审核"}</button></td>
    `;
    $(".lexicon-review-action", row).addEventListener("click", () => openLexiconCandidateDialog(item));
    body.appendChild(row);
  });
}

function renderLexiconEntries(items) {
  const body = $("#lexiconEntryTableBody");
  body.innerHTML = "";
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty-row">暂无正式词条。</td></tr>';
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("tr");
    const contexts = item.required_context_terms?.length ? item.required_context_terms.join("、") : "无";
    row.innerHTML = `
      <td><strong>${escapeHtml(item.user_expression)}</strong>${item.aliases?.length ? `<span class="table-subtext">${escapeHtml(item.aliases.join("、"))}</span>` : ""}</td>
      <td>${escapeHtml(item.canonical_term)}</td>
      <td><code>${escapeHtml(item.intent_label)}</code><span class="table-subtext">${escapeHtml(item.domain)}</span></td>
      <td title="${escapeHtml(contexts)}">${escapeHtml(contexts)}</td>
      <td>${item.domain_gate_enabled ? "领域 + 意图" : item.intent_trigger_enabled ? "仅意图" : "仅扩展"}</td>
      <td>v${displayCount(item.version)}</td>
      <td><span class="status-chip ${lexiconStatusClass(item.status)}">${escapeHtml(lexiconStatusLabel(item.status))}</span></td>
      <td><div class="table-actions"><button class="table-action lexicon-edit-action" type="button">提修改</button><button class="table-action ${item.status === "active" ? "danger" : ""} lexicon-status-action" type="button">${item.status === "active" ? "停用" : "恢复"}</button></div></td>
    `;
    $(".lexicon-edit-action", row).addEventListener("click", () => openLexiconCandidateDialog(null, {
      ...item,
      target_lexicon_id: item.lexicon_id,
      positive_examples: [],
      negative_examples: [],
      status: "draft",
      source_type: "manual",
      source_reference: `修改正式词条 ${item.lexicon_id}`,
      review_note: "",
    }));
    $(".lexicon-status-action", row).addEventListener("click", () => openLexiconStatusDialog(item));
    body.appendChild(row);
  });
}

function renderLexiconAudit(items) {
  const body = $("#lexiconAuditTableBody");
  body.innerHTML = "";
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty-row">暂无审核记录。</td></tr>';
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${escapeHtml(formatDate(item.created_at))}</td>
      <td>${escapeHtml(lexiconActionLabel(item.action))}</td>
      <td><code>${escapeHtml(item.lexicon_id || item.candidate_id || "--")}</code></td>
      <td><code>${escapeHtml(item.actor_user_id || "--")}</code></td>
      <td>${escapeHtml(item.note || "--")}</td>
    `;
    body.appendChild(row);
  });
}

function lexiconFormPayload() {
  return {
    target_lexicon_id: state.lexiconCandidate?.target_lexicon_id || null,
    user_expression: $("#lexiconExpressionInput").value.trim(),
    canonical_term: $("#lexiconCanonicalInput").value.trim(),
    intent_label: $("#lexiconIntentInput").value.trim(),
    domain: $("#lexiconDomainInput").value.trim(),
    aliases: splitLexiconList($("#lexiconAliasesInput").value),
    positive_expansions: splitLexiconList($("#lexiconExpansionsInput").value),
    negative_terms: splitLexiconList($("#lexiconNegativeTermsInput").value),
    evidence_required_patterns: splitLexiconList($("#lexiconEvidencePatternsInput").value),
    required_context_terms: splitLexiconList($("#lexiconRequiredContextInput").value),
    forbidden_context_terms: splitLexiconList($("#lexiconForbiddenContextInput").value),
    positive_examples: splitLexiconList($("#lexiconPositiveExamplesInput").value),
    negative_examples: splitLexiconList($("#lexiconNegativeExamplesInput").value),
    match_type: $("#lexiconMatchTypeInput").value,
    domain_gate_enabled: $("#lexiconDomainGateInput").checked,
    intent_trigger_enabled: $("#lexiconIntentTriggerInput").checked,
    priority: Number($("#lexiconPriorityInput").value),
    risk_level: $("#lexiconRiskInput").value,
    status: $("#lexiconCandidateStatusInput").value,
    source_type: $("#lexiconSourceTypeInput").value,
    source_reference: $("#lexiconSourceReferenceInput").value.trim() || null,
    review_note: $("#lexiconReviewNoteInput").value.trim() || null,
  };
}

function fillLexiconForm(item) {
  $("#lexiconExpressionInput").value = item.user_expression || "";
  $("#lexiconCanonicalInput").value = item.canonical_term || "";
  $("#lexiconIntentInput").value = item.intent_label || "";
  $("#lexiconDomainInput").value = item.domain || "";
  $("#lexiconAliasesInput").value = joinLexiconList(item.aliases);
  $("#lexiconMatchTypeInput").value = item.match_type || "phrase";
  $("#lexiconCandidateStatusInput").value = ["draft", "pending"].includes(item.status) ? item.status : "draft";
  $("#lexiconPriorityInput").value = String(item.priority ?? 50);
  $("#lexiconRiskInput").value = item.risk_level || "medium";
  $("#lexiconDomainGateInput").checked = Boolean(item.domain_gate_enabled);
  $("#lexiconIntentTriggerInput").checked = item.intent_trigger_enabled !== false;
  $("#lexiconRequiredContextInput").value = joinLexiconList(item.required_context_terms);
  $("#lexiconForbiddenContextInput").value = joinLexiconList(item.forbidden_context_terms);
  $("#lexiconExpansionsInput").value = joinLexiconList(item.positive_expansions);
  $("#lexiconNegativeTermsInput").value = joinLexiconList(item.negative_terms);
  $("#lexiconEvidencePatternsInput").value = joinLexiconList(item.evidence_required_patterns);
  $("#lexiconPositiveExamplesInput").value = joinLexiconList(item.positive_examples);
  $("#lexiconNegativeExamplesInput").value = joinLexiconList(item.negative_examples);
  $("#lexiconSourceTypeInput").value = item.source_type || "manual";
  $("#lexiconSourceReferenceInput").value = item.source_reference || "";
  $("#lexiconReviewNoteInput").value = item.review_note || "";
  $("#lexiconPreviewQueryInput").value = item.positive_examples?.[0] || "";
  $("#lexiconPreviewResult").textContent = "尚未运行预览。";
}

function updateLexiconApprovalState() {
  const candidate = state.lexiconCandidate;
  const button = $("#approveLexiconButton");
  const reviewable = Boolean(candidate?.candidate_id) && candidate?.status !== "approved";
  const pending = $("#lexiconCandidateStatusInput").value === "pending";
  const previewReady = Boolean(candidate?.preview_ready);
  button.classList.toggle("hidden", !reviewable);
  button.disabled = !pending || !previewReady;
  if (!pending) {
    button.title = "先将候选状态设为待审核并保存预览";
  } else if (!previewReady) {
    button.title = "必须先通过正向示例和反例的上线前预览";
  } else {
    button.title = "批准后立即发布到运行时词典";
  }
}

function openLexiconCandidateDialog(candidate = null, initial = {}) {
  const item = candidate || {
    target_lexicon_id: null,
    user_expression: "",
    canonical_term: "",
    intent_label: "",
    domain: "",
    aliases: [],
    positive_expansions: [],
    negative_terms: [],
    evidence_required_patterns: [],
    required_context_terms: [],
    forbidden_context_terms: [],
    positive_examples: [],
    negative_examples: [],
    match_type: "phrase",
    domain_gate_enabled: false,
    intent_trigger_enabled: true,
    priority: 50,
    risk_level: "medium",
    status: "draft",
    source_type: "manual",
    source_reference: null,
    review_note: null,
    ...initial,
  };
  state.lexiconCandidate = item;
  $("#lexiconDialogTitle").textContent = candidate?.candidate_id
    ? "审核词典候选"
    : item.target_lexicon_id ? "提出词条修改" : "新增词典候选";
  fillLexiconForm(item);
  const approved = candidate?.status === "approved";
  $$("#lexiconDialog .lexicon-dialog-body input, #lexiconDialog .lexicon-dialog-body select, #lexiconDialog .lexicon-dialog-body textarea").forEach((control) => {
    control.disabled = approved;
  });
  $("#previewLexiconButton").classList.toggle("hidden", approved);
  $("#rejectLexiconButton").classList.toggle("hidden", !candidate?.candidate_id || approved);
  $("#saveLexiconCandidateButton").classList.toggle("hidden", approved);
  updateLexiconApprovalState();
  $("#lexiconDialog").showModal();
  $("#lexiconExpressionInput").focus();
  refreshIcons();
}

function validateLexiconForm(payload) {
  if (!payload.user_expression || !payload.canonical_term || !payload.intent_label || !payload.domain) {
    showToast("请填写用户表达、规范术语、意图标签和适用领域", "error");
    return false;
  }
  if (!Number.isInteger(payload.priority) || payload.priority < 0 || payload.priority > 100) {
    showToast("优先级应为 0 至 100 的整数", "error");
    return false;
  }
  return true;
}

async function saveLexiconCandidate() {
  const payload = lexiconFormPayload();
  if (!validateLexiconForm(payload)) return;
  const button = $("#saveLexiconCandidateButton");
  setBusy(button, true, "保存中");
  try {
    const candidateId = state.lexiconCandidate?.candidate_id;
    await apiRequest(candidateId
      ? `/api/admin/lexicon/candidates/${encodeURIComponent(candidateId)}`
      : "/api/admin/lexicon/candidates", {
      method: candidateId ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    $("#lexiconDialog").close();
    showToast(payload.status === "pending" ? "候选已提交审核" : "候选草稿已保存");
    await loadLexiconData();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

function formatLexiconPreview(label, result) {
  const matches = (result.intent_matches || []).map((item) => `${item.user_expression} → ${item.intent_label}`).join("；") || "无";
  const expansions = (result.expansions || []).join("、") || "无";
  const evidencePatterns = (result.evidence_patterns || []).join("、") || "无";
  return `${label}\n领域门控：${result.domain_gate_passed ? "通过" : "不通过"}\n意图命中：${matches}\n检索扩展：${expansions}\n证据约束：${evidencePatterns}`;
}

async function previewLexiconCandidate() {
  const payload = lexiconFormPayload();
  const query = $("#lexiconPreviewQueryInput").value.trim();
  if (!validateLexiconForm(payload) || !query) {
    if (!query) showToast("请输入上线前测试问题", "error");
    return;
  }
  const button = $("#previewLexiconButton");
  setBusy(button, true, "测试中");
  try {
    const candidateId = state.lexiconCandidate?.candidate_id;
    const saved = await apiRequest(candidateId
      ? `/api/admin/lexicon/candidates/${encodeURIComponent(candidateId)}`
      : "/api/admin/lexicon/candidates", {
      method: candidateId ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    state.lexiconCandidate = saved.item;
    const data = await apiRequest("/api/admin/lexicon/preview", {
      method: "POST",
      body: JSON.stringify({ candidate_id: saved.item.candidate_id, query, candidate: payload }),
    });
    state.lexiconCandidate = { ...saved.item, preview_ready: data.verification_passed };
    $("#lexiconDialogTitle").textContent = "审核词典候选";
    $("#rejectLexiconButton").classList.remove("hidden");
    updateLexiconApprovalState();
    const checks = (data.example_checks || []).map((item) => (
      `${item.passed ? "通过" : "失败"} · ${item.kind === "positive" ? "正例" : "反例"} · ${item.query}`
    ));
    const warnings = data.warnings?.length ? `\n\n风险提示\n- ${data.warnings.join("\n- ")}` : "";
    const verification = `\n\n正反例校验：${data.verification_passed ? "全部通过" : "未通过"}${checks.length ? `\n${checks.join("\n")}` : ""}`;
    $("#lexiconPreviewResult").textContent = `${formatLexiconPreview("当前规则", data.current)}\n\n${formatLexiconPreview("候选生效后", data.proposed)}${verification}${warnings}`;
    showToast(data.verification_passed ? "候选已保存，正反例预览通过" : "候选已保存，但预览仍有未通过项", data.verification_passed ? "success" : "error");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function reviewLexiconCandidate(action) {
  const candidateId = state.lexiconCandidate?.candidate_id;
  const note = $("#lexiconReviewNoteInput").value.trim();
  if (!candidateId || !note) {
    showToast("批准或驳回前必须填写审核记录", "error");
    return;
  }
  const button = action === "approve" ? $("#approveLexiconButton") : $("#rejectLexiconButton");
  setBusy(button, true, action === "approve" ? "发布中" : "驳回中");
  try {
    if (action === "approve") {
      const payload = lexiconFormPayload();
      if (!validateLexiconForm(payload)) return;
      await apiRequest(`/api/admin/lexicon/candidates/${encodeURIComponent(candidateId)}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
    }
    await apiRequest(`/api/admin/lexicon/candidates/${encodeURIComponent(candidateId)}/review`, {
      method: "POST",
      body: JSON.stringify({ action, note }),
    });
    $("#lexiconDialog").close();
    showToast(action === "approve" ? "候选已批准并发布" : "候选已驳回");
    await loadLexiconData();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

function openLexiconStatusDialog(entry) {
  state.lexiconStatusEntry = entry;
  const activating = entry.status !== "active";
  $("#lexiconStatusDialogTitle").textContent = activating ? "恢复词条" : "停用词条";
  $("#lexiconStatusTarget").textContent = `${entry.user_expression} → ${entry.canonical_term}`;
  $("#lexiconStatusNoteInput").value = "";
  $("#lexiconStatusDialog").showModal();
  $("#lexiconStatusNoteInput").focus();
}

async function updateLexiconEntryStatus() {
  const entry = state.lexiconStatusEntry;
  const note = $("#lexiconStatusNoteInput").value.trim();
  if (!entry || !note) {
    showToast("请输入操作原因", "error");
    return;
  }
  const status = entry.status === "active" ? "disabled" : "active";
  const button = $("#confirmLexiconStatusButton");
  setBusy(button, true, "保存中");
  try {
    await apiRequest(`/api/admin/lexicon/entries/${encodeURIComponent(entry.lexicon_id)}/status`, {
      method: "POST",
      body: JSON.stringify({ status, note }),
    });
    $("#lexiconStatusDialog").close();
    state.lexiconStatusEntry = null;
    showToast(status === "active" ? "词条已恢复" : "词条已停用");
    await loadLexiconData();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

function createLexiconCandidateFromFeedback() {
  const item = state.feedbackItem;
  if (!item) return;
  $("#feedbackDialog").close();
  navigate("lexicon");
  openLexiconCandidateDialog(null, {
    source_type: "user_feedback",
    source_reference: item.feedback_id,
    positive_examples: item.question ? [item.question] : [],
    review_note: item.comment || "",
  });
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
  $$("#qaModeControl .segment").forEach((button) => button.addEventListener("click", () => setQAMode(button.dataset.mode)));
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
    if (button.dataset.mode) setQAMode(button.dataset.mode);
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
  $("#createLexiconFromFeedbackButton").addEventListener("click", createLexiconCandidateFromFeedback);

  $("#createLexiconCandidateButton").addEventListener("click", () => openLexiconCandidateDialog());
  $("#refreshLexiconButton").addEventListener("click", loadLexiconData);
  $("#saveLexiconCandidateButton").addEventListener("click", saveLexiconCandidate);
  $("#previewLexiconButton").addEventListener("click", previewLexiconCandidate);
  $("#approveLexiconButton").addEventListener("click", () => reviewLexiconCandidate("approve"));
  $("#rejectLexiconButton").addEventListener("click", () => reviewLexiconCandidate("reject"));
  $("#confirmLexiconStatusButton").addEventListener("click", updateLexiconEntryStatus);
  $("#lexiconDialog").addEventListener("input", (event) => {
    if (!state.lexiconCandidate?.candidate_id) return;
    if (["lexiconPreviewQueryInput", "lexiconReviewNoteInput"].includes(event.target.id)) return;
    state.lexiconCandidate.preview_ready = false;
    updateLexiconApprovalState();
  });
  $("#lexiconCandidateStatusInput").addEventListener("change", updateLexiconApprovalState);

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
  try {
    setQAMode(window.localStorage.getItem("geowiki.qaMode") || "basic", false);
  } catch {
    setQAMode("basic", false);
  }
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
