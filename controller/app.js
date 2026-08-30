/* 述播 ScriptCue 主控端（原生 JS，无框架、无构建步骤）
 * 协议定义见 docs/protocol.md
 */
"use strict";

const PROTO = 1;
const HOLD_MS = 1000;            // 长按确认时长（C-10 防误触）
const RECEIPT_TIMEOUT_MS = 8000; // 回执等待窗口
const DELTA_WARN_MS = 50;        // 偏差告警阈值（PRD 验收标准）
const RECONNECT_MAX_S = 10;      // 重连退避上限

// ---------------------------------------------------------------------------
// 状态
// ---------------------------------------------------------------------------
const state = {
  ws: null,
  token: null,
  roomCode: null,
  roomName: null,
  serverOffset: 0,          // 服务器时间 - 本地时间（粗估，仅用于倒计时显示）
  agents: new Map(),        // session_id -> AgentState
  activeCmd: null,          // {command_id, command, at, receipts: Map}
  lastReceipts: null,       // 最近一次已结束指令的回执汇总
  reconnectAttempt: 0,
  manualLeave: false,
  countdownRaf: 0,
};

const $ = (id) => document.getElementById(id);

function serverNow() { return Date.now() + state.serverOffset; }

function wsUrl() {
  const scheme = location.protocol === "https:" ? "wss://" : "ws://";
  return scheme + location.host + "/ws";
}

// ---------------------------------------------------------------------------
// 连接与消息
// ---------------------------------------------------------------------------

function send(msg) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(msg));
    return true;
  }
  return false;
}

function connect(firstMsg) {
  setConnPill("connecting");
  const ws = new WebSocket(wsUrl());
  state.ws = ws;

  ws.onopen = () => {
    state.reconnectAttempt = 0;
    ws.send(JSON.stringify(firstMsg));
  };

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    dispatch(msg);
  };

  ws.onclose = () => {
    setConnPill("offline");
    if (state.manualLeave) return;
    if (!state.token) return; // 尚未进入房间，无需自动重连
    const delay = Math.min(2 ** state.reconnectAttempt, RECONNECT_MAX_S);
    state.reconnectAttempt += 1;
    showToast(`连接断开，${delay} 秒后自动重连…`);
    setTimeout(() => {
      if (state.manualLeave) return;
      connect({ type: "controller.resume", proto: PROTO,
                room_code: state.roomCode, token: state.token });
    }, delay * 1000);
  };

  ws.onerror = () => { /* onclose 会接管后续处理 */ };
}

function dispatch(msg) {
  switch (msg.type) {
    case "controller.joined":
      state.token = msg.token;
      state.roomCode = msg.room_code;
      state.roomName = msg.room_name;
      state.serverOffset = (msg.server_time || Date.now()) - Date.now();
      state.agents = new Map((msg.agents || []).map(a => [a.session_id, a]));
      state.manualLeave = false;
      setConnPill("online");
      showRoomView();
      renderAgents();
      break;

    case "agent.updated":
      state.agents.set(msg.agent.session_id, msg.agent);
      renderAgents();
      break;

    case "command.scheduled":
      state.activeCmd = {
        command_id: msg.command_id, command: msg.command, at: msg.at,
        receipts: new Map(),
      };
      state.lastReceipts = null;
      showCountdownBanner();
      startCountdown();
      scheduleReceiptTimeout(msg.at);
      break;

    case "command.cancelled":
      if (state.activeCmd && state.activeCmd.command_id === msg.command_id) {
        state.activeCmd = null;
        hideCountdownBanner();
        stopCountdown();
        showToast("指令已取消");
      }
      break;

    case "command.receipt":
      if (state.activeCmd && msg.command_id === state.activeCmd.command_id) {
        state.activeCmd.receipts.set(msg.session_id, msg);
        renderReceipts();
        renderAgents();
      }
      break;

    case "error":
      handleServerError(msg);
      break;
  }
}

function handleServerError(msg) {
  if (msg.code === "room_not_found" && state.token) {
    // 服务器重启后会话失效，回到首页
    showToast("服务器会话已失效，请重新创建或加入房间");
    leaveRoom(true);
    return;
  }
  showHomeError(msg.message || msg.code);
}

// ---------------------------------------------------------------------------
// 视图切换
// ---------------------------------------------------------------------------

function showRoomView() {
  $("home-view").hidden = true;
  $("room-view").hidden = false;
  $("room-name").textContent = state.roomName || "";
  $("room-code").textContent = state.roomCode;
}

function leaveRoom(silent) {
  state.manualLeave = true;
  if (state.ws) { state.ws.onclose = null; state.ws.close(); state.ws = null; }
  state.token = null;
  state.activeCmd = null;
  stopCountdown();
  hideCountdownBanner();
  $("room-view").hidden = true;
  $("home-view").hidden = false;
  setConnPill("offline");
  if (!silent) showHomeError("");
}

function setConnPill(mode) {
  const pill = $("conn-pill");
  pill.className = "pill " + (mode === "online" ? "online" : "offline");
  pill.textContent = { online: "已连接", offline: "未连接", connecting: "连接中…" }[mode] || mode;
}

function showHomeError(text) {
  const el = $("home-error");
  el.hidden = !text;
  el.textContent = text;
}

let toastTimer = 0;
function showToast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

// ---------------------------------------------------------------------------
// 设备列表渲染
// ---------------------------------------------------------------------------

const QUALITY_TEXT = { excellent: "优", good: "良", poor: "差", none: "未同步" };
const CMD_TEXT = { play: "起播", pause: "暂停", test: "测试" };

function renderAgents() {
  const list = $("agent-list");
  list.innerHTML = "";
  const agents = [...state.agents.values()];
  const readyCount = agents.filter(a => a.online && a.ready).length;
  $("ready-count").textContent = agents.length
    ? `${readyCount}/${agents.filter(a => a.online).length} 在线就绪` : "";

  if (!agents.length) {
    list.innerHTML = '<p class="muted">暂无设备，等待被控端加入…</p>';
    return;
  }

  for (const agent of agents) {
    list.appendChild(renderAgentCard(agent));
  }
}

function renderAgentCard(agent) {
  const card = document.createElement("div");
  const q = agent.clock_quality || "none";
  card.className = "agent " + (agent.online
    ? (q === "poor" || q === "none" ? "bad-clock" : "online")
    : "offline");

  // 顶部：昵称 + 状态徽章
  const top = document.createElement("div");
  top.className = "agent-top";
  const nick = document.createElement("span");
  nick.className = "agent-nick";
  nick.textContent = agent.nickname;
  top.appendChild(nick);

  if (!agent.online) {
    top.appendChild(badge("离线", "state-offline"));
  } else if (agent.ready) {
    top.appendChild(badge("已就绪", "ready"));
  } else {
    top.appendChild(badge("未就绪", "not-ready"));
  }
  top.appendChild(badge("时钟" + (QUALITY_TEXT[q] || q), "q-" + q));
  card.appendChild(top);

  // 元信息：偏移 / RTT / 最近偏差
  const meta = document.createElement("div");
  meta.className = "agent-meta";
  const parts = [];
  if (agent.clock_offset_ms != null) {
    parts.push(`偏移 ${fmtMs(agent.clock_offset_ms)}`);
  }
  if (agent.clock_rtt_ms != null) {
    parts.push(`RTT ${fmtMs(agent.clock_rtt_ms)}`);
  }
  const receipt = state.activeCmd && state.activeCmd.receipts.get(agent.session_id);
  if (receipt) {
    const span = document.createElement("span");
    span.className = "delta " + (Math.abs(receipt.delta_ms) <= DELTA_WARN_MS ? "ok" : "bad");
    span.textContent = ` 最近触发偏差 ${fmtSigned(receipt.delta_ms)}`;
    parts.push("");
    meta.textContent = parts.filter(Boolean).join(" · ");
    meta.appendChild(span);
  } else {
    meta.textContent = parts.join(" · ");
  }
  card.appendChild(meta);

  // 操作行：单设备测试 + 补偿值编辑
  const actions = document.createElement("div");
  actions.className = "agent-actions";

  const testBtn = document.createElement("button");
  testBtn.className = "mini";
  testBtn.textContent = "测试触发";
  testBtn.disabled = !agent.online;
  testBtn.onclick = () => sendCommand("test", agent.session_id);
  actions.appendChild(testBtn);

  const compEditor = document.createElement("span");
  compEditor.className = "comp-editor";
  compEditor.append("补偿 ");
  const compInput = document.createElement("input");
  compInput.type = "number";
  compInput.min = "-10000"; compInput.max = "10000"; compInput.step = "10";
  compInput.value = agent.compensation_ms ?? 0;
  compEditor.appendChild(compInput);
  compEditor.append(" ms ");
  const compBtn = document.createElement("button");
  compBtn.className = "mini";
  compBtn.textContent = "设置";
  compBtn.onclick = () => {
    const val = parseInt(compInput.value, 10);
    if (Number.isNaN(val)) return;
    send({ type: "controller.set_comp", session_id: agent.session_id,
           compensation_ms: val });
  };
  compEditor.appendChild(compBtn);
  actions.appendChild(compEditor);

  card.appendChild(actions);
  return card;
}

function badge(text, cls) {
  const el = document.createElement("span");
  el.className = "badge " + cls;
  el.textContent = text;
  return el;
}

// ---------------------------------------------------------------------------
// 指令下发与倒计时
// ---------------------------------------------------------------------------

function currentLeadMs() {
  const val = parseInt($("lead-ms").value, 10);
  return Number.isNaN(val) ? 3000 : Math.max(1000, Math.min(30000, val));
}

function sendCommand(command, target) {
  if (state.activeCmd && serverNow() < state.activeCmd.at) {
    showToast("上一条指令仍在倒计时，请先取消");
    return;
  }
  const msg = {
    type: "controller.command",
    command_id: crypto.randomUUID ? crypto.randomUUID()
      : "cmd-" + Date.now() + "-" + Math.random().toString(16).slice(2),
    command,
    lead_ms: currentLeadMs(),
  };
  if (target) msg.target = target;
  if (!send(msg)) showToast("未连接服务器");
}

function showCountdownBanner() {
  $("countdown-banner").hidden = false;
}

function hideCountdownBanner() {
  $("countdown-banner").hidden = true;
}

function startCountdown() {
  stopCountdown();
  const tick = () => {
    const cmd = state.activeCmd;
    if (!cmd) { stopCountdown(); hideCountdownBanner(); return; }
    const remain = cmd.at - serverNow();
    const label = CMD_TEXT[cmd.command] || cmd.command;
    if (remain > 0) {
      $("countdown-text").textContent =
        `${label}指令 · ${(remain / 1000).toFixed(1)} 秒后执行`;
      state.countdownRaf = requestAnimationFrame(tick);
    } else {
      $("countdown-text").textContent = `${label}指令已到期，等待回执…`;
      state.countdownRaf = requestAnimationFrame(tick);
    }
  };
  tick();
}

function stopCountdown() {
  if (state.countdownRaf) cancelAnimationFrame(state.countdownRaf);
  state.countdownRaf = 0;
}

function scheduleReceiptTimeout(at) {
  const waitMs = at - serverOffset - Date.now() + RECEIPT_TIMEOUT_MS;
  setTimeout(finalizeReceipts, Math.max(0, waitMs));
}

function finalizeReceipts() {
  const cmd = state.activeCmd;
  if (!cmd) return;
  // 汇总：未回执的在线设备标记为"未回执"
  state.lastReceipts = {
    command: cmd.command,
    rows: [...state.agents.values()].map(agent => {
      const r = cmd.receipts.get(agent.session_id);
      return {
        nickname: agent.nickname,
        delta_ms: r ? r.delta_ms : null,
        status: r ? r.status : (agent.online ? "missing" : "offline"),
      };
    }),
  };
  state.activeCmd = null;
  stopCountdown();
  hideCountdownBanner();
  renderReceipts();
  renderAgents();
}

function renderReceipts() {
  const box = $("receipt-list");
  const cmd = state.activeCmd;
  const data = cmd
    ? { command: cmd.command,
        rows: [...state.agents.values()].map(agent => {
          const r = cmd.receipts.get(agent.session_id);
          return { nickname: agent.nickname, delta_ms: r ? r.delta_ms : null,
                   status: r ? r.status : "waiting" };
        }) }
    : state.lastReceipts;

  if (!data) {
    box.innerHTML = '<p class="muted">尚无指令回执</p>';
    return;
  }

  $("receipt-title").textContent =
    `最近指令回执 · ${CMD_TEXT[data.command] || data.command}`;
  const table = document.createElement("table");
  table.innerHTML = "<tr><th>设备</th><th style='text-align:right'>触发偏差</th><th>状态</th></tr>";
  for (const row of data.rows) {
    const tr = document.createElement("tr");
    const tdNick = document.createElement("td");
    tdNick.textContent = row.nickname;
    const tdDelta = document.createElement("td");
    tdDelta.className = "num";
    const tdStatus = document.createElement("td");
    if (row.delta_ms != null) {
      tdDelta.textContent = fmtSigned(row.delta_ms);
      tdDelta.style.color = Math.abs(row.delta_ms) <= DELTA_WARN_MS
        ? "var(--ok)" : "var(--bad)";
      tdDelta.style.fontWeight = "700";
    } else {
      tdDelta.textContent = "—";
    }
    const statusText = { ok: "正常", error: "异常", skipped: "跳过",
                         waiting: "等待中", missing: "未回执", offline: "离线" };
    tdStatus.textContent = statusText[row.status] || row.status;
    if (row.status === "missing" || row.status === "offline" || row.status === "error") {
      tdStatus.style.color = "var(--bad)";
      tdStatus.style.fontWeight = "700";
    }
    tr.append(tdNick, tdDelta, tdStatus);
    table.appendChild(tr);
  }
  box.innerHTML = "";
  box.appendChild(table);
}

// ---------------------------------------------------------------------------
// 长按确认（C-10 防误触）
// ---------------------------------------------------------------------------

function attachHold(button, action) {
  let timer = 0;
  const start = (e) => {
    e.preventDefault();
    button.classList.add("holding");
    timer = setTimeout(() => {
      button.classList.remove("holding");
      action();
    }, HOLD_MS);
  };
  const abort = () => {
    clearTimeout(timer);
    button.classList.remove("holding");
  };
  button.addEventListener("pointerdown", start);
  for (const ev of ["pointerup", "pointerleave", "pointercancel"]) {
    button.addEventListener(ev, abort);
  }
  // 防止移动端双击缩放与长按弹出菜单
  button.addEventListener("contextmenu", (e) => e.preventDefault());
}

// ---------------------------------------------------------------------------
// 工具函数与事件绑定
// ---------------------------------------------------------------------------

function fmtMs(v) {
  return v == null ? "—" : `${Math.round(v)}ms`;
}

function fmtSigned(v) {
  return v == null ? "—" : `${v > 0 ? "+" : ""}${Math.round(v)}ms`;
}

function bindEvents() {
  $("btn-create").onclick = () => {
    showHomeError("");
    connect({ type: "controller.create", proto: PROTO,
              room_name: $("create-name").value.trim() || "未命名房间",
              password: $("create-password").value || undefined });
  };

  $("btn-join").onclick = () => {
    const code = $("join-code").value.trim().toUpperCase();
    if (!/^[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{6}$/.test(code)) {
      showHomeError("请输入 6 位房间码");
      return;
    }
    showHomeError("");
    connect({ type: "controller.join", proto: PROTO, room_code: code,
              password: $("join-password").value || undefined });
  };

  $("btn-leave").onclick = () => leaveRoom(false);

  $("room-code").onclick = async () => {
    try {
      await navigator.clipboard.writeText(state.roomCode);
      showToast("房间码已复制");
    } catch {
      showToast("复制失败，请手动抄录: " + state.roomCode);
    }
  };

  $("btn-cancel-cmd").onclick = () => {
    if (state.activeCmd) {
      send({ type: "controller.cancel", command_id: state.activeCmd.command_id });
    }
  };

  attachHold($("btn-play"), () => sendCommand("play"));
  attachHold($("btn-test-all"), () => sendCommand("test"));
  $("btn-pause").onclick = () => sendCommand("pause");
  $("btn-resume").onclick = () => sendCommand("play");
}

bindEvents();
