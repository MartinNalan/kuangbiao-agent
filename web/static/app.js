const askView = document.querySelector("#askView");
const standardsView = document.querySelector("#standardsView");
const viewTitle = document.querySelector("#viewTitle");
const statusLine = document.querySelector("#statusLine");
const historyList = document.querySelector("#historyList");
const questionInput = document.querySelector("#questionInput");
const answerText = document.querySelector("#answerText");
const confidenceBadge = document.querySelector("#confidenceBadge");
const retrievalStats = document.querySelector("#retrievalStats");
const limitations = document.querySelector("#limitations");
const sourcesList = document.querySelector("#sourcesList");
const askButton = document.querySelector("#askButton");
const standardsList = document.querySelector("#standardsList");
const feedbackPanel = document.querySelector("#feedbackPanel");
const feedbackDetail = document.querySelector("#feedbackDetail");
const feedbackReason = document.querySelector("#feedbackReason");
const feedbackComment = document.querySelector("#feedbackComment");
const feedbackStatus = document.querySelector("#feedbackStatus");
const satisfiedButton = document.querySelector("#satisfiedButton");
const unsatisfiedButton = document.querySelector("#unsatisfiedButton");
const sendFeedbackButton = document.querySelector("#sendFeedbackButton");

const historyKey = "mining_qa_recent_questions";
const devApiKey = "dev-local-key";
let currentAnswer = null;

function switchView(view) {
  const isAsk = view === "ask";
  askView.classList.toggle("active", isAsk);
  standardsView.classList.toggle("active", !isAsk);
  document.querySelectorAll(".nav-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  viewTitle.textContent = isAsk ? "专业问答" : "标准目录";
  statusLine.textContent = isAsk
    ? "本地知识库优先，证据不足时降级回答。"
    : "查询本地知识库中已入库或可用的标准。";
}

function loadHistory() {
  try {
    return JSON.parse(localStorage.getItem(historyKey) || "[]");
  } catch {
    return [];
  }
}

function saveHistory(question) {
  const items = loadHistory().filter((item) => item !== question);
  items.unshift(question);
  localStorage.setItem(historyKey, JSON.stringify(items.slice(0, 8)));
  renderHistory();
}

function renderHistory() {
  const items = loadHistory();
  historyList.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "暂无记录。";
    historyList.appendChild(empty);
    return;
  }
  items.forEach((item) => {
    const button = document.createElement("button");
    button.className = "history-item";
    button.type = "button";
    button.textContent = item;
    button.addEventListener("click", () => {
      questionInput.value = item;
      switchView("ask");
      questionInput.focus();
    });
    historyList.appendChild(button);
  });
}

function setStats(stats) {
  const values = [
    stats.full_text_hits || 0,
    stats.vector_hits || 0,
    stats.graph_hits || 0,
    stats.web_hits || 0,
  ];
  retrievalStats.querySelectorAll("dd").forEach((node, index) => {
    node.textContent = values[index];
  });
}

function renderLimitations(data) {
  const notes = data?.notes || [];
  if (!notes.length) {
    limitations.textContent = data?.has_clause_level_evidence ? "已找到条款级证据。" : "";
    return;
  }
  limitations.innerHTML = notes.map((note) => `<div>${escapeHtml(note)}</div>`).join("");
}

function renderSources(sources) {
  sourcesList.innerHTML = "";
  if (!sources.length) {
    sourcesList.classList.add("empty");
    sourcesList.textContent = "暂无引用来源。";
    return;
  }
  sourcesList.classList.remove("empty");
  sources.forEach((source) => {
    const item = document.createElement("article");
    item.className = "source-item";
    const officialLink = source.url
      ? `<a class="source-link" href="${escapeAttr(source.url)}" target="_blank" rel="noreferrer">${escapeHtml(source.source_platform || "官方来源")}</a>`
      : "";
    item.innerHTML = `
      <div class="source-title">${escapeHtml(source.title || "未知文件")}</div>
      <div class="source-meta">
        <span>${escapeHtml(source.standard_no || "无标准号")}</span>
        <span>${escapeHtml(source.chapter || "无条款")}</span>
        <span>${source.page == null ? "无页码" : `第 ${source.page} 页`}</span>
        <span>${escapeHtml(source.source_type || "unknown")}</span>
        <span>${escapeHtml(source.text_access || "unknown")}</span>
        ${officialLink}
      </div>
      ${source.quote ? `<div class="quote">${escapeHtml(source.quote)}</div>` : ""}
    `;
    sourcesList.appendChild(item);
  });
}

async function submitQuestion(event) {
  event.preventDefault();
  const question = questionInput.value.trim();
  if (!question) return;

  askButton.disabled = true;
  currentAnswer = null;
  resetFeedback();
  askButton.textContent = "查询中";
  answerText.textContent = "正在检索知识库...";
  confidenceBadge.textContent = "查询中";
  confidenceBadge.classList.add("muted");

  try {
    const response = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": devApiKey },
      body: JSON.stringify({ question }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    currentAnswer = { sessionId: data.session_id, question };

    renderAnswer(data.answer || "无回答。");
    confidenceBadge.textContent = data.status || data.confidence || "unknown";
    confidenceBadge.classList.toggle("muted", data.confidence === "low");
    setStats(data.retrieval || {});
    renderLimitations(data.limitations || {});
    if (data.knowledge_gap_task) {
      const taskNote = document.createElement("div");
      taskNote.textContent = `补库任务：${data.knowledge_gap_task.task_id}（${data.knowledge_gap_task.status}）`;
      limitations.appendChild(taskNote);
    }
    renderSources(data.sources || []);
    feedbackPanel.classList.remove("hidden");
    saveHistory(question);
  } catch (error) {
    renderAnswer(`请求失败：${error.message}`);
    confidenceBadge.textContent = "失败";
    confidenceBadge.classList.add("muted");
  } finally {
    askButton.disabled = false;
    askButton.textContent = "提交问题";
  }
}

function resetFeedback() {
  feedbackPanel.classList.add("hidden");
  feedbackDetail.classList.add("hidden");
  feedbackStatus.textContent = "";
  feedbackComment.value = "";
  satisfiedButton.disabled = false;
  unsatisfiedButton.disabled = false;
  sendFeedbackButton.disabled = false;
}

async function submitFeedback(rating, reason = null, comment = "") {
  if (!currentAnswer?.sessionId) return;
  satisfiedButton.disabled = true;
  unsatisfiedButton.disabled = true;
  sendFeedbackButton.disabled = true;
  feedbackStatus.textContent = "正在提交反馈...";
  try {
    const response = await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": devApiKey },
      body: JSON.stringify({
        session_id: currentAnswer.sessionId,
        question: currentAnswer.question,
        rating,
        reason,
        comment: comment.trim() || null,
      }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    feedbackStatus.textContent = rating === "satisfied" ? "已记录：满意。" : "已记录：不满意。";
    feedbackDetail.classList.add("hidden");
  } catch (error) {
    feedbackStatus.textContent = `反馈提交失败：${error.message}`;
    satisfiedButton.disabled = false;
    unsatisfiedButton.disabled = false;
    sendFeedbackButton.disabled = false;
  }
}

async function submitStandards(event) {
  event.preventDefault();
  const params = new URLSearchParams();
  const q = document.querySelector("#standardQuery").value.trim();
  const standardNo = document.querySelector("#standardNo").value.trim();
  const textAccess = document.querySelector("#textAccess").value;
  if (q) params.set("q", q);
  if (standardNo) params.set("standard_no", standardNo);
  if (textAccess) params.set("text_access", textAccess);
  params.set("page", "1");
  params.set("page_size", "20");

  standardsList.classList.add("empty");
  standardsList.textContent = "查询中...";

  try {
    const response = await fetch(`/api/standards?${params.toString()}`, {
      headers: { "X-API-Key": devApiKey },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderStandards(data.items || []);
  } catch (error) {
    standardsList.textContent = `请求失败：${error.message}`;
  }
}

function renderStandards(items) {
  standardsList.innerHTML = "";
  if (!items.length) {
    standardsList.classList.add("empty");
    standardsList.textContent = "未查询到标准。";
    return;
  }
  standardsList.classList.remove("empty");
  items.forEach((item) => {
    const node = document.createElement("article");
    node.className = "standard-item";
    const officialLink = item.url
      ? `<a class="source-link" href="${escapeAttr(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source_platform || "官方来源")}</a>`
      : "";
    node.innerHTML = `
      <div class="standard-title">${escapeHtml(item.title || "未知标准")}</div>
      <div class="standard-meta">
        <span>${escapeHtml(item.standard_no || "无标准号")}</span>
        <span>${escapeHtml(item.status || "状态未知")}</span>
        <span>${escapeHtml(item.text_access || "unknown")}</span>
        <span>${item.can_answer ? "可问答" : "不可问答"}</span>
        ${officialLink}
      </div>
    `;
    standardsList.appendChild(node);
  });
}

function renderAnswer(value) {
  answerText.innerHTML = renderInlineMarkdown(value);
}

function renderInlineMarkdown(value) {
  let html = escapeHtml(value);
  const links = [];
  html = html.replace(
    /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    (_, label, url) => {
      const token = `@@LINK_${links.length}@@`;
      links.push(`<a href="${escapeAttr(url)}" target="_blank" rel="noreferrer">${label}</a>`);
      return token;
    },
  );
  html = html.replace(
    /(https?:\/\/[^\s<]+)/g,
    (url) => `<a href="${escapeAttr(url)}" target="_blank" rel="noreferrer">${url}</a>`,
  );
  html = html.replace(/^###\s+(.+)$/gm, '<span class="answer-heading">$1</span>');
  html = html.replace(/^##\s+(.+)$/gm, '<span class="answer-heading">$1</span>');
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  links.forEach((link, index) => {
    html = html.replace(`@@LINK_${index}@@`, link);
  });
  return html;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

document.querySelectorAll(".nav-tab").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});

document.querySelector("#askForm").addEventListener("submit", submitQuestion);
document.querySelector("#standardsForm").addEventListener("submit", submitStandards);
satisfiedButton.addEventListener("click", () => submitFeedback("satisfied"));
unsatisfiedButton.addEventListener("click", () => {
  feedbackStatus.textContent = "";
  feedbackDetail.classList.remove("hidden");
  feedbackReason.focus();
});
sendFeedbackButton.addEventListener("click", () => {
  submitFeedback("unsatisfied", feedbackReason.value, feedbackComment.value);
});
renderHistory();
