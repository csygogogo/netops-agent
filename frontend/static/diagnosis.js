// 故障诊断使用后端 opencode 风格事件（id/type/properties）；隐患页面继续使用原有演示协议。
let diagnosisAbort = null;
let diagnosisRunId = null;
let networkRequest = 0;
const terminalLabels = { completed: "已完成", needs_input: "等待补充信息", incomplete: "部分完成",
  error: "执行失败", timeout: "执行超时", cancelled: "已停止", interrupted: "连接中断" };

function updateSessionLabel() {
  $("#diagnosisSession").textContent = `会话 ${state.currentConversationId}`;
  $("#diagnosisSession").title = state.currentConversationId;
}

function persistDiagnosis() {
  // 仅保留本浏览器最近的会话视图；后端 SQLite 才是完整执行记录。
  try {
    const items = $$(".recent-item", $("#recentSection")).map(button => ({
      id: button.dataset.conversationId, title: button.querySelector("span:nth-child(2)").textContent,
      titled: button.dataset.titled, record: state.conversations.get(button.dataset.conversationId),
    }));
    localStorage.setItem("dcn.diagnosis.v1", JSON.stringify({ current: state.currentConversationId, items }));
  } catch { /* 浏览器存储不可用时仍可正常诊断。 */ }
}

function initializeDiagnosis() {
  $("#recentSection").querySelectorAll(".recent-item").forEach(item => item.remove());
  try {
    const saved = JSON.parse(localStorage.getItem("dcn.diagnosis.v1") || "null");
    for (const item of saved?.items || []) {
      if (!/^dcn-[a-z0-9-]+$/i.test(item.id)) continue;
      const button = document.createElement("button");
      button.className = "recent-item";
      button.dataset.conversationId = item.id;
      button.dataset.titled = item.titled;
      button.innerHTML = `<span class="recent-icon">◇</span><span>${escapeHtml(item.title)}</span><time>历史</time>`;
      $("#recentSection").append(button);
      state.conversations.set(item.id, item.record);
      bindRecentConversation(button);
    }
    const buttons = $$(".recent-item", $("#recentSection"));
    const selected = buttons.find(b => b.dataset.conversationId === saved?.current) || buttons[0];
    if (selected) { activateRecentConversation(selected); restoreConversation(state.currentConversationId); }
    else createRecentConversation();
  } catch { createRecentConversation(); }
  updateSessionLabel();
  $("#stopDiagnosis").addEventListener("click", async () => {
    $("#stopDiagnosis").disabled = true;
    const runId = diagnosisRunId;
    diagnosisAbort?.abort(); // 先停止界面读取，不等待取消接口返回。
    try {
      if (runId) await apiJson(`/v1/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
    } catch (error) { showToast("停止请求未确认", error.message); }
  });
  $("#fabricSelect").addEventListener("change", () => loadNetwork($("#fabricSelect").value, true));
  $("#refreshNetwork").addEventListener("click", () => loadNetwork($("#fabricSelect").value, true));
  $("#podSelect").addEventListener("change", () => {
    networkState().viewPod = $("#podSelect").value;
    drawNetwork(); saveCurrentConversationState();
  });
  // 委托事件使浏览器恢复的卡片也能继续展开全文。
  $("#messages").addEventListener("click", event => {
    const button = event.target.closest("[data-read-artifact]");
    if (button) readArtifactPage(button);
  });
  apiJson("/health").then(info => {
    $("#backendConnection").textContent = `后端已连接 · ${info.model}`;
  }).catch(() => { $("#backendConnection").textContent = "后端未连接"; });
  loadNetwork();
}

async function apiJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data));
  return data;
}

function networkState() {
  const record = ensureConversationRecord(state.currentConversationId);
  record.network ||= { fabrics: [], deviceInfo: [], snapshots: {}, selected: "", activeIds: [], faultIds: [], faultPorts: [] };
  return record.network;
}

async function loadNetwork(fabricId = "", refresh = false, isCurrent = () => true) {
  const session = state.currentConversationId;
  const request = ++networkRequest;
  const network = networkState();
  fabricId ||= network.selected;
  if (!network.selected) {
    $("#fabricSelect").replaceChildren();
    $("#networkFocus").textContent = "等待本会话设备查询";
  }
  if (!refresh && network.snapshots[fabricId]) {
    network.selected = fabricId; $("#refreshNetwork").disabled = false; drawNetwork(); return;
  }
  $("#networkStatus").textContent = "正在通过 MCP 查询拓扑…";
  $("#refreshNetwork").disabled = true;
  setDiagnosisRailExpanded(true);
  try {
    const data = await apiJson(`/v1/topology?fabric_id=${encodeURIComponent(fabricId)}`);
    if (session !== state.currentConversationId || request !== networkRequest || !isCurrent()) return;
    network.fabrics = data.fabrics;
    network.deviceInfo = data.deviceInfo;
    network.selected = data.fabricId;
    network.snapshots[data.fabricId] = data;
    drawNetwork();
    saveCurrentConversationState();
  } catch (error) {
    if (session === state.currentConversationId && request === networkRequest && isCurrent()) {
      $("#networkStatus").textContent = `拓扑不可用：${error.message}。可点击 ↻ 重试，诊断仍可使用。`;
    }
  } finally {
    if (request === networkRequest) $("#refreshNetwork").disabled = false;
  }
}

function drawNetwork() {
  const network = networkState();
  const data = network.snapshots[network.selected];
  if (!data) return;
  const select = $("#fabricSelect");
  select.innerHTML = network.fabrics.map(f => `<option value="${escapeHtml(f.fabricId)}">${escapeHtml(f.fabricName || f.fabricId)}</option>`).join("");
  select.value = network.selected;
  const inventory = new Map(network.deviceInfo.map(d => [String(d.neResId), d]));
  const devices = data.nodes.map(node => {
    const d = { ...inventory.get(String(node.neResId)), ...node };
    return { id: String(d.neResId), name: d.neName || String(d.neResId), ip: d.neIp || d.NeIp || "",
      device_type: d.neRole || d.neCategory || "设备", pod: d.podName || d.podId || "Pod 未提供",
      active: network.activeIds.includes(String(d.neResId)), has_problem: network.faultIds.includes(String(d.neResId)) };
  });
  const links = data.links.map(link => ({ ...link, source: String(link.srcNeResId), target: String(link.desNeResId),
    has_problem: (network.faultPorts || []).includes(`${link.srcNeResId}/${link.srcItfName}`) ||
      (network.faultPorts || []).includes(`${link.desNeResId}/${link.desItfName}`) }));
  const pods = [...new Set(devices.map(d => d.pod))];
  const scope = $("#podSelect");
  if (!pods.includes(network.viewPod) && !["auto", "all"].includes(network.viewPod)) network.viewPod = "auto";
  scope.innerHTML = '<option value="auto">跟随当前排查</option><option value="all">全网概览</option>' +
    pods.map(pod => `<option value="${escapeHtml(pod)}">${escapeHtml(pod)}</option>`).join("");
  scope.value = network.viewPod;
  const focusedPod = network.viewPod === "auto" ? network.focusPod : network.viewPod;
  const visible = pods.includes(focusedPod) ? devices.filter(d => d.pod === focusedPod) : devices;
  const visibleIds = new Set(visible.map(d => d.id));
  const topology = { native: true, devices: visible, links: links.filter(l => visibleIds.has(l.source) && visibleIds.has(l.target)), interface: network.interface };
  state.currentDiagnosisTopology = topology;
  state.currentDiagnosisTopologyPayload = { phase: "MCP 拓扑" };
  renderNetworkTopology(topology, $("#diagnosisTopologySvg"));
  setDiagnosisTopologyScale(state.diagnosisTopologyScale);
  $("#diagnosisTopologyEmpty").classList.toggle("hidden", devices.length > 0);
  $("#diagnosisTopologyContent").classList.toggle("hidden", devices.length === 0);
  $("#networkStatus").textContent = `${data.data_source === "DEMO" ? "DEMO · " : ""}${devices.length} 台设备 / ${links.length} 条链路${visible.length < devices.length ? ` · ${focusedPod}` : " · 全网"}`;
  $("#networkStatus").title = "跟随排查时显示当前 Pod；跨 Pod 链路可切换全网概览查看。";
  $("#diagnosisTopologyPhase").textContent = network.fabrics.find(f => f.fabricId === network.selected)?.fabricName || network.selected;
  const active = devices.filter(d => d.active);
  $("#networkFocus").textContent = active.length
    ? `${network.focusLabel || "当前排查"} · ${active.map(d => `${d.pod} / ${d.name}`).join("、")}${network.interface ? ` · ${network.interface}` : ""}`
    : (network.focusLabel || "等待排查步骤；蓝色表示正在查询，不代表故障");
}

function renderNetworkTopology(topology, svg) {
  // 按 Pod 分区、按设备角色分层，所有连接使用资源 ID，允许不同 Pod 中同名设备。
  svg.replaceChildren();
  const pods = [...new Set(topology.devices.map(d => d.pod))];
  // 同角色设备每行最多两台，主备 Leaf 并排，单 Pod 视图可同时看见两个异常。
  const rows = new Map(pods.map(pod => {
    const nodes = topology.devices.filter(d => d.pod === pod);
    const levels = [...new Set(nodes.map(d => layerForType(d.device_type)))].sort((a, b) => a - b);
    const lines = [];
    for (const level of levels) {
      const layer = nodes.filter(d => layerForType(d.device_type) === level);
      for (let i = 0; i < layer.length; i += 2) lines.push(layer.slice(i, i + 2));
    }
    return [pod, lines];
  }));
  const width = Math.max(320, pods.length * 280), height = Math.max(390,
    ...pods.map(p => 100 + rows.get(p).length * 150));
  svg.dataset.width = width; svg.dataset.height = height;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMin meet");
  const positions = new Map();
  pods.forEach((pod, index) => {
    const x = index * width / pods.length, span = width / pods.length;
    svg.append(svgElement("rect", { x: x + 12, y: 14, width: span - 24, height: height - 28, rx: 16, class: "pod-region" }));
    const title = svgElement("text", { x: x + 30, y: 44, class: "pod-label" }); title.textContent = pod; svg.append(title);
    rows.get(pod).forEach((row, i) => row.forEach((d, column) => positions.set(d.id,
      { x: x + span * (column + .5) / row.length, y: 100 + i * 150, pod })));
  });
  for (const [index, link] of topology.links.entries()) {
    const a = positions.get(link.source), b = positions.get(link.target);
    if (!a || !b) continue;
    const active = topology.devices.some(d => d.active && (
      (link.source === d.id && (!topology.interface || link.srcItfName === topology.interface)) ||
      (link.target === d.id && (!topology.interface || link.desItfName === topology.interface))));
    // 同 Pod 的连线绕开名称与 IP，避免文字压在线上。
    const lane = a.x + 53 + (index % 3) * 7;
    const path = a.x === b.x ? `M ${a.x + 32} ${a.y} H ${lane} V ${b.y} H ${b.x + 32}`
      : a.pod === b.pod ? `M ${a.x + Math.sign(b.x - a.x) * 32} ${a.y} H ${b.x} V ${b.y - 35}`
      : `M ${a.x} ${a.y - 35} V ${Math.min(a.y, b.y) - 52 + index % 4 * 3} H ${b.x} V ${b.y - 35}`;
    const line = svgElement("path", { d: path, fill: "none",
      class: `topology-link${active ? " inspecting" : ""}${link.has_problem ? " problem" : ""}` });
    const title = svgElement("title"); title.textContent = `${link.srcNeName} ${link.srcItfName} ↔ ${link.desNeName} ${link.desItfName}`;
    line.append(title); svg.append(line);
  }
  for (const d of topology.devices) {
    const p = positions.get(d.id);
    const group = svgElement("g", { transform: `translate(${p.x} ${p.y})`, class: `topology-node${d.active ? " inspecting" : ""}${d.has_problem ? " problem" : ""}` });
    appendDeviceIcon(group);
    const title = svgElement("title"); title.textContent = `${d.pod} / ${d.name}\n${d.id}\n${d.ip}`; group.append(title);
    [[d.name, 53, "node-label"], [`${typeLabel(d.device_type)} · ${d.ip}`, 71, "node-type"],
      [d.active ? "● 正在排查" : d.has_problem ? "● 异常证据" : "", 88, "node-state"]].forEach(([text, y, cls]) => {
      const label = svgElement("text", { x: 0, y, class: cls }); label.textContent = text; group.append(label);
    });
    svg.append(group);
  }
}

function mcpPayload(result) {
  // MCP 包装的 structuredContent 优先，兼容字符串 JSON 和 SDK 的 result 字段。
  let value = result?.data ?? parsePossibleJson(result?.text) ?? result;
  if (value && typeof value === "object" && Object.keys(value).length === 1 && "result" in value) value = parsePossibleJson(value.result);
  return value;
}

async function locateTool(call, result = null, isCurrent = () => true) {
  const network = networkState();
  const args = call.arguments?.arguments || call.arguments || {};
  const value = result ? mcpPayload(result.result) : null;
  const payload = result ? { ...(value && typeof value === "object" ? value : {}), ...result.location } : null;
  const values = new Set();
  function collect(value) {
    if (typeof value === "string" || typeof value === "number") values.add(String(value).toLowerCase());
    else if (value && typeof value === "object") Object.values(value).forEach(collect);
  }
  collect(args);
  // 只从单设备业务返回中取定位信息，不把拓扑清单的所有设备都当作排查目标。
  if (payload && !payload.nodes && !payload.deviceInfo && !payload.fabrics) {
    for (const key of ["device", "neResId", "neName", "neIp", "NeIp"]) collect(payload[key]);
  }
  const candidates = new Map(network.deviceInfo.map(d => [String(d.neResId), d]));
  Object.values(network.snapshots).forEach(s => s.nodes.forEach(d => candidates.set(String(d.neResId),
    { ...candidates.get(String(d.neResId)), ...d })));
  const matches = [...candidates.values()].filter(d => [d.neResId, d.neName, d.neIp, d.NeIp].some(v => v && values.has(String(v).toLowerCase())));
  if (call.name !== "mcp.call") return; // 技能和原文检索不改变设备定位。
  network.activeIds = matches.map(d => String(d.neResId));
  if (matches.length) network.focusPod = matches[0].podName || matches[0].podId || "Pod 未提供";
  network.interface = args.interface || args.itfName || payload?.interface || "";
  network.focusLabel = `步骤 ${call.step || "—"} · ${call.arguments?.name || call.name}`;
  if (!matches.length) network.focusLabel += " · 全网查询或未匹配到设备";
  const ports = network.activeIds.map(id => `${id}/${network.interface}`);
  network.faultPorts ||= [];
  const abnormal = payload?.oper_status === "down" || ["down", "degraded"].includes(payload?.health_status);
  if (abnormal) network.faultPorts = [...new Set([...network.faultPorts, ...ports])];
  if (payload?.oper_status === "up" && !abnormal) network.faultPorts = network.faultPorts.filter(port => !ports.includes(port));
  // 一个接口恢复不能清除同设备上其他接口的异常。
  if (["up", "down"].includes(payload?.oper_status)) {
    network.faultIds = [...new Set(network.faultPorts.map(port => port.slice(0, port.indexOf("/"))))];
  }
  let fabricId = args.fabricId || matches.find(d => d.fabricId)?.fabricId;
  // 原始设备字段没有 fabricId 时，用拓扑成员关系查归属，不猜测 Fabric。
  const containsTarget = snapshot => snapshot.nodes.some(d => network.activeIds.includes(String(d.neResId)));
  if (!fabricId && matches.length) {
    fabricId = Object.keys(network.snapshots).find(id => containsTarget(network.snapshots[id]));
    for (const fabric of network.fabrics) {
      if (fabricId) break;
      if (network.snapshots[fabric.fabricId]) continue;
      try {
        const snapshot = await apiJson(`/v1/topology?fabric_id=${encodeURIComponent(fabric.fabricId)}`);
        if (!isCurrent()) return;
        network.snapshots[fabric.fabricId] = snapshot;
        if (containsTarget(snapshot)) fabricId = fabric.fabricId;
      } catch { /* 一个 Fabric 查询失败不终止诊断流。 */ }
    }
  }
  if (!isCurrent()) return;
  if (fabricId && fabricId !== network.selected) await loadNetwork(fabricId, false, isCurrent);
  else drawNetwork();
}

function eventNote(run, title, text, kind = "") {
  const card = document.createElement("section"); card.className = `agent-phase-message ${kind}`;
  const phase = { "执行计划": "plan", "当前步骤": "reasoning", "证据评估": "reflection", "需要补充信息": "question",
    "执行错误": "error", "连接或请求失败": "error", "连接中断": "error" }[title] || "notice";
  card.dataset.phase = phase;
  const symbol = { plan: "≡", reasoning: "◇", reflection: "✓", question: "?", error: "!", notice: "·" }[phase];
  card.innerHTML = `<div class="agent-phase-indicator"><span class="phase-symbol" aria-hidden="true">${symbol}</span><strong>${escapeHtml(title)}</strong></div><div class="agent-phase-content"></div>`;
  $(".agent-phase-content", card).textContent = text;
  $(".steps", run).append(card);
}

function toolCard(run, part, calls) {
  // 工具分段：running 建卡，completed/error 时补结果与原文入口。
  const toolState = part.state || {};
  const callId = part.callID;
  if (toolState.status === "pending" || toolState.status === "running") {
    if (calls.has(callId)) return;
    const card = document.createElement("article"); card.className = "step-card native-tool";
    card.dataset.callId = callId;
    card.dataset.status = "running";
    const category = part.tool.startsWith("mcp.") ? "MCP" : part.tool.startsWith("skill.") ? "SKILL" : "检索";
    card.innerHTML = `<header class="step-header"><span class="tool-category">${category}</span><strong>${escapeHtml(toolState.title || part.tool)}</strong><span class="step-status">执行中</span></header><details><summary>调用参数 <span>${escapeHtml(part.tool)}</span></summary></details><details class="native-result"><summary>返回结果 <span>等待返回</span></summary></details>`;
    $("details", card).append(renderRawContent(toolState.input));
    $(".steps", run).append(card);
    calls.set(callId, { call_id: callId.slice(5), name: part.tool, arguments: toolState.input || {},
      step: toolState.metadata?.step, card });
    return;
  }
  const call = calls.get(callId); if (!call) return;
  let data = {};
  try { data = JSON.parse(toolState.output || "{}"); } catch { /* 输出不是 JSON 时仅展示原文。 */ }
  const ok = toolState.status === "completed";
  call.card.dataset.status = ok ? "completed" : "error";
  $(".step-status", call.card).textContent = ok ? "已返回" : "调用失败";
  call.card.classList.toggle("tool-failed", !ok);
  const details = $(".native-result", call.card);
  $("summary", details).textContent = data.truncated ? `结果摘要 · 原文 ${data.chars} 字符` : "返回结果 · 点击展开";
  const content = call.name === "mcp.call" && ok ? mcpPayload(data.result) : data.result;
  details.append(renderRawContent(data.truncated ? { preview: data.preview, status: data.status, exit_code: data.exit_code } : content));
  if (data.artifact_id) {
    const session = state.currentConversationId;
    const area = document.createElement("div"); area.className = "artifact-controls";
    area.innerHTML = `<small>结果 ID：${escapeHtml(data.artifact_id)}</small><button type="button" data-read-artifact="${escapeHtml(data.artifact_id)}" data-session="${escapeHtml(session)}" data-offset="0">读取原文片段</button><a href="/v1/sessions/${encodeURIComponent(session)}/artifacts/${encodeURIComponent(data.artifact_id)}/raw" target="_blank" rel="noopener">完整 JSON</a><pre class="artifact-page hidden"></pre>`;
    details.append(area);
  }
  return data;
}

async function readArtifactPage(button) {
  button.disabled = true;
  try {
    const page = await apiJson(`/v1/sessions/${encodeURIComponent(button.dataset.session)}/artifacts/${encodeURIComponent(button.dataset.readArtifact)}?offset=${button.dataset.offset}&limit=2000`);
    const output = $(".artifact-page", button.parentElement); output.classList.remove("hidden");
    output.textContent = `[字符 ${page.offset}..${page.next_offset}]\n${page.text}`;
    button.dataset.offset = page.eof ? "0" : String(page.next_offset);
    button.textContent = page.eof ? "已到末尾 · 从头读取" : "读取下一段";
  } catch (error) { showToast("读取失败", error.message); }
  finally { button.disabled = false; }
}

async function sendBackendDiagnosis(question) {
  if (state.diagnosing || !question.trim()) return;
  state.diagnosing = true; diagnosisRunId = null; diagnosisAbort = new AbortController();
  $("#sendButton").disabled = true; $("#stopDiagnosis").disabled = false; $("#stopDiagnosis").classList.remove("hidden");
  $("#introCard").classList.add("hidden");
  appendUserMessage(question.trim()); nameCurrentConversation(question.trim());
  const run = createAgentRun(), calls = new Map();
  const session = state.currentConversationId;
  let networkStep = 0, finished = false;
  const network = networkState();
  network.activeIds = []; network.interface = ""; network.focusLabel = "等待本轮设备查询";
  drawNetwork();
  let answer = "", answerElement = null, terminal = "", lastSeq = 0;
  $("#questionInput").value = ""; resizeComposer(); scrollChatToBottom(true);
  saveCurrentConversationState();
  const messages = new Map(); // messageID 到 role 的映射，用于区分用户消息的 text 分段。
  function renderAnswer() {
    if (!answerElement) {
      const article = document.createElement("article"); article.className = "agent-final-response";
      article.innerHTML = '<div class="answer-heading"><span aria-hidden="true">✦</span> 回答</div><div class="answer-body markdown"></div>';
      $(".steps", run).append(article); answerElement = $(".answer-body", article);
    }
    answerElement.innerHTML = markdownToHtml(answer);
  }
  async function accept(event, seq = 0) {
    const props = event.properties || {};
    if (props.sessionID !== "ses_" + state.currentConversationId) return;
    if (seq && seq <= lastSeq) return;
    if (seq) lastSeq = seq;
    const follow = isNearChatBottom();
    $(".typing-card", run)?.remove();
    if (event.type === "message.updated") {
      if (props.info?.id) messages.set(props.info.id, props.info.role);
    } else if (event.type === "message.part.updated") {
      const part = props.part || {};
      if (part.type === "step-start") {
        const meta = part.metadata || {};
        $(".run-label span", run).textContent = meta.phase === "answer" ? "正在生成结论…" : `正在分析 · 步骤 ${meta.step || "—"}`;
      } else if (part.type === "reasoning") {
        const kind = part.metadata?.kind;
        if (kind === "plan") eventNote(run, "执行计划", (part.text || "").split("\n").map((s, i) => `${i + 1}. ${s.replace(/^\d+\.\s*/, "")}`).join("\n"));
        else if (kind === "reflection") eventNote(run, "证据评估", part.text);
        else if (part.text) eventNote(run, "当前步骤", part.text);
      } else if (part.type === "tool") {
        const data = toolCard(run, part, calls);
        const call = calls.get(part.callID);
        if (call?.name === "mcp.call" && part.state?.status !== "pending") {
          const step = ++networkStep;
          const isCurrent = () => !finished && step === networkStep && session === state.currentConversationId;
          // 拓扑是辅助展示，慢请求不能阻塞后续文本、停止或结束事件。
          locateTool(call, part.state.status === "running" ? null : data, isCurrent).then(() => {
            const payload = mcpPayload(data?.result);
            if (isCurrent() && part.state.status !== "running" && (payload?.nodes || payload?.deviceInfo || payload?.fabrics)) {
              return loadNetwork(call.arguments?.arguments?.fabricId || network.selected, true, isCurrent);
            }
          }).catch(error => { if (isCurrent()) $("#networkStatus").textContent = `拓扑定位失败：${error.message}`; });
        }
      } else if (part.type === "text" && messages.get(part.messageID) !== "user") {
        answer = part.text || answer;
        renderAnswer();
      }
    } else if (event.type === "message.part.delta") {
      answer += props.delta || "";
      renderAnswer();
    } else if (event.type === "session.idle") {
      terminal = props.status || "completed";
    } else if (event.type === "clarification") eventNote(run, "需要补充信息", props.question);
    else if (event.type === "error") { terminal = props.code || "error"; eventNote(run, "执行错误", props.message, "tool-failed"); }
    else if (event.type === "context.compressed") eventNote(run, "上下文已压缩", `估算量 ${props.before_tokens_estimate} → ${props.after_tokens_estimate}${props.fallback ? "，摘要生成失败，已使用历史截取" : ""}`);
    else if (event.type === "limit") eventNote(run, "执行限制", props.reason);
    else if (event.type === "model.retry") eventNote(run, "重新生成决策", props.reason);
    else if (event.type === "skill.loaded") eventNote(run, "已加载技能", props.name);
    if (follow) $("#chatScroll").scrollTop = $("#chatScroll").scrollHeight;
  }
  try {
    const response = await fetch("/v1/chat/stream", { method: "POST", signal: diagnosisAbort.signal,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: question.trim(), session_id: state.currentConversationId }) });
    if (!response.ok || !response.body) {
      const error = await response.json(); throw new Error(typeof error.detail === "string" ? error.detail : JSON.stringify(error.detail || error));
    }
    diagnosisRunId = response.headers.get("X-Run-ID");
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    // TCP 分块可能截断中文或 SSE 分隔符，持续解码并同时兼容 LF / CRLF。
    try {
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        let boundary;
        while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
          const block = buffer.slice(0, boundary.index); buffer = buffer.slice(boundary.index + boundary[0].length);
          const parsed = parseSseBlock(block.replaceAll("\r", ""));
          if (parsed) await accept(parsed.data, parsed.id);
        }
        if (done) break;
      }
    } finally { reader.releaseLock(); }
    if (!terminal) { terminal = "interrupted"; eventNote(run, "连接中断", "未收到结束事件，不能判定诊断成功。可检查本轮运行状态后继续追问。"); }
  } catch (error) {
    terminal = error.name === "AbortError" ? "cancelled" : "error";
    if (terminal === "error") eventNote(run, "连接或请求失败", error.message, "tool-failed");
  } finally {
    finished = true;
    ++networkRequest; // 忽略本轮尚未返回的辅助拓扑查询。
    network.activeIds = []; network.interface = "";
    network.focusLabel = `本轮${terminalLabels[terminal] || terminal}；异常标记保留最近查询证据，不代表实时状态`;
    $("#refreshNetwork").disabled = false;
    drawNetwork();
    $(".run-label span", run).textContent = `DCN Copilot · ${terminalLabels[terminal] || terminal}`;
    run.dataset.status = terminal;
    for (const call of calls.values()) if ($(".step-status", call.card).textContent === "执行中") {
      $(".step-status", call.card).textContent = "未收到返回";
      call.card.dataset.status = "interrupted";
    }
    finishAgentRun(run); $(".typing-card", run)?.remove();
    state.diagnosing = false; diagnosisAbort = null; diagnosisRunId = null;
    $("#sendButton").disabled = false; $("#stopDiagnosis").classList.add("hidden");
    saveCurrentConversationState();
  }
}

initializeApp();
