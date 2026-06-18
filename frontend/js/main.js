/**
 * main.js — 入口：WebSocket、聊天、3D 预览、Agent 推理、LLM 设置、模型历史
 *
 * viewer3d.js 动态加载（本地 Three.js，不阻断主模块链）
 * chat.js / agent_trace.js 同步 import（纯本地，无外部依赖）
 */

import { ChatPanel }  from './chat.js';
import { AgentTrace } from './agent_trace.js';

// ── Session ID ───────────────────────────────────────────────────────────────
let SESSION_ID = sessionStorage.getItem('cad_session_id') || crypto.randomUUID();
sessionStorage.setItem('cad_session_id', SESSION_ID);

// ── DOM refs ──────────────────────────────────────────────────────────────────
const viewerWrap      = document.getElementById('viewer-canvas-wrap');
const chatMsgsEl      = document.getElementById('chat-messages');
const traceLogEl      = document.getElementById('trace-log');
const traceDotEl      = document.getElementById('trace-dot');
const chatInput       = document.getElementById('chat-input');
const sendBtn         = document.getElementById('send-btn');
const newSessionBtn   = document.getElementById('btn-new-session');
const settingsBtn     = document.getElementById('btn-settings');
const modalOverlay    = document.getElementById('modal-settings');
const modalClose      = document.getElementById('modal-close');
const modalCancelBtn  = document.getElementById('btn-modal-cancel');
const saveConfigBtn   = document.getElementById('btn-save-config');
const resetViewBtn    = document.getElementById('btn-reset-view');
const traceClear      = document.getElementById('trace-clear');
const llmBadge        = document.getElementById('llm-badge');
const llmBadgeText    = document.getElementById('llm-badge-text');
const downloadStlBtn  = document.getElementById('btn-download-stl');
const downloadStepBtn = document.getElementById('btn-download-step');
const historyToggle   = document.getElementById('btn-history-toggle');
const historyPanel    = document.getElementById('history-panel');
const historyClose    = document.getElementById('history-close');
const historyList     = document.getElementById('history-list');

// ── Instances ─────────────────────────────────────────────────────────────────
const chat  = new ChatPanel(chatMsgsEl);
const trace = new AgentTrace(traceLogEl, traceDotEl);
let   viewer = null;

async function initViewer() {
  try {
    const { Viewer3D } = await import('./viewer3d.js?v=2');
    viewer = new Viewer3D(viewerWrap);
    trace.addInfo('3D 渲染器就绪');
  } catch (e) {
    console.warn('[viewer] 初始化失败:', e.message);
    const ph = document.getElementById('viewer-placeholder');
    if (ph) ph.innerHTML = '<div class="icon">⚠</div><div>3D 预览加载失败</div>';
  }
}
initViewer();

// ── Model history (P7) ───────────────────────────────────────────────────────
let _currentModelUrl = null;        // URL of the latest model for download
let _modelHistory    = [];          // [{filename, url, timestamp}, ...]

function addToHistory(entry) {
  _modelHistory.push(entry);
  _currentModelUrl = entry.url;

  // Enable download buttons
  downloadStlBtn.disabled = false;
  downloadStlBtn.title = `下载 ${entry.filename}`;
  downloadStepBtn.disabled = false;

  // Re-render history list
  renderHistory();
}

function renderHistory() {
  if (_modelHistory.length === 0) {
    historyList.innerHTML = '<div class="history-empty">暂无历史记录</div>';
    return;
  }
  historyList.innerHTML = '';
  // Show newest first
  [..._modelHistory].reverse().forEach((item, i) => {
    const isLatest = (i === 0);
    const div = document.createElement('div');
    div.className = `history-item${isLatest ? ' active' : ''}`;
    div.innerHTML = `
      <div class="history-item-name">${item.filename}</div>
      <div class="history-item-time">${item.timestamp}</div>
      <a class="history-item-dl" href="${item.url}" download="${item.filename}">⬇ 下载</a>
    `;
    // Click to load this version in viewer
    div.addEventListener('click', (e) => {
      if (e.target.tagName === 'A') return;   // let download link work
      if (viewer) viewer.loadSTL(item.url);
      _currentModelUrl = item.url;
      // Highlight active
      historyList.querySelectorAll('.history-item').forEach(el => el.classList.remove('active'));
      div.classList.add('active');
    });
    historyList.appendChild(div);
  });
}

// ── History panel toggle ───────────────────────────────────────────────────
historyToggle.addEventListener('click', () => {
  historyPanel.classList.toggle('hidden');
  historyToggle.classList.toggle('active');
});
historyClose.addEventListener('click', () => {
  historyPanel.classList.add('hidden');
  historyToggle.classList.remove('active');
});

// ── Download STL ──────────────────────────────────────────────────────────────
downloadStlBtn.addEventListener('click', () => {
  if (!_currentModelUrl) return;
  const a = document.createElement('a');
  a.href = _currentModelUrl;
  a.download = _currentModelUrl.split('/').pop();
  a.click();
});

// ── Download STEP ─────────────────────────────────────────────────────────────
downloadStepBtn.addEventListener('click', async () => {
  if (downloadStepBtn.disabled) return;
  const orig = downloadStepBtn.textContent;
  downloadStepBtn.textContent = '导出中…';
  downloadStepBtn.disabled = true;
  try {
    const res = await fetch(`/api/sessions/${SESSION_ID}/export/step`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      chat.addSystem(`STEP 导出失败: ${err.error || res.statusText}`);
      return;
    }
    const blob = await res.blob();
    const disp = res.headers.get('Content-Disposition') || '';
    const match = disp.match(/filename="([^"]+)"/);
    const fname = match ? match[1] : `export_${SESSION_ID.slice(0, 8)}.step`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fname;
    a.click();
    URL.revokeObjectURL(url);
    chat.addSystem(`STEP 文件已下载：${fname}`);
  } catch (e) {
    chat.addSystem(`STEP 导出错误: ${e.message}`);
  } finally {
    downloadStepBtn.textContent = orig;
    downloadStepBtn.disabled = false;
  }
});

// ── WebSocket ──────────────────────────────────────────────────────────────────
let ws        = null;
let agentBusy = false;
let _wsReady  = false;

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/chat/${SESSION_ID}`);

  ws.onopen = () => {
    _wsReady = true;
    setSendEnabled(true);
    trace.addInfo(`已连接  session=${SESSION_ID.slice(0, 8)}…`);
  };
  ws.onclose = () => {
    _wsReady = false;
    setSendEnabled(false);
    trace.addInfo('连接断开，3s 后重连…');
    setTimeout(connectWS, 3000);
  };
  ws.onerror  = () => trace.addInfo('WebSocket 连接错误');
  ws.onmessage = ({ data }) => {
    try { handleEvent(JSON.parse(data)); } catch (_) {}
  };
}

function handleEvent(evt) {
  switch (evt.type) {

    case 'agent_start':
      agentBusy = true;
      setSendEnabled(false);
      trace.onAgentStart();
      chat.startAgentStream();
      break;

    case 'agent_done':
      agentBusy = false;
      setSendEnabled(true);
      trace.onAgentDone();
      chat.finaliseAgentStream();
      break;

    case 'text_delta':
      chat.appendAgentText(evt.text);
      trace.onEvent(evt);
      break;

    case 'model_ready':
      // Load into 3D viewer + update history
      if (viewer && evt.filename && evt.filename.endsWith('.stl')) viewer.loadSTL(evt.url);
      addToHistory({ filename: evt.filename, url: evt.url, timestamp: evt.timestamp || now() });
      downloadStepBtn.disabled = false;
      chat.addSystem(`3D 模型已更新 ↗  ${evt.filename}`);
      trace.onEvent(evt);
      break;

    case 'error':
      agentBusy = false;
      setSendEnabled(true);
      chat.finaliseAgentStream();
      chat.addSystem(`错误: ${evt.message}`);
      trace.onEvent(evt);
      break;

    case 'session_ready':
      chat.clear();
      if (viewer) viewer.clearModel();
      trace.clear();
      _modelHistory = [];
      _currentModelUrl = null;
      downloadStlBtn.disabled = true;
      downloadStepBtn.disabled = true;
      renderHistory();
      trace.addInfo(`新会话  session=${evt.session_id.slice(0, 8)}…`);
      break;

    default:
      trace.onEvent(evt);
  }
}

function now() {
  return new Date().toLocaleTimeString('zh-CN', { hour12: false });
}

function setSendEnabled(on) {
  sendBtn.disabled  = !on;
  chatInput.disabled = !on;
}

// ── Send message ──────────────────────────────────────────────────────────────
function sendMessage() {
  const text = chatInput.value.trim();
  if (!text || agentBusy || !ws || ws.readyState !== WebSocket.OPEN) return;
  chat.addUser(text);
  ws.send(JSON.stringify({ action: 'chat', text }));
  chatInput.value = '';
  chatInput.style.height = 'auto';
}

sendBtn.addEventListener('click', sendMessage);
chatInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
chatInput.addEventListener('input', () => {
  chatInput.style.height = 'auto';
  chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
});

// ── New session ────────────────────────────────────────────────────────────────
newSessionBtn.addEventListener('click', () => {
  if (agentBusy) return;
  SESSION_ID = crypto.randomUUID();
  sessionStorage.setItem('cad_session_id', SESSION_ID);
  if (ws) ws.close();
  // Wait for ws.onclose to fire → reconnect → then notify server
  const poll = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      clearInterval(poll);
      ws.send(JSON.stringify({ action: 'new_session' }));
    }
  }, 150);
});

// ── Reset view ─────────────────────────────────────────────────────────────────
resetViewBtn.addEventListener('click', () => { if (viewer) viewer.resetView(); });

// ── Trace clear ────────────────────────────────────────────────────────────────
traceClear.addEventListener('click', () => trace.clear());

// ── LLM badge (P6) ────────────────────────────────────────────────────────────
function updateBadge(provider, model) {
  if (!provider) { llmBadgeText.textContent = '—'; llmBadge.classList.add('inactive'); return; }
  llmBadgeText.textContent = `${provider} / ${model || '?'}`;
  llmBadge.classList.remove('inactive');
}

async function fetchConfig() {
  try {
    const res = await fetch('/api/config');
    return await res.json();
  } catch (e) {
    console.warn('fetchConfig:', e.message);
    return null;
  }
}

// ── Settings modal (P6) ───────────────────────────────────────────────────────
let _cfg = null;

settingsBtn.addEventListener('click', () => {
  // Open immediately — don't await
  modalOverlay.classList.add('open');
  fetchConfig().then(cfg => {
    if (cfg) { _cfg = cfg; populateForm(cfg); }
  });
});

function closeModal() { modalOverlay.classList.remove('open'); }
modalClose.addEventListener('click', closeModal);
modalCancelBtn.addEventListener('click', closeModal);
modalOverlay.addEventListener('click', e => { if (e.target === modalOverlay) closeModal(); });

function populateForm(cfg) {
  const sel     = document.getElementById('cfg-provider');
  const modelEl = document.getElementById('cfg-model');
  const baseEl  = document.getElementById('cfg-baseurl');
  const statusEl = document.getElementById('cfg-status');

  sel.value = cfg.active_provider || 'openai';
  const pCfg = (cfg.providers || {})[cfg.active_provider] || {};
  modelEl.value = pCfg.model_name || '';
  baseEl.value  = pCfg.base_url   || '';
  document.getElementById('cfg-apikey').value = '';  // never pre-fill
  if (statusEl) {
    statusEl.textContent = pCfg.has_api_key ? '已配置' : '未配置';
    statusEl.className = `status-badge ${pCfg.has_api_key ? 'badge-ok' : 'badge-err'}`;
  }
}

document.getElementById('cfg-provider').addEventListener('change', () => {
  if (!_cfg) return;
  const p = document.getElementById('cfg-provider').value;
  const pCfg = (_cfg.providers || {})[p] || {};
  document.getElementById('cfg-model').value   = pCfg.model_name || '';
  document.getElementById('cfg-baseurl').value = pCfg.base_url   || '';
  document.getElementById('cfg-apikey').value  = '';
  const statusEl = document.getElementById('cfg-status');
  if (statusEl) {
    statusEl.textContent = pCfg.has_api_key ? '已配置' : '未配置';
    statusEl.className = `status-badge ${pCfg.has_api_key ? 'badge-ok' : 'badge-err'}`;
  }
});

saveConfigBtn.addEventListener('click', async () => {
  const provider  = document.getElementById('cfg-provider').value;
  const modelName = document.getElementById('cfg-model').value.trim();
  const apiKey    = document.getElementById('cfg-apikey').value.trim();
  const baseUrl   = document.getElementById('cfg-baseurl').value.trim() || null;

  const body = {
    active_provider: provider,
    provider_config: { model_name: modelName, base_url: baseUrl },
  };
  if (apiKey) body.provider_config.api_key = apiKey;

  try {
    const res  = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.status === 'ok') {
      closeModal();
      updateBadge(data.active_provider, data.model_name);
      trace.addInfo(`LLM 已切换: ${data.active_provider} / ${data.model_name}`);
    } else {
      alert(data.error || '保存失败');
    }
  } catch (e) {
    alert('网络错误: ' + e.message);
  }
});

// ── Boot ───────────────────────────────────────────────────────────────────────
setSendEnabled(false);
downloadStepBtn.disabled = true;
chat.addSystem('正在连接服务…');
connectWS();

// Load config and update badge
fetchConfig().then(cfg => {
  if (!cfg) return;
  _cfg = cfg;
  const pCfg = (cfg.providers || {})[cfg.active_provider] || {};
  updateBadge(cfg.active_provider, pCfg.model_name);
});
