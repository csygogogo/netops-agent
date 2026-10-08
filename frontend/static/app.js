const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const SVG_NS = "http://www.w3.org/2000/svg";
const agentRunStates = new WeakMap();

const state = {
  diagnosing: false,
  features: {
    perception: true,
    deepDiagnosis: true,
  },
  risks: [],
  executions: [],
  selectedExecutionId: null,
  executionFilter: "all",
  perceptionMode: "tasks",
  diagnosisTopologyScale: 1,
  diagnosisTopologyPanX: 0,
  diagnosisTopologyPanY: 0,
  perceptionTopologyScale: 1,
  perceptionTopologyPanX: 0,
  perceptionTopologyPanY: 0,
  deepDiagnosing: false,
  deepExecutionId: null,
  deepTopologyScale: 1,
  deepTopologyPanX: 0,
  deepTopologyPanY: 0,
  deepDiagnosisTopology: null,
  diagnosisRailWidth: 330,
  currentConversationId: "initial-conversation",
  currentConversationTitled: true,
  currentDiagnosisTopology: null,
  currentDiagnosisTopologyPayload: null,
  conversations: new Map(),
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function markdownToHtml(markdown) {
  const lines = String(markdown ?? "").replace(/\r/g, "").split("\n");
  const html = [];
  let list = null;
  let fence = "", code = [], table = false;

  const closeList = () => {
    if (list) html.push(`</${list}>`);
    list = null;
  };

  for (let index = 0; index < lines.length; index++) {
    const rawLine = lines[index];
    const line = rawLine.trim();
    if (fence) {
      if (line.startsWith(fence)) { html.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`); fence = ""; code = []; }
      else code.push(rawLine);
      continue;
    }
    if (table && (!line.includes("|") || !line)) { html.push("</tbody></table></div>"); table = false; }
    if (/^(```|~~~)/.test(line)) {
      closeList(); fence = line.slice(0, 3); continue;
    }
    const cells = value => value.trim().replace(/^\||\|$/g, "").split("|").map(cell => cell.trim());
    const divider = lines[index + 1]?.trim();
    if (!table && line.includes("|") && divider?.includes("|") && cells(divider).every(cell => /^:?-{3,}:?$/.test(cell))) {
      closeList(); table = true;
      html.push(`<div class="markdown-table"><table><thead><tr>${cells(line).map(cell => `<th>${inlineMarkdown(cell)}</th>`).join("")}</tr></thead><tbody>`);
      index++; continue;
    }
    if (table) { html.push(`<tr>${cells(line).map(cell => `<td>${inlineMarkdown(cell)}</td>`).join("")}</tr>`); continue; }
    if (!line) {
      closeList();
      continue;
    }
    if (line.startsWith("# ")) {
      closeList();
      html.push(`<h2>${inlineMarkdown(line.slice(2))}</h2>`);
    } else if (line.startsWith("### ")) {
      closeList();
      html.push(`<h3>${inlineMarkdown(line.slice(4))}</h3>`);
    } else if (line.startsWith("## ")) {
      closeList();
      html.push(`<h2>${inlineMarkdown(line.slice(3))}</h2>`);
    } else if (/^[-*]\s+/.test(line)) {
      if (list !== "ul") {
        closeList();
        list = "ul";
        html.push("<ul>");
      }
      html.push(`<li>${inlineMarkdown(line.replace(/^[-*]\s+/, ""))}</li>`);
    } else if (/^\d+\.\s+/.test(line)) {
      if (list !== "ol") {
        closeList();
        list = "ol";
        html.push("<ol>");
      }
      html.push(`<li>${inlineMarkdown(line.replace(/^\d+\.\s+/, ""))}</li>`);
    } else {
      closeList();
      html.push(`<p>${inlineMarkdown(line)}</p>`);
    }
  }
  closeList();
  // 流式输出尚未闭合的代码块也保持原样排版。
  if (fence) html.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
  if (table) html.push("</tbody></table></div>");
  return html.join("");
}

function svgElement(tag, attrs = {}) {
  const element = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([name, value]) => element.setAttribute(name, value));
  return element;
}

function serializeRawData(data) {
  if (typeof data === "string") return data;
  if (data === undefined) return "undefined";
  try {
    return JSON.stringify(data, null, 2);
  } catch {
    return String(data);
  }
}

function renderRawContent(data) {
  const output = document.createElement("pre");
  output.className = `raw-output ${typeof data === "string" ? "raw-text" : "raw-structured"}`;
  output.textContent = serializeRawData(data);
  return output;
}

function agentEventTag(payload) {
  const tag = String(payload?.tag || "").trim();
  if (tag) return tag;
  return payload?.final ? "最终结果" : "";
}

function agentEventContent(payload) {
  if (payload && Object.prototype.hasOwnProperty.call(payload, "content")) return payload.content;
  return payload?.raw;
}

function parsePossibleJson(value) {
  if (typeof value !== "string") return value;
  const text = value.trim();
  if (!text.startsWith("{") && !text.startsWith("[")) return value;
  try { return JSON.parse(text); }
  catch { return value; }
}

function extractTopologyValue(value, depth = 0) {
  if (depth > 3) return null;
  value = parsePossibleJson(value);
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;

  const tag = String(value.tag || value.type || value.kind || "").toLowerCase();
  if (tag === "topology") {
    const topology = parsePossibleJson(value.data ?? value.topology ?? value.raw);
    if (topology && Array.isArray(topology.devices) && Array.isArray(topology.links)) return topology;
  }

  for (const key of ["content", "result", "data", "raw", "topology"]) {
    const topology = extractTopologyValue(value[key], depth + 1);
    if (topology) return topology;
  }
  return null;
}

function extractTopologyEvent(payload) {
  return extractTopologyValue(payload);
}

function applyFeatureConfig(config) {
  const features = config?.features || {};
  state.features.perception = features.perception !== false;
  state.features.deepDiagnosis = features.deep_diagnosis !== false;

  const perceptionNav = $('.nav-item[data-view="perception"]');
  const deepDiagnosisNav = $('.nav-item[data-view="deepDiagnosis"]');
  perceptionNav.classList.toggle("hidden", !state.features.perception);
  deepDiagnosisNav.classList.toggle("hidden", !state.features.deepDiagnosis);
  $("#perceptionView").classList.toggle("hidden", !state.features.perception);
  $("#deepDiagnosisView").classList.toggle("hidden", !state.features.deepDiagnosis);
  $("#deepBackButton").classList.toggle("hidden", !state.features.perception);
  document.documentElement.classList.remove("feature-config-loading");
}

async function loadFeatureConfig() {
  try {
    const response = await fetch("/api/config");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    applyFeatureConfig(await response.json());
  } catch {
    applyFeatureConfig({ features: { perception: true, deep_diagnosis: true } });
  }
}

function setView(viewName) {
  if (viewName === "perception" && !state.features.perception) viewName = "diagnosis";
  if (viewName === "deepDiagnosis" && !state.features.deepDiagnosis) viewName = "diagnosis";
  $$(".nav-item[data-view]").forEach(button => button.classList.toggle("active", button.dataset.view === viewName));
  $$(".view").forEach(view => view.classList.remove("active"));
  $(`#${viewName}View`).classList.add("active");
  $("#recentSection").classList.toggle("hidden", viewName !== "diagnosis");
  if (viewName === "perception") $("#navAlertDot").classList.add("hidden");
}

function appendUserMessage(question) {
  const message = document.createElement("div");
  message.className = "message user-message";
  const time = new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  message.innerHTML = `<div class="user-bubble">${escapeHtml(question)}</div><span class="user-meta">${time}</span>`;
  $("#messages").append(message);
}

function activateRecentConversation(button) {
  const section = $("#recentSection");
  $$(".recent-item", section).forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  state.currentConversationId = button.dataset.conversationId;
  state.currentConversationTitled = button.dataset.titled !== "false";
}

function ensureConversationRecord(conversationId) {
  if (!state.conversations.has(conversationId)) {
    state.conversations.set(conversationId, {
      messagesHtml: "",
      hasMessages: false,
      scrollTop: 0,
      topology: null,
      topologyPayload: null,
      railExpanded: false,
      topologyScale: 1,
      topologyPanX: 0,
      topologyPanY: 0,
    });
  }
  return state.conversations.get(conversationId);
}

function saveCurrentConversationState() {
  if (!state.currentConversationId) return;
  const record = ensureConversationRecord(state.currentConversationId);
  record.messagesHtml = $("#messages").innerHTML;
  record.hasMessages = $("#messages").children.length > 0;
  record.scrollTop = $("#chatScroll").scrollTop;
  record.topology = state.currentDiagnosisTopology;
  record.topologyPayload = state.currentDiagnosisTopologyPayload;
  record.railExpanded = !$("#diagnosisLayout").classList.contains("context-collapsed");
  record.topologyScale = state.diagnosisTopologyScale;
  record.topologyPanX = state.diagnosisTopologyPanX;
  record.topologyPanY = state.diagnosisTopologyPanY;
  persistDiagnosis();
}

function restoreConversation(conversationId) {
  const record = ensureConversationRecord(conversationId);
  const shouldExpandRail = record.railExpanded;
  const topologyScale = record.topologyScale || 1;
  $("#messages").innerHTML = record.messagesHtml || "";
  $("#introCard").classList.toggle("hidden", Boolean(record.hasMessages));
  state.currentDiagnosisTopology = record.topology;
  state.currentDiagnosisTopologyPayload = record.topologyPayload;

  if (record.topology) {
    showDiagnosisTopology(record.topology, record.topologyPayload || { phase: "执行拓扑" });
    state.diagnosisTopologyPanX = record.topologyPanX || 0;
    state.diagnosisTopologyPanY = record.topologyPanY || 0;
    setDiagnosisRailExpanded(shouldExpandRail);
    setDiagnosisTopologyScale(topologyScale);
  } else {
    resetDiagnosisTopology();
    setDiagnosisRailExpanded(shouldExpandRail);
  }

  requestAnimationFrame(() => {
    $("#chatScroll").scrollTop = record.scrollTop || 0;
  });
}

function switchConversation(button) {
  const conversationId = button.dataset.conversationId;
  if (!conversationId || conversationId === state.currentConversationId) {
    activateRecentConversation(button);
    return;
  }
  if (state.diagnosing) {
    showToast("诊断仍在进行", "当前流式诊断结束后再切换历史对话。 ");
    return;
  }
  saveCurrentConversationState();
  activateRecentConversation(button);
  restoreConversation(conversationId);
  updateSessionLabel();
  loadNetwork();
  setView("diagnosis");
}

function bindRecentConversation(button) {
  if (button.dataset.bound === "true") return;
  button.dataset.bound = "true";
  button.addEventListener("click", () => {
    switchConversation(button);
    $("#questionInput").focus();
  });
}

function createRecentConversation() {
  const section = $("#recentSection");
  const conversationId = `dcn-${crypto.randomUUID()}`;

  const button = document.createElement("button");
  button.className = "recent-item active";
  button.dataset.conversationId = conversationId;
  button.dataset.titled = "false";

  const icon = document.createElement("span");
  icon.className = "recent-icon";
  icon.textContent = "✦";
  const label = document.createElement("span");
  label.textContent = "新对话";
  const time = document.createElement("time");
  time.textContent = "刚刚";
  button.append(icon, label, time);
  bindRecentConversation(button);

  const firstItem = $(".recent-item", section);
  section.insertBefore(button, firstItem);
  activateRecentConversation(button);
  ensureConversationRecord(conversationId);
  const items = $$(".recent-item", section);
  items.slice(7).forEach(item => {
    state.conversations.delete(item.dataset.conversationId);
    item.remove();
  });
}

function nameCurrentConversation(question) {
  if (state.currentConversationTitled) return;
  const button = $(`.recent-item[data-conversation-id="${state.currentConversationId}"]`, $("#recentSection"));
  if (!button) return;
  const label = button.querySelector("span:nth-child(2)");
  if (label) label.textContent = question;
  button.dataset.question = question;
  button.dataset.titled = "true";
  state.currentConversationTitled = true;
  ensureConversationRecord(state.currentConversationId).title = question;
}

function isNearChatBottom(threshold = 140) {
  const scroller = $("#chatScroll");
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= threshold;
}

function scrollChatToBottom(force = false) {
  const scroller = $("#chatScroll");
  if (force || isNearChatBottom()) {
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
  }
}

function createAgentRun() {
  const run = document.createElement("div");
  run.className = "message agent-run";
  run.innerHTML = `<div class="run-label"><i></i><span>DCN Copilot 正在诊断</span></div><div class="steps"></div><div class="typing-card"><span class="typing-dots"><i></i><i></i><i></i></span><span>正在等待智能体事件…</span></div><time class="run-duration"></time>`;
  getAgentRunState(run).startedAt = performance.now();
  $("#messages").append(run);
  return run;
}

function setDiagnosisRailExpanded(expanded) {
  $("#diagnosisLayout").classList.toggle("context-collapsed", !expanded);
  $("#diagnosisRailToggle").setAttribute("aria-expanded", String(expanded));
  $(".rail-toggle-icon", $("#diagnosisRailToggle")).textContent = expanded ? "›" : "‹";
  if (state.currentConversationId) ensureConversationRecord(state.currentConversationId).railExpanded = expanded;
}

function setDiagnosisRailWidth(nextWidth) {
  const layout = $("#diagnosisLayout");
  const maxWidth = Math.max(260, Math.min(620, layout.clientWidth - 420));
  const width = Math.round(Math.min(maxWidth, Math.max(260, nextWidth)));
  state.diagnosisRailWidth = width;
  layout.style.setProperty("--diagnosis-rail-width", `${width}px`);
  $("#diagnosisRailResizer").setAttribute("aria-valuenow", String(width));
}

function startDiagnosisRailResize(event) {
  if ($("#diagnosisLayout").classList.contains("context-collapsed")) return;
  event.preventDefault();
  const startX = event.clientX;
  const startWidth = $("#diagnosisRail").getBoundingClientRect().width;
  document.body.classList.add("resizing-diagnosis-rail");

  const onMove = moveEvent => setDiagnosisRailWidth(startWidth + startX - moveEvent.clientX);
  const onUp = () => {
    document.body.classList.remove("resizing-diagnosis-rail");
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp, { once: true });
}

function resetDiagnosisTopology() {
  state.currentDiagnosisTopology = null;
  state.currentDiagnosisTopologyPayload = null;
  state.diagnosisTopologyPanX = 0;
  state.diagnosisTopologyPanY = 0;
  $("#diagnosisTopologySvg").innerHTML = "";
  $("#diagnosisTopologyContent").classList.add("hidden");
  $("#diagnosisTopologyEmpty").classList.remove("hidden");
  setDiagnosisTopologyScale(1);
  setDiagnosisRailExpanded(false);
}

function applyTopologyView(svg, scale, panX, panY) {
  const width = Number(svg.dataset.width || 760);
  const height = Number(svg.dataset.height || 470);
  const visibleWidth = width / scale;
  const visibleHeight = height / scale;
  const x = (width - visibleWidth) / 2 + panX;
  const y = (height - visibleHeight) / 2 + panY;
  svg.setAttribute("viewBox", `${x} ${y} ${visibleWidth} ${visibleHeight}`);
}

function setDiagnosisTopologyScale(nextScale) {
  const scale = Math.min(2.5, Math.max(0.6, Math.round(nextScale * 10) / 10));
  state.diagnosisTopologyScale = scale;
  applyTopologyView($("#diagnosisTopologySvg"), scale, state.diagnosisTopologyPanX, state.diagnosisTopologyPanY);
  $("#diagnosisZoomReset").textContent = `${Math.round(scale * 100)}%`;
  if (state.currentConversationId) {
    const record = ensureConversationRecord(state.currentConversationId);
    record.topologyScale = scale;
    record.topologyPanX = state.diagnosisTopologyPanX;
    record.topologyPanY = state.diagnosisTopologyPanY;
  }
}

function resetDiagnosisTopologyView() {
  state.diagnosisTopologyPanX = 0;
  state.diagnosisTopologyPanY = 0;
  setDiagnosisTopologyScale(1);
}

function showDiagnosisTopology(topology, payload) {
  state.currentDiagnosisTopology = topology;
  state.currentDiagnosisTopologyPayload = payload;
  renderTopology(topology, $("#diagnosisTopologySvg"));
  resetDiagnosisTopologyView();
  $("#diagnosisTopologyPhase").textContent = `${payload.phase || "拓扑数据"} · ${topology.devices.length} 个节点 / ${topology.links.length} 条连线`;
  $("#diagnosisTopologyEmpty").classList.add("hidden");
  $("#diagnosisTopologyContent").classList.remove("hidden");
  setDiagnosisRailExpanded(true);
  if (state.currentConversationId) {
    const record = ensureConversationRecord(state.currentConversationId);
    record.topology = topology;
    record.topologyPayload = payload;
  }
}

function getAgentRunState(run) {
  if (!agentRunStates.has(run)) {
    agentRunStates.set(run, { thinkingCard: null, toolCard: null, toolTimer: null, toolStartedAt: null, startedAt: null });
  }
  return agentRunStates.get(run);
}

function formatRunDuration(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  return `${hours}h ${minutes}m ${totalSeconds % 60}s`;
}

function finishThinkingBlock(runState) {
  if (!runState.thinkingCard) return;
  runState.thinkingCard.classList.remove("streaming");
  runState.thinkingCard = null;
}

function createThinkingCard(run) {
  const block = document.createElement("section");
  block.className = "thinking-stream streaming";
  block.innerHTML = `<p class="thinking-stream-content"></p>`;
  $(".steps", run).append(block);
  return block;
}

function appendThinkingContent(run, content) {
  const runState = getAgentRunState(run);
  if (!runState.thinkingCard) runState.thinkingCard = createThinkingCard(run);
  $(".thinking-stream-content", runState.thinkingCard).textContent += String(content ?? "");
}

function toolNameFromContent(content) {
  const parsed = parsePossibleJson(content);
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && parsed.tool) return String(parsed.tool);
  if (typeof content !== "string") return "工具调用";
  const candidate = content.split(/[｜:：]/, 1)[0].trim();
  return candidate && candidate.length <= 64 ? candidate : "工具调用";
}

function toolDetailFromContent(content) {
  if (typeof content !== "string") return serializeRawData(content);
  const separatorIndex = content.indexOf("｜");
  return separatorIndex >= 0 ? content.slice(separatorIndex + 1).trim() : content;
}

function createToolProcessCard(run, content) {
  const card = document.createElement("section");
  card.className = "step-card tool-process-card";
  card.innerHTML = `<header class="step-header"><span class="step-icon">↯</span><div><strong>工具调用</strong><br><small class="tool-name">${escapeHtml(toolNameFromContent(content))}</small></div><span class="step-status tool-status">等待执行</span></header><div class="tool-process-body"><div class="tool-progress"><div data-tool-stage="tool_pending"><i>1</i><span>已创建</span></div><span></span><div data-tool-stage="tool_running"><i>2</i><span>执行中</span></div><span></span><div data-tool-stage="tool_completed"><i>3</i><span>已完成</span></div></div><div class="tool-live-state"><i class="tool-live-indicator"></i><span class="tool-state-copy"></span><time class="tool-elapsed"></time></div><div class="tool-result hidden"></div></div>`;
  $(".steps", run).append(card);
  return card;
}

function stopToolTimer(runState) {
  if (runState.toolTimer !== null) window.clearInterval(runState.toolTimer);
  runState.toolTimer = null;
}

function updateToolElapsed(card, runState) {
  if (!runState.toolStartedAt) return;
  const seconds = Math.max(0, (performance.now() - runState.toolStartedAt) / 1000);
  $(".tool-elapsed", card).textContent = `${seconds.toFixed(1)} 秒`;
}

function startToolTimer(card, runState) {
  stopToolTimer(runState);
  runState.toolStartedAt = performance.now();
  updateToolElapsed(card, runState);
  runState.toolTimer = window.setInterval(() => updateToolElapsed(card, runState), 100);
}

function updateToolProcessCard(card, tag, content, runState, historical = false) {
  const order = ["tool_pending", "tool_running", "tool_completed"];
  const currentIndex = Math.max(0, order.indexOf(tag));
  const labels = { tool_pending: "等待执行", tool_running: "正在执行", tool_completed: "执行完成" };
  $$("[data-tool-stage]", card).forEach((stage, index) => {
    stage.classList.toggle("done", index < currentIndex || tag === "tool_completed");
    stage.classList.toggle("active", index === currentIndex && tag !== "tool_completed");
  });
  card.dataset.toolState = tag;
  $(".tool-status", card).textContent = labels[tag] || "状态更新";
  $(".tool-state-copy", card).textContent = toolDetailFromContent(content);

  if (tag === "tool_pending") {
    stopToolTimer(runState);
    runState.toolStartedAt = null;
    $(".tool-elapsed", card).textContent = "";
  } else if (tag === "tool_running") {
    if (!historical) startToolTimer(card, runState);
  } else if (tag === "tool_completed") {
    if (!historical) updateToolElapsed(card, runState);
    stopToolTimer(runState);
    runState.toolStartedAt = null;
    $(".tool-state-copy", card).textContent = "工具执行完成，已返回结果";
    const result = $(".tool-result", card);
    result.innerHTML = "";
    const output = renderRawContent(parsePossibleJson(content));
    output.classList.add("tool-event-content");
    result.append(output);
    result.classList.remove("hidden");
  }
}

function appendToolEvent(run, tag, content, historical = false) {
  const runState = getAgentRunState(run);
  finishThinkingBlock(runState);
  if (tag === "tool_pending" && runState.toolCard) {
    stopToolTimer(runState);
    $(".tool-status", runState.toolCard).textContent = "流程已中断";
    runState.toolCard.dataset.toolState = "interrupted";
    runState.toolCard.classList.add("interrupted");
    runState.toolCard = null;
  }
  if (!runState.toolCard) {
    runState.toolCard = createToolProcessCard(run, content);
  }
  updateToolProcessCard(runState.toolCard, tag, content, runState, historical);
  if (tag === "tool_completed") runState.toolCard = null;
}

function appendFinalResult(run, content) {
  const runState = getAgentRunState(run);
  finishThinkingBlock(runState);
  const response = document.createElement("article");
  response.className = "agent-final-response";
  if (typeof content === "string") {
    response.classList.add("markdown");
    response.innerHTML = markdownToHtml(content);
  } else {
    const output = document.createElement("pre");
    output.className = "agent-final-raw";
    output.textContent = serializeRawData(content);
    response.append(output);
  }
  $(".steps", run).append(response);
}

function appendLegacyAgentStep(run, payload) {
  const runState = getAgentRunState(run);
  finishThinkingBlock(runState);
  const block = document.createElement("section");
  block.className = "agent-phase-message";
  block.innerHTML = `<div class="agent-phase-indicator"><span>当前阶段</span><strong>${escapeHtml(payload.phase || "智能体处理")}</strong></div>`;
  const content = document.createElement("div");
  content.className = "agent-phase-content";
  if (typeof payload.raw === "string") {
    const text = document.createElement("p");
    text.textContent = payload.raw;
    content.append(text);
  } else {
    content.append(renderRawContent(payload.raw));
  }
  block.append(content);
  $(".steps", run).append(block);
}

function renderAgentEvent(run, payload, { historical = false } = {}) {
  const tag = agentEventTag(payload);
  const content = agentEventContent(payload);
  if (tag === "思考") appendThinkingContent(run, content);
  else if (["tool_pending", "tool_running", "tool_completed"].includes(tag)) appendToolEvent(run, tag, content, historical);
  else if (tag === "最终结果") appendFinalResult(run, content);
  else appendLegacyAgentStep(run, payload);
  return tag;
}

function finishAgentRun(run) {
  const runState = getAgentRunState(run);
  const duration = $(".run-duration", run);
  if (duration && runState.startedAt !== null) {
    duration.textContent = `耗时 ${formatRunDuration(performance.now() - runState.startedAt)}`;
    runState.startedAt = null;
  }
  finishThinkingBlock(runState);
  stopToolTimer(runState);
  runState.toolStartedAt = null;
  if (runState.toolCard) {
    $(".tool-status", runState.toolCard).textContent = "流程已中断";
    runState.toolCard.dataset.toolState = "interrupted";
    $(".tool-state-copy", runState.toolCard).textContent = "工具执行被中断";
    runState.toolCard.classList.add("interrupted");
    runState.toolCard = null;
  }
}

function appendAgentStep(run, payload) {
  const shouldFollow = isNearChatBottom();
  const tag = renderAgentEvent(run, payload);

  const topology = extractTopologyEvent(payload);
  if (topology) showDiagnosisTopology(topology, payload);
  if (shouldFollow) {
    if (tag === "思考") $("#chatScroll").scrollTop = $("#chatScroll").scrollHeight;
    else scrollChatToBottom(true);
  }
  if (tag !== "思考") saveCurrentConversationState();
}

function parseSseBlock(block) {
  const lines = block.split("\n");
  let event = "message";
  let id = 0;
  const data = [];
  lines.forEach(line => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("id:")) id = Number(line.slice(3).trim()) || 0;
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  });
  if (!data.length) return null;
  try { return { event, id, data: JSON.parse(data.join("\n")) }; }
  catch { return null; }
}

async function sendDiagnosis(question) {
  return sendBackendDiagnosis(question);
}

function resizeComposer() {
  const textarea = $("#questionInput");
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(115, textarea.scrollHeight)}px`;
}

function riskLevelClass(level) {
  if (level.includes("高")) return "high";
  if (level.includes("中")) return "medium";
  return "low";
}

function renderRiskList(filter = "全部", keyword = "") {
  const list = $("#riskList");
  list.innerHTML = "";
  const filtered = state.risks.filter(risk => (filter === "全部" || risk.level === filter) && (!keyword || `${risk.title}${risk.scope}`.toLowerCase().includes(keyword.toLowerCase())));
  if (state.perceptionMode === "tasks") $("#riskCount").textContent = `${filtered.length} 项`;
  filtered.forEach(risk => {
    const row = document.createElement("article");
    row.className = "risk-row";
    row.dataset.riskId = risk.id;
    row.innerHTML = `<div class="risk-row-copy"><div class="risk-row-title"><h3>${escapeHtml(risk.title)}</h3><span class="risk-level ${riskLevelClass(risk.level)}">${escapeHtml(risk.level)}</span></div><p>${escapeHtml(risk.scope)}</p><small>任务模板 · 支持重复执行</small></div><button class="risk-dispatch-button" type="button"><span>▶</span> 下发</button>`;
    $(".risk-dispatch-button", row).addEventListener("click", event => dispatchRiskTemplate(risk.id, event.currentTarget));
    list.append(row);
  });
}

function upsertExecution(execution) {
  if (!execution?.id) return null;
  const index = state.executions.findIndex(item => item.id === execution.id);
  if (index >= 0) {
    state.executions[index] = { ...state.executions[index], ...execution };
  } else {
    state.executions.unshift(execution);
  }
  return state.executions.find(item => item.id === execution.id);
}

function executionStatusLabel(status) {
  if (status === "completed") return "已生成报告";
  if (status === "failed") return "执行失败";
  return "感知中";
}

function updateExecutionMetrics() {
  $("#totalExecutionCount").textContent = String(state.executions.length);
  $("#highRiskExecutionCount").textContent = String(state.executions.filter(item => String(item.level).includes("高")).length);
  $("#runningExecutionCount").textContent = String(state.executions.filter(item => item.status === "running").length);
  $("#completedExecutionCount").textContent = String(state.executions.filter(item => item.status === "completed").length);
}

function setPerceptionMode(mode, filter = state.executionFilter) {
  state.perceptionMode = mode;
  state.executionFilter = filter;
  const showTasks = mode === "tasks";
  $("#taskLibraryTab").classList.toggle("active", showTasks);
  $("#executionHistoryTab").classList.toggle("active", !showTasks);
  $("#taskLibraryContent").classList.toggle("hidden", !showTasks);
  $("#executionHistoryContent").classList.toggle("hidden", showTasks);
  $("#riskPanelTitle").textContent = showTasks ? "隐患任务库" : "隐患执行记录";
  if (showTasks) {
    renderRiskList($(".filter-chip.active")?.textContent || "全部", $("#riskSearch").value.trim());
  } else {
    renderExecutionList();
  }
}

function renderExecutionList() {
  const list = $("#executionList");
  list.innerHTML = "";
  const records = state.executions.filter(item => state.executionFilter !== "high" || String(item.level).includes("高"));
  $("#executionFilterLabel").textContent = state.executionFilter === "high" ? "高风险执行记录" : "全部执行记录";
  $("#riskCount").textContent = `${records.length} 条`;

  if (!records.length) {
    list.innerHTML = '<div class="event-empty"><span>◌</span><p>暂无符合条件的执行记录。</p></div>';
    return;
  }

  records.forEach(execution => {
    const row = document.createElement("article");
    row.className = `execution-row${execution.id === state.selectedExecutionId ? " active" : ""}`;
    row.dataset.executionId = execution.id;
    const hasDiagnosisReport = state.features.deepDiagnosis && Boolean(execution.diagnosis?.report_markdown || execution.diagnosis?.turns?.some(turn => turn.status === "completed"));
    const diagnosisDisabled = execution.status !== "completed" || !execution.report_markdown;
    const diagnosisActions = state.features.deepDiagnosis
      ? `<div class="execution-actions"><button class="execution-diagnose" type="button"${diagnosisDisabled ? " disabled" : ""}>✦ 诊断</button>${hasDiagnosisReport ? '<button class="execution-diagnosis-report" type="button">▤ 诊断报告</button>' : ""}</div>`
      : "";
    row.innerHTML = `<button class="execution-open" type="button"><div class="execution-row-head"><span class="execution-status ${escapeHtml(execution.status || "running")}">${escapeHtml(executionStatusLabel(execution.status))}</span><time>${escapeHtml(execution.updated_at || execution.created_at || "刚刚")}</time></div><h3>${escapeHtml(execution.title || "未命名隐患执行")}</h3><p>${escapeHtml(execution.id)} · ${escapeHtml(execution.scope || "范围未知")}</p><div class="execution-row-foot"><span class="risk-level ${riskLevelClass(execution.level || "低风险")}">${escapeHtml(execution.level || "未分级")}</span><span>${hasDiagnosisReport ? "已有深度诊断" : "打开详情 →"}</span></div></button>${diagnosisActions}`;
    $(".execution-open", row).addEventListener("click", () => selectExecution(execution.id));
    $(".execution-diagnose", row)?.addEventListener("click", () => openDeepDiagnosis(execution.id, false));
    $(".execution-diagnosis-report", row)?.addEventListener("click", () => openDeepDiagnosis(execution.id, true));
    list.append(row);
  });
}

function renderSelectedExecution(execution, preserveTopologyView = false) {
  if (!execution) {
    $("#selectedRiskTitle").textContent = "请选择一条执行记录";
    $("#selectedRiskDescription").textContent = "任务模板只负责下发；拓扑、报告和事件流均属于某一次独立执行。";
    $("#selectedRiskMeta").innerHTML = "<span>未选择</span>";
    $("#eventState").textContent = "等待任务";
    $("#eventEmpty").classList.remove("hidden");
    $("#eventTimeline").classList.add("hidden");
    clearPerceptionTopology("打开一条执行过的隐患后，这里展示该实例对应的拓扑。");
    hidePerceptionReport();
    $("#selectedRiskSummary").innerHTML = "";
    return;
  }

  $("#selectedRiskTitle").textContent = execution.title;
  $("#selectedRiskDescription").textContent = execution.description || "暂无任务描述";
  $("#selectedRiskMeta").innerHTML = `<span>${escapeHtml(execution.level || "未分级")}</span><span>${escapeHtml(execution.scope || "范围未知")}</span><span>${escapeHtml(executionStatusLabel(execution.status))}</span><span title="${escapeHtml(execution.id)}">ID · ${escapeHtml(execution.id)}</span><span>${escapeHtml(execution.created_at || "刚刚")}</span>`;
  $("#eventState").textContent = execution.status === "completed" ? "报告已生成" : "实时更新";

  if (execution.events?.length) {
    renderTimeline(execution.events, execution.updated_at);
  } else {
    $("#eventEmpty").classList.remove("hidden");
    $("#eventTimeline").classList.add("hidden");
    $("#eventTimeline").innerHTML = "";
  }

  if (execution.topology) {
    showPerceptionTopology(execution.topology, execution, preserveTopologyView);
  } else {
    clearPerceptionTopology("该执行正在等待感知服务返回 topology 标签数据。");
    $("#selectedRiskSummary").innerHTML = `<h3>${escapeHtml(execution.title)}</h3><p>${escapeHtml(execution.description || "暂无任务描述")}</p><div class="summary-signals"><span>${escapeHtml(executionStatusLabel(execution.status))}</span><span>等待拓扑事件</span></div>`;
  }

  if (execution.report_markdown) showPerceptionReport(execution.report_markdown);
  else hidePerceptionReport();
}

function selectExecution(executionId) {
  const execution = state.executions.find(item => item.id === executionId);
  if (!execution) return;
  state.selectedExecutionId = executionId;
  if (state.perceptionMode === "executions") renderExecutionList();
  renderSelectedExecution(execution);
}

function layerForType(type) {
  const value = String(type || "").toLowerCase();
  if (/spine|core|核心/.test(value)) return 0;
  if (/server|host|compute|业务|服务器|主机|集群/.test(value)) return 2;
  return 1;
}

function typeLabel(type) {
  const value = String(type || "device");
  if (/spine|core/i.test(value)) return "SPINE";
  if (/leaf|tor|access/i.test(value)) return "LEAF";
  if (/server|host|group|compute/i.test(value)) return "SERVER GROUP";
  return value.toUpperCase().slice(0, 14);
}

function topologyPositions(devices, width, height) {
  const groups = new Map();
  devices.forEach(device => {
    const layer = layerForType(device.device_type);
    if (!groups.has(layer)) groups.set(layer, []);
    groups.get(layer).push(device);
  });
  const usedLayers = [...groups.keys()].sort();
  const yByLayer = usedLayers.length === 1
    ? new Map([[usedLayers[0], height / 2]])
    : new Map(usedLayers.map((layer, index) => [layer, 86 + index * ((height - 172) / Math.max(usedLayers.length - 1, 1))]));
  const positions = new Map();
  groups.forEach((items, layer) => {
    items.forEach((device, index) => {
      positions.set(device.name, {
        x: width * (index + 1) / (items.length + 1),
        y: yByLayer.get(layer),
      });
    });
  });
  return positions;
}

function appendDeviceIcon(group) {
  // 原界面的交换机图标，故障与隐患拓扑共用。
  group.append(svgElement("rect", { x: -43, y: -35, width: 86, height: 70, rx: 17, class: "node-halo" }));
  group.append(svgElement("rect", { x: -32, y: -25, width: 64, height: 50, rx: 10, class: "node-body" }));
  group.append(svgElement("path", { d: "M-17 -9h34M-17 0h34M-17 9h34M-12 -13v8M12 -4v8M-12 5v8", class: "node-detail" }));
}

function renderTopology(topology, svg = $("#topologySvg")) {
  if (topology.native) return renderNetworkTopology(topology, svg);
  svg.innerHTML = "";
  const width = 760;
  const height = 470;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const devices = topology?.devices || [];
  const links = topology?.links || [];
  const positions = topologyPositions(devices, width, height);

  links.forEach(link => {
    const source = positions.get(link.source);
    const target = positions.get(link.target);
    if (!source || !target) return;
    svg.append(svgElement("line", {
      x1: source.x, y1: source.y, x2: target.x, y2: target.y,
      class: `topology-link${link.has_problem ? " problem" : ""}`,
    }));
  });

  devices.forEach(device => {
    const position = positions.get(device.name);
    const group = svgElement("g", { transform: `translate(${position.x} ${position.y})`, class: `topology-node${device.has_problem ? " problem" : ""}` });
    appendDeviceIcon(group);
    const label = svgElement("text", { x: 0, y: 48, class: "node-label" });
    label.textContent = device.name;
    const type = svgElement("text", { x: 0, y: 62, class: "node-type" });
    type.textContent = typeLabel(device.device_type);
    group.append(label, type);
    svg.append(group);
  });
}

function clearPerceptionTopology(message = "下发隐患后，收到拓扑事件才会展示。") {
  state.perceptionTopologyPanX = 0;
  state.perceptionTopologyPanY = 0;
  $("#topologySvg").innerHTML = "";
  $("#topologySvg").classList.add("hidden");
  $("#topologyBadge").classList.add("hidden");
  $("#perceptionZoomControls").classList.add("hidden");
  $("#topologyEmpty").classList.remove("hidden");
  $("#topologyEmpty p").textContent = message;
  setPerceptionTopologyScale(1);
}

function setPerceptionTopologyScale(nextScale) {
  const scale = Math.min(2.5, Math.max(0.6, Math.round(nextScale * 10) / 10));
  state.perceptionTopologyScale = scale;
  applyTopologyView($("#topologySvg"), scale, state.perceptionTopologyPanX, state.perceptionTopologyPanY);
  $("#perceptionZoomReset").textContent = `${Math.round(scale * 100)}%`;
}

function resetPerceptionTopologyView() {
  state.perceptionTopologyPanX = 0;
  state.perceptionTopologyPanY = 0;
  setPerceptionTopologyScale(1);
}

function showPerceptionTopology(topology, execution, preserveView = false) {
  renderTopology(topology, $("#topologySvg"));
  if (preserveView) setPerceptionTopologyScale(state.perceptionTopologyScale);
  else resetPerceptionTopologyView();
  $("#topologyEmpty").classList.add("hidden");
  $("#topologySvg").classList.remove("hidden");
  $("#topologyBadge").classList.remove("hidden");
  $("#perceptionZoomControls").classList.remove("hidden");
  if (execution) {
    $("#selectedRiskSummary").innerHTML = `<h3>${escapeHtml(execution.title)}</h3><p>${escapeHtml(execution.description || "暂无任务描述")}</p><div class="summary-signals"><span>${topology.devices.length} 台/组设备</span><span>${topology.links.length} 条连线</span><span>执行 ID · ${escapeHtml(execution.id)}</span></div>`;
  }
}

function setDeepTopologyScale(nextScale) {
  const scale = Math.min(2.5, Math.max(0.6, Math.round(nextScale * 10) / 10));
  state.deepTopologyScale = scale;
  applyTopologyView($("#deepTopologySvg"), scale, state.deepTopologyPanX, state.deepTopologyPanY);
  $("#deepZoomReset").textContent = `${Math.round(scale * 100)}%`;
}

function resetDeepTopologyView() {
  state.deepTopologyPanX = 0;
  state.deepTopologyPanY = 0;
  setDeepTopologyScale(1);
}

function clearDeepTopology(message = "收到诊断流中的 topology 标签后自动绘制。") {
  state.deepDiagnosisTopology = null;
  state.deepTopologyPanX = 0;
  state.deepTopologyPanY = 0;
  $("#deepTopologySvg").innerHTML = "";
  $("#deepTopologySvg").classList.add("hidden");
  $("#deepZoomControls").classList.add("hidden");
  $("#deepTopologyEmpty").classList.remove("hidden");
  $("#deepTopologyEmpty p").textContent = message;
  $("#deepTopologyState").textContent = "等待数据";
  setDeepTopologyScale(1);
}

function showDeepTopology(topology, preserveView = false) {
  state.deepDiagnosisTopology = topology;
  renderTopology(topology, $("#deepTopologySvg"));
  if (preserveView) setDeepTopologyScale(state.deepTopologyScale);
  else resetDeepTopologyView();
  $("#deepTopologyEmpty").classList.add("hidden");
  $("#deepTopologySvg").classList.remove("hidden");
  $("#deepZoomControls").classList.remove("hidden");
  $("#deepTopologyState").textContent = `${topology.devices.length} 节点 · ${topology.links.length} 连线`;
}

function hidePerceptionReport() {
  $("#reportMarkdown").innerHTML = "";
  $("#reportCard").classList.add("hidden");
  $("#reportResizeHandle").classList.add("hidden");
  $("#topologyPanel").classList.remove("report-visible", "report-collapsed");
}

function showPerceptionReport(markdown) {
  $("#reportMarkdown").innerHTML = markdownToHtml(markdown);
  $("#reportCard").classList.remove("hidden", "collapsed");
  $("#reportResizeHandle").classList.remove("hidden");
  $("#topologyPanel").classList.add("report-visible");
  $("#topologyPanel").classList.remove("report-collapsed");
  $("#toggleReport").textContent = "收起";
}

function startTopologyPan(kind, event) {
  if (event.button !== 0) return;
  const configs = {
    diagnosis: {
      svg: $("#diagnosisTopologySvg"),
      getScale: () => state.diagnosisTopologyScale,
      getPan: () => [state.diagnosisTopologyPanX, state.diagnosisTopologyPanY],
      setPan: (x, y) => { state.diagnosisTopologyPanX = x; state.diagnosisTopologyPanY = y; },
      apply: setDiagnosisTopologyScale,
    },
    perception: {
      svg: $("#topologySvg"),
      getScale: () => state.perceptionTopologyScale,
      getPan: () => [state.perceptionTopologyPanX, state.perceptionTopologyPanY],
      setPan: (x, y) => { state.perceptionTopologyPanX = x; state.perceptionTopologyPanY = y; },
      apply: setPerceptionTopologyScale,
    },
    deep: {
      svg: $("#deepTopologySvg"),
      getScale: () => state.deepTopologyScale,
      getPan: () => [state.deepTopologyPanX, state.deepTopologyPanY],
      setPan: (x, y) => { state.deepTopologyPanX = x; state.deepTopologyPanY = y; },
      apply: setDeepTopologyScale,
    },
  };
  const config = configs[kind];
  if (!config) return;
  const svg = config.svg;
  if (!svg.childElementCount || svg.classList.contains("hidden")) return;
  event.preventDefault();
  const startX = event.clientX;
  const startY = event.clientY;
  const [startPanX, startPanY] = config.getPan();
  const scale = config.getScale();
  svg.classList.add("panning");

  const onMove = moveEvent => {
    const rect = svg.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const panX = startPanX - (moveEvent.clientX - startX) * (760 / scale / rect.width);
    const panY = startPanY - (moveEvent.clientY - startY) * (470 / scale / rect.height);
    config.setPan(panX, panY);
    config.apply(scale);
  };
  const onUp = () => {
    svg.classList.remove("panning");
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp, { once: true });
}

function startReportResize(event) {
  if (!$("#topologyPanel").classList.contains("report-visible") || $("#topologyPanel").classList.contains("report-collapsed")) return;
  event.preventDefault();
  const stageTop = $("#topologyStage").getBoundingClientRect().top;
  const panelBottom = $("#topologyPanel").getBoundingClientRect().bottom;
  document.body.classList.add("resizing-report");

  const onMove = moveEvent => {
    const maxTopologyHeight = Math.max(180, panelBottom - stageTop - 190);
    const topologyHeight = Math.round(Math.min(maxTopologyHeight, Math.max(180, moveEvent.clientY - stageTop)));
    $("#topologyPanel").style.setProperty("--perception-topology-height", `${topologyHeight}px`);
    $("#reportResizeHandle").setAttribute("aria-valuenow", String(topologyHeight));
  };
  const onUp = () => {
    document.body.classList.remove("resizing-report");
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp, { once: true });
}

function defaultDeepDiagnosisQuestion() {
  return "基于这份隐患分析报告进一步诊断，确认根因并给出处置方案。";
}

function renderDeepSourceExecution(execution) {
  $("#deepSourceExecutionId").textContent = `执行 ID · ${execution.id}`;
  $("#deepSourceTitle").textContent = execution.title;
  $("#deepSourceMeta").innerHTML = `<span>${escapeHtml(execution.level || "未分级")}</span><span>${escapeHtml(execution.scope || "范围未知")}</span><span>隐患报告已生成</span>`;
  $("#deepSourceReport").innerHTML = markdownToHtml(execution.report_markdown || "暂无隐患报告");
}

function appendDeepUserMessage(question) {
  const message = document.createElement("div");
  message.className = "message user-message";
  message.innerHTML = `<div class="user-bubble">${escapeHtml(question)}</div><span class="user-meta">隐患报告作为输入上下文</span>`;
  $("#deepMessages").append(message);
}

function createDeepAgentRun(completed = false) {
  const run = document.createElement("div");
  run.className = "message agent-run";
  run.innerHTML = `<div class="run-label"><i></i><span>${completed ? "DCN Copilot 已完成深度诊断" : "DCN Copilot 正在进行深度诊断"}</span></div><div class="steps"></div>${completed ? "" : '<div class="typing-card"><span class="typing-dots"><i></i><i></i><i></i></span><span>正在等待智能体事件…</span></div><time class="run-duration"></time>'}`;
  if (!completed) getAgentRunState(run).startedAt = performance.now();
  $("#deepMessages").append(run);
  return run;
}

function appendDeepAgentStep(run, payload, preserveTopologyView = false, autoScroll = true) {
  const scroller = $("#deepChatScroll");
  const shouldFollow = autoScroll && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 140;
  renderAgentEvent(run, payload, { historical: !autoScroll });

  const topology = extractTopologyEvent(payload);
  if (topology) showDeepTopology(topology, preserveTopologyView);
  if (shouldFollow) requestAnimationFrame(() => scroller.scrollTo({ top: scroller.scrollHeight }));
}

function renderDeepDiagnosisHistory(execution) {
  const diagnosis = execution.diagnosis;
  const turns = diagnosis?.turns?.length ? diagnosis.turns : diagnosis?.steps?.length ? [{
    question: diagnosis.question || defaultDeepDiagnosisQuestion(),
    status: diagnosis.status,
    steps: diagnosis.steps,
  }] : [];
  if (!turns.length) {
    showToast("暂无诊断报告", "请先对该隐患执行一次深度诊断。");
    return;
  }
  $("#deepChatEmpty").classList.add("hidden");
  $("#deepMessages").innerHTML = "";
  clearDeepTopology("正在恢复历史诊断拓扑…");
  turns.forEach(turn => {
    appendDeepUserMessage(turn.question || defaultDeepDiagnosisQuestion());
    const run = createDeepAgentRun(turn.status === "completed");
    turn.steps?.forEach(step => appendDeepAgentStep(run, step, true, false));
    finishAgentRun(run);
    if (turn.status !== "completed") $(".typing-card", run)?.remove();
  });
  $("#deepDiagnosisState").textContent = "历史结果";
  $("#deepWorkflowState").textContent = "已完成";
  $("#deepRerunButton").classList.remove("hidden");
  requestAnimationFrame(() => {
    const scroller = $("#deepChatScroll");
    scroller.scrollTop = scroller.scrollHeight;
  });
}

function openDeepDiagnosisHome() {
  if (!state.features.deepDiagnosis) return;
  const current = state.executions.find(item => item.id === state.deepExecutionId);
  const latestWithReport = state.executions.find(item => item.diagnosis?.report_markdown);
  const execution = current || latestWithReport;
  setView("deepDiagnosis");
  if (!execution) return;
  state.deepExecutionId = execution.id;
  renderDeepSourceExecution(execution);
  if (!$("#deepMessages").children.length && execution.diagnosis) renderDeepDiagnosisHistory(execution);
}

async function openDeepDiagnosis(executionId, useHistory) {
  if (!state.features.deepDiagnosis) return;
  if (state.deepDiagnosing) {
    showToast("诊断正在进行", "请等待当前深度诊断完成。");
    return;
  }
  const execution = state.executions.find(item => item.id === executionId);
  if (!execution?.report_markdown) {
    showToast("隐患报告尚未生成", "感知事件流完成后才能进行深度诊断。");
    return;
  }
  state.deepExecutionId = executionId;
  renderDeepSourceExecution(execution);
  setView("deepDiagnosis");
  if (useHistory) {
    renderDeepDiagnosisHistory(execution);
  } else {
    if (execution.diagnosis?.steps?.length) {
      renderDeepDiagnosisHistory(execution);
    } else {
      $("#deepMessages").innerHTML = "";
      clearDeepTopology("正在等待诊断服务返回 topology 标签数据…");
    }
    await runDeepDiagnosis(defaultDeepDiagnosisQuestion());
  }
}

async function runDeepDiagnosis(question) {
  if (!state.features.deepDiagnosis) return;
  if (state.deepDiagnosing || !question.trim()) return;
  const execution = state.executions.find(item => item.id === state.deepExecutionId);
  if (!execution) return;

  state.deepDiagnosing = true;
  $("#deepSendButton").disabled = true;
  $("#deepRerunButton").classList.add("hidden");
  $("#deepDiagnosisState").textContent = "流式诊断中";
  $("#deepWorkflowState").textContent = "进行中";
  $("#deepChatEmpty").classList.add("hidden");
  $("#deepQuestionInput").value = "";
  appendDeepUserMessage(question.trim());
  const run = createDeepAgentRun();
  const steps = [];
  let completedDiagnosis = null;

  try {
    const response = await fetch(`/api/executions/${encodeURIComponent(execution.id)}/diagnose`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question.trim() }),
    });
    if (!response.ok || !response.body) throw new Error(`深度诊断请求失败：${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      blocks.forEach(block => {
        const message = parseSseBlock(block);
        if (message?.event === "agent") {
          steps.push(message.data);
          appendDeepAgentStep(run, message.data);
        } else if (message?.event === "done") {
          completedDiagnosis = message.data;
        }
      });
      if (done) break;
    }

    const previousDiagnosis = execution.diagnosis;
    const previousTurns = previousDiagnosis?.turns?.length ? previousDiagnosis.turns : previousDiagnosis?.steps?.length ? [{
      id: `legacy-${execution.id}`,
      question: previousDiagnosis.question || defaultDeepDiagnosisQuestion(),
      status: previousDiagnosis.status,
      steps: previousDiagnosis.steps,
    }] : [];
    const diagnosis = completedDiagnosis?.turns?.length ? completedDiagnosis : {
      ...(previousDiagnosis || {}),
      status: "completed",
      question: question.trim(),
      steps: [...(previousDiagnosis?.steps || []), ...steps],
      turns: [...previousTurns, {
        id: `turn-${Date.now()}`,
        question: question.trim(),
        status: "completed",
        steps,
      }],
      topology: state.deepDiagnosisTopology || previousDiagnosis?.topology || null,
      report_markdown: agentEventContent([...steps].reverse().find(step => agentEventTag(step) === "最终结果")) || previousDiagnosis?.report_markdown || null,
      updated_at: new Date().toLocaleTimeString("zh-CN"),
    };
    upsertExecution({ ...execution, diagnosis });
    $(".run-label span", run).textContent = "DCN Copilot 已完成深度诊断";
    $("#deepDiagnosisState").textContent = "诊断已完成";
    $("#deepWorkflowState").textContent = "已完成";
    $("#deepRerunButton").classList.remove("hidden");
    if (state.perceptionMode === "executions") renderExecutionList();
    showToast("深度诊断完成", "诊断结果已保存到当前隐患执行记录。 ");
  } catch (error) {
    const errorCard = document.createElement("article");
    errorCard.className = "typing-card";
    errorCard.textContent = error.message || "深度诊断服务暂时不可用";
    $(".steps", run).append(errorCard);
    $("#deepDiagnosisState").textContent = "诊断失败";
    $("#deepWorkflowState").textContent = "执行失败";
  } finally {
    finishAgentRun(run);
    $(".typing-card", run)?.remove();
    state.deepDiagnosing = false;
    $("#deepSendButton").disabled = false;
  }
}

async function loadRisks() {
  if (!state.features.perception) return;
  try {
    const response = await fetch("/api/risks");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.risks = await response.json();
    renderRiskList();
  } catch {
    $("#riskList").innerHTML = '<div class="event-empty"><p>隐患数据加载失败，请确认 Python 服务已启动。</p></div>';
  }
}

async function loadExecutions() {
  if (!state.features.perception && !state.features.deepDiagnosis) return;
  try {
    const response = await fetch("/api/executions");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.executions = await response.json();
    updateExecutionMetrics();
    if (state.perceptionMode === "executions") renderExecutionList();
    if (state.selectedExecutionId) {
      const selected = state.executions.find(item => item.id === state.selectedExecutionId);
      if (selected) renderSelectedExecution(selected, true);
    }
  } catch {
    if (state.perceptionMode === "executions") {
      $("#executionList").innerHTML = '<div class="event-empty"><p>执行记录加载失败，请确认 Python 服务已启动。</p></div>';
    }
  }
}

function showToast(title, message) {
  $("#toastTitle").textContent = title;
  $("#toastMessage").textContent = message;
  $("#toast").classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => $("#toast").classList.remove("show"), 6000);
}

function renderTimeline(events, timestamp) {
  const timeline = $("#eventTimeline");
  timeline.innerHTML = "";
  events.forEach((event, index) => {
    const [title, detail] = String(event).split("｜").map(part => part.trim());
    const item = document.createElement("li");
    const isAlert = /检测|风险确认|异常/.test(title);
    const isDone = /报告已生成/.test(title);
    item.className = `event-item${isAlert ? " alert" : ""}${isDone ? " done" : ""}`;
    item.innerHTML = `<strong>${escapeHtml(title)}${detail ? `<br><span style="color:#7187a2;font-weight:400">${escapeHtml(detail)}</span>` : ""}</strong><time>${escapeHtml(timestamp || new Date().toLocaleTimeString("zh-CN"))}</time>`;
    timeline.append(item);
  });
  $("#eventEmpty").classList.add("hidden");
  timeline.classList.remove("hidden");
}

function connectPerceptionStream() {
  if (!state.features.perception) return null;
  const source = new EventSource("/api/perception/stream");
  source.onmessage = event => {
    const payload = JSON.parse(event.data);
    if (!payload.execution_id) return;
    const existing = state.executions.find(item => item.id === payload.execution_id);
    const risk = state.risks.find(item => item.id === payload.risk_id);
    const topology = extractTopologyEvent(payload);
    const execution = upsertExecution({
      ...(existing || {}),
      id: payload.execution_id,
      template_id: payload.risk_id,
      title: payload.risk_title || existing?.title || risk?.title,
      description: existing?.description || risk?.description,
      scope: existing?.scope || risk?.scope,
      level: existing?.level || risk?.level,
      status: payload.state || existing?.status || "running",
      events: payload.events || existing?.events || [],
      topology: topology || existing?.topology || null,
      report_markdown: payload.report_markdown || existing?.report_markdown || null,
      created_at: existing?.created_at || "刚刚",
      updated_at: payload.timestamp || existing?.updated_at,
    });
    updateExecutionMetrics();
    if (state.perceptionMode === "executions") renderExecutionList();
    if (state.selectedExecutionId === execution.id) renderSelectedExecution(execution, !topology);

    const latest = execution.events?.at(-1) || "";
    if (latest.includes("检测到隐患")) {
      // 隐患仍是演示数据，不在真实问答页面弹出模拟告警。
      if ($("#perceptionView").classList.contains("active")) showToast("DEMO 模拟隐患", payload.risk_title);
      $("#navAlertDot").classList.remove("hidden");
    }
  };
}

async function dispatchRiskTemplate(riskId, button) {
  if (!state.features.perception) return;
  const risk = state.risks.find(item => item.id === riskId);
  if (!risk) return;
  button.disabled = true;
  button.classList.add("running");
  button.innerHTML = "<span>◌</span> 下发中";

  try {
    const response = await fetch(`/api/risks/${encodeURIComponent(risk.id)}/dispatch`, { method: "POST" });
    if (!response.ok) throw new Error(`下发失败：HTTP ${response.status}`);
    const result = await response.json();
    const streamed = state.executions.find(item => item.id === result.execution?.id);
    const execution = upsertExecution({
      ...result.execution,
      events: streamed?.events?.length ? streamed.events : result.execution?.events,
      topology: streamed?.topology || result.execution?.topology,
      report_markdown: streamed?.report_markdown || result.execution?.report_markdown,
      status: streamed?.status || result.execution?.status,
      updated_at: streamed?.updated_at || result.execution?.updated_at,
    });
    if (!execution) throw new Error("下发响应中缺少 execution.id");
    updateExecutionMetrics();
    setPerceptionMode("executions", "all");
    selectExecution(execution.id);
    showToast("任务已下发", risk.title);
  } catch (error) {
    showToast("下发失败", error.message);
  } finally {
    button.disabled = false;
    button.classList.remove("running");
    button.innerHTML = "<span>▶</span> 再次下发";
  }
}

function bindEvents() {
  $$(".nav-item[data-view]").forEach(button => button.addEventListener("click", () => {
    if (button.dataset.view === "deepDiagnosis") openDeepDiagnosisHome();
    else setView(button.dataset.view);
  }));
  $$(".suggestion").forEach(button => button.addEventListener("click", () => sendDiagnosis(button.dataset.prompt)));
  $("#composer").addEventListener("submit", event => {
    event.preventDefault();
    sendDiagnosis($("#questionInput").value);
  });
  $("#questionInput").addEventListener("input", resizeComposer);
  $("#questionInput").addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("#composer").requestSubmit();
    }
  });
  $("#newChatButton").addEventListener("click", () => {
    if (state.diagnosing) return;
    saveCurrentConversationState();
    createRecentConversation();
    $("#messages").innerHTML = "";
    $("#introCard").classList.remove("hidden");
    resetDiagnosisTopology();
    saveCurrentConversationState();
    updateSessionLabel();
    loadNetwork();
  });
  $("#diagnosisRailToggle").addEventListener("click", () => {
    setDiagnosisRailExpanded($("#diagnosisLayout").classList.contains("context-collapsed"));
  });
  $("#diagnosisRailResizer").addEventListener("pointerdown", startDiagnosisRailResize);
  $("#diagnosisRailResizer").addEventListener("dblclick", () => setDiagnosisRailWidth(330));
  window.addEventListener("resize", () => setDiagnosisRailWidth(state.diagnosisRailWidth));
  $("#diagnosisZoomIn").addEventListener("click", () => setDiagnosisTopologyScale(state.diagnosisTopologyScale + 0.2));
  $("#diagnosisZoomOut").addEventListener("click", () => setDiagnosisTopologyScale(state.diagnosisTopologyScale - 0.2));
  $("#diagnosisZoomReset").addEventListener("click", resetDiagnosisTopologyView);
  $("#diagnosisTopologySvg").addEventListener("pointerdown", event => startTopologyPan("diagnosis", event));
  $(".diagnosis-topology-stage").addEventListener("wheel", event => {
    if ($("#diagnosisTopologyContent").classList.contains("hidden")) return;
    event.preventDefault();
    setDiagnosisTopologyScale(state.diagnosisTopologyScale + (event.deltaY < 0 ? 0.1 : -0.1));
  }, { passive: false });
  $("#perceptionZoomIn").addEventListener("click", () => setPerceptionTopologyScale(state.perceptionTopologyScale + 0.2));
  $("#perceptionZoomOut").addEventListener("click", () => setPerceptionTopologyScale(state.perceptionTopologyScale - 0.2));
  $("#perceptionZoomReset").addEventListener("click", resetPerceptionTopologyView);
  $("#topologySvg").addEventListener("pointerdown", event => startTopologyPan("perception", event));
  $("#topologyStage").addEventListener("wheel", event => {
    if ($("#topologySvg").classList.contains("hidden")) return;
    event.preventDefault();
    setPerceptionTopologyScale(state.perceptionTopologyScale + (event.deltaY < 0 ? 0.1 : -0.1));
  }, { passive: false });
  $("#deepZoomIn").addEventListener("click", () => setDeepTopologyScale(state.deepTopologyScale + 0.2));
  $("#deepZoomOut").addEventListener("click", () => setDeepTopologyScale(state.deepTopologyScale - 0.2));
  $("#deepZoomReset").addEventListener("click", resetDeepTopologyView);
  $("#deepTopologySvg").addEventListener("pointerdown", event => startTopologyPan("deep", event));
  $("#deepTopologyStage").addEventListener("wheel", event => {
    if ($("#deepTopologySvg").classList.contains("hidden")) return;
    event.preventDefault();
    setDeepTopologyScale(state.deepTopologyScale + (event.deltaY < 0 ? 0.1 : -0.1));
  }, { passive: false });
  $("#reportResizeHandle").addEventListener("pointerdown", startReportResize);
  $("#reportResizeHandle").addEventListener("dblclick", () => $("#topologyPanel").style.removeProperty("--perception-topology-height"));
  $("#toastClose").addEventListener("click", () => $("#toast").classList.remove("show"));
  $("#toggleReport").addEventListener("click", () => {
    $("#reportCard").classList.toggle("collapsed");
    const collapsed = $("#reportCard").classList.contains("collapsed");
    $("#topologyPanel").classList.toggle("report-collapsed", collapsed);
    $("#toggleReport").textContent = collapsed ? "展开" : "收起";
  });
  $("#riskSearch").addEventListener("input", event => renderRiskList($(".filter-chip.active").textContent, event.target.value.trim()));
  $("#taskLibraryTab").addEventListener("click", () => setPerceptionMode("tasks"));
  $("#executionHistoryTab").addEventListener("click", () => setPerceptionMode("executions", "all"));
  $("#refreshExecutions").addEventListener("click", loadExecutions);
  $("#deepBackButton").addEventListener("click", () => {
    setView("perception");
    setPerceptionMode("executions", state.executionFilter);
    if (state.deepExecutionId) selectExecution(state.deepExecutionId);
  });
  $("#deepRerunButton").addEventListener("click", () => runDeepDiagnosis(defaultDeepDiagnosisQuestion()));
  $("#deepComposer").addEventListener("submit", event => {
    event.preventDefault();
    runDeepDiagnosis($("#deepQuestionInput").value || defaultDeepDiagnosisQuestion());
  });
  $("#deepQuestionInput").addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("#deepComposer").requestSubmit();
    }
  });
  $$(".metric-action").forEach(button => button.addEventListener("click", () => setPerceptionMode("executions", button.dataset.executionFilter || "all")));
  $$(".filter-chip").forEach(button => button.addEventListener("click", () => {
    $$(".filter-chip").forEach(item => item.classList.remove("active"));
    button.classList.add("active");
    renderRiskList(button.textContent, $("#riskSearch").value.trim());
  }));
}

async function initializeApp() {
  await loadFeatureConfig();
  bindEvents();
  initializeDiagnosis();
  await Promise.all([
    state.features.perception ? loadRisks() : Promise.resolve(),
    state.features.perception || state.features.deepDiagnosis ? loadExecutions() : Promise.resolve(),
  ]);
  if (state.features.perception) connectPerceptionStream();
}
