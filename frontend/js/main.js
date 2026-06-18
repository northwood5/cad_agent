/**
 * main.js — entry point.
 *
 * Wires together: user/project management, WebSocket event stream, chat,
 * the three graphics tabs (workflow / 3D viewer / scripts), the agent trace
 * panel, and the LLM settings modal.
 */
import { ChatPanel }    from './chat.js';
import { AgentTrace }   from './agent_trace.js';
import { Tabs }         from './tabs.js';
import { WorkflowView } from './workflow.js';
import { ScriptsView }  from './scripts.js';
import { UserManager }  from './user.js';

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
const chat     = new ChatPanel(chatMsgsEl);
const trace    = new AgentTrace(traceLogEl, traceDotEl);
const tabs     = new Tabs();
const workflow = new WorkflowView(
  document.getElementById('workflow-list'),
  document.getElementById('workflow-placeholder'),
);
const scripts  = new ScriptsView(
  document.getElementById('scripts-list'),
  document.getElementById('scripts-placeholder'),
);
const user     = new UserManager();
let   viewer   = null;

let PROJECT_ID = null;
let ws         = null;
let agentBusy  = false;

async function initViewer() {
  try {
    const { Viewer3D } = await import('./viewer3d.js');
    viewer = new Viewer3D(viewerWrap);
    trace.addInfo('3D 渲染器就绪');
  } catch (e) {
    console.warn('[viewer] 初始化失败:', e.message);
    const ph = document.getElementById('viewer-placeholder');
    if (ph) ph.innerHTML = '<div class="icon">⚠</div><div>3D 预览加载失败</div>';
  }
}

// When the viewer becomes visible after being hidden, fix its size.
tabs.onSwitch((name) => { if (name === 'viewer' && viewer) viewer.resetView(); });

// ── Model history (per project) ────────────────────────────────────────────────
let _currentModelUrl = null;
let _modelHistory    = [];

function addToHistory(entry) {
  _modelHistory.push(entry);
  _currentModelUrl = entry.url;
  downloadStlBtn.disabled = false;
  downloadStlBtn.title = `下载 ${entry.filename}`;
  downloadStepBtn.disabled = false;
  renderHistory();
}

function renderHistory() {
  if (_modelHistory.length === 0) {
    historyList.innerHTML = '<div class="history-empty">暂无历史记录</div>';
    return;
  }
  historyList.innerHTML = '';
  [..._modelHistory].reverse().forEach((item, i) => {
    const isLatest = (i === 0);
    const div = document.createElement('div');
    div.className = `history-item${isLatest ? ' active' : ''}`;
    div.innerHTML = `
      <div class="history-item-name">${item.filename}</div>
      <div class="history-item-time">${item.timestamp}</div>
      <a class="history-item-dl" href="${item.url}" download="${item.filename}">⬇ 下载</a>
    `;
    div.addEventListener('click', (e) => {
      if (e.target.tagName === 'A') return;
      if (viewer && item.filename.endsWith('.stl')) viewer.loadSTL(item.url);
      _currentModelUrl = item.url;
      historyList.querySelectorAll('.history-item').forEach(el => el.classList.remove('active'));
      div.classList.add('active');
    });
    historyList.appendChild(div);
  });
}

historyToggle.addEventListener('click', () => {
  historyPanel.classList.toggle('hidden');
  historyToggle.classList.toggle('active');
});
historyClose.addEventListener('click', () => {
  historyPanel.classList.add('hidden');
  historyToggle.classList.remove('active');
});

// ── Downloads ───────────────────────────────────────────────────────────────
downloadStlBtn.addEventListener('click', () => {
  if (!_currentModelUrl) return;
  const a = document.createElement('a');
  a.href = _currentModelUrl;
  a.download = _currentModelUrl.split('/').pop();
  a.click();
});

downloadStepBtn.addEventListener('click', async () => {
  if (downloadStepBtn.disabled || PROJECT_ID == null) return;
  const orig = downloadStepBtn.textContent;
  downloadStepBtn.textContent = '导出中…';
  downloadStepBtn.disabled = true;
  try {
    const res = await fetch(`/api/sessions/${PROJECT_ID}/export/step`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      chat.addSystem(`STEP 导出失败: ${err.error || res.statusText}`);
      return;
    }
    const blob = await res.blob();
    const disp = res.headers.get('Content-Disposition') || '';
    const match = disp.match(/filename="([^"]+)"/);
    const fname = match ? match[1] : `export_${PROJECT_ID}.step`;
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

// ── WebSocket (per project) ────────────────────────────────────────────────────
function connectWS(projectId) {
  if (ws) { try { ws.onclose = null; ws.close(); } catch (_) {} }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/chat/${projectId}`);

  ws.onopen = () => {
    setSendEnabled(true);
    trace.addInfo(`已连接  项目 #${projectId}`);
  };
  ws.onclose = () => {
    setSendEnabled(false);
    trace.addInfo('连接断开，3s 后重连…');
    setTimeout(() => { if (PROJECT_ID === projectId) connectWS(projectId); }, 3000);
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

    // ── Workflow (Tab 1) ──
    case 'workflow_plan':
      workflow.renderPlan(evt);
      tabs.notify('workflow');
      break;
    case 'workflow_node_start':
      workflow.setNodeStatus(evt.node_id, 'running');
      break;
    case 'workflow_node_done':
      workflow.setNodeDone(evt.node_id, evt.status, evt.summary, evt.artifacts);
      break;
    case 'workflow_done':
      trace.addInfo(`工作流完成：${evt.status}`);
      break;

    // ── Script log (Tab 3) ──
    case 'script_generated':
      scripts.add(evt);
      tabs.notify('scripts');
      break;

    // ── Model (Tab 2) ──
    case 'model_ready':
      if (viewer && evt.filename && evt.filename.endsWith('.stl')) viewer.loadSTL(evt.url);
      addToHistory({ filename: evt.filename, url: evt.url, timestamp: now() });
      tabs.notify('viewer');
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
      resetProjectUI();
      trace.addInfo(`场景已清空  项目 #${evt.session_id}`);
      break;

    default:
      trace.onEvent(evt);
  }
}

function resetProjectUI() {
  chat.clear();
  if (viewer) viewer.clearModel();
  trace.clear();
  workflow.clear();
  scripts.clear();
  _modelHistory = [];
  _currentModelUrl = null;
  downloadStlBtn.disabled = true;
  downloadStepBtn.disabled = true;
  renderHistory();
}

function now() {
  return new Date().toLocaleTimeString('zh-CN', { hour12: false });
}

function setSendEnabled(on) {
  sendBtn.disabled  = !on;
  chatInput.disabled = !on;
}

// ── Load a project's persisted history ──────────────────────────────────────────
async function loadProjectHistory(projectId) {
  resetProjectUI();
  try {
    const [msgs, scr] = await Promise.all([
      fetch(`/api/projects/${projectId}/messages`).then(r => r.json()),
      fetch(`/api/projects/${projectId}/scripts`).then(r => r.json()),
    ]);
    (msgs.messages || []).forEach(m => {
      if (m.role === 'user') chat.addUser(m.content);
      else if (m.role === 'agent') { chat.startAgentStream(); chat.appendAgentText(m.content); chat.finaliseAgentStream(); }
      else chat.addSystem(m.content);
    });
    if (scr.scripts && scr.scripts.length) scripts.hydrate(scr.scripts);
  } catch (e) {
    console.warn('loadProjectHistory:', e.message);
  }
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

// ── Clear scene (within current project) ───────────────────────────────────────
newSessionBtn.addEventListener('click', () => {
  if (agentBusy || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ action: 'new_session' }));
});

resetViewBtn.addEventListener('click', () => { if (viewer) viewer.resetView(); });
traceClear.addEventListener('click', () => trace.clear());

// ── Project switching ───────────────────────────────────────────────────────────
user.onProjectChange(async (projectId) => {
  PROJECT_ID = projectId;
  setSendEnabled(false);
  await loadProjectHistory(projectId);
  connectWS(projectId);
});

// ── LLM badge + settings modal ──────────────────────────────────────────────────
function updateBadge(provider, model) {
  if (!provider) { llmBadgeText.textContent = '—'; llmBadge.classList.add('inactive'); return; }
  llmBadgeText.textContent = `${provider} / ${model || '?'}`;
  llmBadge.classList.remove('inactive');
}

async function fetchConfig() {
  try { return await (await fetch('/api/config')).json(); }
  catch (e) { console.warn('fetchConfig:', e.message); return null; }
}

let _cfg = null;
settingsBtn.addEventListener('click', () => {
  modalOverlay.classList.add('open');
  fetchConfig().then(cfg => { if (cfg) { _cfg = cfg; populateForm(cfg); } });
});

function closeModal() { modalOverlay.classList.remove('open'); }
modalClose.addEventListener('click', closeModal);
modalCancelBtn.addEventListener('click', closeModal);
modalOverlay.addEventListener('click', e => { if (e.target === modalOverlay) closeModal(); });

function populateForm(cfg) {
  const sel = document.getElementById('cfg-provider');
  const modelEl = document.getElementById('cfg-model');
  const baseEl = document.getElementById('cfg-baseurl');
  const statusEl = document.getElementById('cfg-status');
  sel.value = cfg.active_provider || 'openai';
  const pCfg = (cfg.providers || {})[cfg.active_provider] || {};
  modelEl.value = pCfg.model_name || '';
  baseEl.value  = pCfg.base_url   || '';
  document.getElementById('cfg-apikey').value = '';
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
chat.addSystem('正在初始化…');
initViewer();
user.init();
fetchConfig().then(cfg => {
  if (!cfg) return;
  _cfg = cfg;
  const pCfg = (cfg.providers || {})[cfg.active_provider] || {};
  updateBadge(cfg.active_provider, pCfg.model_name);
});
