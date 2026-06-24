/**
 * workflow.js — graphical flow diagram for the orchestration workflow (Tab 1).
 *
 * Backend events consumed:
 *   workflow_plan                { run_id, user_request, nodes:[{id,agent,title,instruction,depends_on,status}] }
 *   workflow_node_start          { run_id, node_id, agent, title }
 *   workflow_node_done           { run_id, node_id, status, summary, artifacts }
 *   workflow_node_reset          { run_id, node_id }
 *   workflow_node_paused         { run_id, node_id, agent, title }
 *   workflow_node_instruction_updated { run_id, node_id, instruction }
 *   workflow_loopback            { run_id, from_node, to_node, target_agent, reason, instruction, iteration }
 *   workflow_done                { run_id, status }
 *
 * Actions sent upstream via callbacks:
 *   onInterrupt(nodeId)
 *   onReset(nodeId)
 *   onResetWithInstruction(nodeId, instruction)
 *   onBreakpointToggle(nodeId, enabled)   — set_breakpoint / remove_breakpoint
 *   onResume(nodeId, instruction?)        — resume_node (null instruction = no override)
 */

// ── Agent visual config ──────────────────────────────────────────────────────

const AGENT_CFG = {
  cad:  { label: 'CAD',  color: '#4caf88', bg: '#0e2018', shape: 'hex'      },
  mesh: { label: 'MESH', color: '#9b8fff', bg: '#150e30', shape: 'diamond'  },
  cae:  { label: 'CAE',  color: '#f5a623', bg: '#2a1a00', shape: 'bolt'     },
  post: { label: 'POST', color: '#4f8ef7', bg: '#0a1a35', shape: 'chart'    },
};

const STATUS_CLASS = {
  pending:     'wfg-status-pending',
  running:     'wfg-status-running',
  success:     'wfg-status-success',
  failed:      'wfg-status-failed',
  skipped:     'wfg-status-skipped',
  interrupted: 'wfg-status-interrupted',
  paused:      'wfg-status-paused',
};

// Inline SVG shapes for each agent type
function agentShape(agent) {
  const cfg = AGENT_CFG[agent] || { color: '#7a8299', shape: 'hex' };
  const c = cfg.color;
  switch (cfg.shape) {
    case 'hex':
      return `<svg viewBox="0 0 32 32" width="32" height="32">
        <polygon points="16,2 28,9 28,23 16,30 4,23 4,9" fill="none" stroke="${c}" stroke-width="2"/>
        <text x="16" y="21" text-anchor="middle" font-size="11" font-weight="700" fill="${c}" font-family="monospace">CAD</text>
      </svg>`;
    case 'diamond':
      return `<svg viewBox="0 0 32 32" width="32" height="32">
        <polygon points="16,2 30,16 16,30 2,16" fill="none" stroke="${c}" stroke-width="2"/>
        <text x="16" y="21" text-anchor="middle" font-size="9" font-weight="700" fill="${c}" font-family="monospace">MESH</text>
      </svg>`;
    case 'bolt':
      return `<svg viewBox="0 0 32 32" width="32" height="32">
        <circle cx="16" cy="16" r="13" fill="none" stroke="${c}" stroke-width="2"/>
        <polyline points="19,4 12,16 18,16 13,28" fill="none" stroke="${c}" stroke-width="2.2" stroke-linejoin="round"/>
      </svg>`;
    case 'chart':
      return `<svg viewBox="0 0 32 32" width="32" height="32">
        <rect x="3" y="3" width="26" height="26" rx="4" fill="none" stroke="${c}" stroke-width="2"/>
        <rect x="7" y="18" width="4" height="8" fill="${c}" opacity=".8"/>
        <rect x="14" y="12" width="4" height="14" fill="${c}" opacity=".8"/>
        <rect x="21" y="7" width="4" height="19" fill="${c}" opacity=".8"/>
      </svg>`;
    default:
      return `<svg viewBox="0 0 32 32" width="32" height="32">
        <circle cx="16" cy="16" r="13" fill="none" stroke="${c}" stroke-width="2"/>
      </svg>`;
  }
}

// ── WorkflowView ─────────────────────────────────────────────────────────────

export class WorkflowView {
  constructor(canvasEl, placeholderEl) {
    this._canvas = canvasEl;       // #workflow-canvas
    this._ph     = placeholderEl;  // #workflow-placeholder
    this._nodes  = {};             // nodeId → { data, pipeEl, detailEl, breakpoint }
    this._activeDetailId = null;
    this._breakpoints = new Set();

    this._onInterrupt            = null;
    this._onReset                = null;
    this._onResetWithInstruction = null;
    this._onBreakpointToggle     = null;
    this._onResume               = null;
  }

  // ── Callback wiring ───────────────────────────────────────────────────────
  onInterrupt(fn)            { this._onInterrupt = fn; }
  onReset(fn)                { this._onReset = fn; }
  onResetWithInstruction(fn) { this._onResetWithInstruction = fn; }
  onBreakpointToggle(fn)     { this._onBreakpointToggle = fn; }
  onResume(fn)               { this._onResume = fn; }

  // ── Render ────────────────────────────────────────────────────────────────

  clear() {
    this._canvas.innerHTML = '';
    this._nodes = {};
    this._activeDetailId = null;
    this._breakpoints.clear();
    // Re-append placeholder (it was inside canvas and got wiped).
    if (this._ph) {
      this._canvas.appendChild(this._ph);
      this._ph.classList.remove('hidden');
    }
  }

  renderPlan(plan) {
    // Clear without re-adding placeholder — we're about to fill the canvas.
    this._canvas.innerHTML = '';
    this._nodes = {};
    this._activeDetailId = null;
    this._breakpoints.clear();

    // Build layout: pipeline track + detail area
    const root = document.createElement('div');
    root.className = 'wfg-root';

    // Pipeline track
    const track = document.createElement('div');
    track.className = 'wfg-track';

    // Start terminus
    track.appendChild(this._makeTerminus('start'));

    plan.nodes.forEach((node, i) => {
      const arrow = document.createElement('div');
      arrow.className = 'wfg-arrow';
      track.appendChild(arrow);
      track.appendChild(this._makePipeNode(node, i + 1));
    });

    // End terminus
    const lastArrow = document.createElement('div');
    lastArrow.className = 'wfg-arrow';
    track.appendChild(lastArrow);
    track.appendChild(this._makeTerminus('end'));

    root.appendChild(track);

    // Detail panel (one slot, shows selected node)
    const detail = document.createElement('div');
    detail.className = 'wfg-detail';
    detail.id = 'wfg-detail-panel';
    detail.innerHTML = `<div class="wfg-detail-empty">点击节点查看详情</div>`;
    root.appendChild(detail);

    this._canvas.appendChild(root);

    // Auto-select first node
    if (plan.nodes.length) this._showDetail(plan.nodes[0].id);
  }

  _makeTerminus(type) {
    const el = document.createElement('div');
    el.className = `wfg-terminus wfg-terminus-${type}`;
    el.innerHTML = type === 'start'
      ? `<svg viewBox="0 0 20 20" width="16" height="16"><polygon points="4,2 16,10 4,18" fill="currentColor"/></svg>`
      : `<svg viewBox="0 0 20 20" width="16" height="16"><rect x="3" y="2" width="14" height="16" rx="2" fill="currentColor"/></svg>`;
    return el;
  }

  _makePipeNode(node, seq) {
    const agent = (node.agent || 'cad').toLowerCase();
    const cfg   = AGENT_CFG[agent] || AGENT_CFG.cad;

    const wrap = document.createElement('div');
    wrap.className = `wfg-pipe-node wfg-status-pending`;
    wrap.dataset.nodeId = node.id;
    wrap.title = node.title;

    wrap.innerHTML = `
      <div class="wfg-bp-indicator" title="断点"></div>
      <div class="wfg-pipe-icon" style="--agent-color:${cfg.color};--agent-bg:${cfg.bg}">
        ${agentShape(agent)}
        <div class="wfg-pipe-status-ring"></div>
      </div>
      <div class="wfg-pipe-badge" style="color:${cfg.color}">${cfg.label}</div>
      <div class="wfg-pipe-title">${escHtml(node.title)}</div>
    `;

    wrap.addEventListener('click', () => this._showDetail(node.id));

    this._nodes[node.id] = {
      data: { ...node },
      pipeEl: wrap,
      breakpoint: false,
    };

    return wrap;
  }

  // ── Detail panel ──────────────────────────────────────────────────────────

  _showDetail(nodeId) {
    const n = this._nodes[nodeId];
    if (!n) return;
    this._activeDetailId = nodeId;

    // Highlight active in pipeline
    Object.values(this._nodes).forEach(m =>
      m.pipeEl.classList.toggle('wfg-pipe-selected', m === n)
    );

    const panel = document.getElementById('wfg-detail-panel');
    if (!panel) return;

    const agent = (n.data.agent || 'cad').toLowerCase();
    const cfg   = AGENT_CFG[agent] || AGENT_CFG.cad;
    const status = n.data.status || 'pending';
    const isPaused      = status === 'paused';
    const isInterrupted = status === 'interrupted';
    const isDone        = status === 'success' || status === 'failed';
    const isRunning     = status === 'running';
    const hasBp         = n.breakpoint;

    panel.innerHTML = `
      <div class="wfg-detail-head">
        <div class="wfg-detail-agent-icon" style="--agent-color:${cfg.color};--agent-bg:${cfg.bg}">
          ${agentShape(agent)}
        </div>
        <div class="wfg-detail-info">
          <div class="wfg-detail-badge" style="color:${cfg.color};border-color:${cfg.color}">${cfg.label}</div>
          <div class="wfg-detail-title">${escHtml(n.data.title)}</div>
        </div>
        <div class="wfg-detail-status-chip wfg-status-chip-${status}">${statusLabel(status)}</div>
      </div>
      <div class="wfg-detail-section-label">任务指令</div>
      <div class="wfg-detail-instruction">${escHtml(n.data.instruction || '')}</div>
      ${n.data.summary ? `<div class="wfg-detail-section-label">执行摘要</div><div class="wfg-detail-summary">${escHtml(n.data.summary)}</div>` : ''}
      ${n.data.artifacts && n.data.artifacts.length ? this._renderArtifacts(n.data.artifacts) : ''}
      <div class="wfg-detail-controls">
        <button class="wfg-ctrl-btn wfg-ctrl-bp${hasBp ? ' active' : ''}" data-action="bp" title="${hasBp ? '取消断点' : '设置断点'}">
          <span class="wfg-bp-dot${hasBp ? ' on' : ''}"></span>${hasBp ? '取消断点' : '设置断点'}
        </button>
        ${isRunning  ? `<button class="wfg-ctrl-btn wfg-ctrl-interrupt" data-action="interrupt">⏸ 中断</button>` : ''}
        ${(isDone || isInterrupted) ? `<button class="wfg-ctrl-btn wfg-ctrl-reset" data-action="reset">↻ 重置重跑</button>` : ''}
      </div>
      ${(isPaused || isInterrupted) ? this._renderReinputForm(nodeId, isPaused) : ''}
    `;

    // Wire up controls
    panel.querySelector('[data-action="bp"]')?.addEventListener('click', () => {
      this._toggleBreakpoint(nodeId);
    });
    panel.querySelector('[data-action="interrupt"]')?.addEventListener('click', () => {
      if (this._onInterrupt) this._onInterrupt(nodeId);
    });
    panel.querySelector('[data-action="reset"]')?.addEventListener('click', () => {
      if (this._onReset) this._onReset(nodeId);
    });
    // Re-input form
    const form = panel.querySelector('.wfg-reinput-form');
    if (form) {
      const submitBtn = form.querySelector('.wfg-reinput-submit');
      const textarea  = form.querySelector('.wfg-reinput-textarea');
      submitBtn?.addEventListener('click', () => {
        const text = (textarea?.value || '').trim();
        if (isPaused) {
          if (this._onResume) this._onResume(nodeId, text || null);
        } else {
          if (!text) return;
          if (this._onResetWithInstruction) this._onResetWithInstruction(nodeId, text);
        }
      });
    }
  }

  _renderArtifacts(artifacts) {
    const links = artifacts.map(a =>
      `<a class="wfg-artifact-link" href="${a.url}" download="${a.filename}">&#8595; ${escHtml(a.filename)}</a>`
    ).join('');
    return `<div class="wfg-detail-section-label">产出文件</div><div class="wfg-detail-artifacts">${links}</div>`;
  }

  _renderReinputForm(nodeId, isPaused) {
    const labelText = isPaused
      ? '已在断点处暂停。可修改本节点指令后继续执行，或直接继续：'
      : '已中断。可重新描述该节点的要求后重新提交：';
    const submitText = isPaused ? '继续执行' : '重新提交';
    return `
      <div class="wfg-reinput-form">
        <div class="wfg-reinput-label">${labelText}</div>
        <textarea class="wfg-reinput-textarea" rows="3" placeholder="输入新的指令（留空则使用原指令）"></textarea>
        <div class="wfg-reinput-actions">
          <button class="wfg-ctrl-btn wfg-ctrl-primary wfg-reinput-submit">${submitText}</button>
        </div>
      </div>
    `;
  }

  _toggleBreakpoint(nodeId) {
    const n = this._nodes[nodeId];
    if (!n) return;
    n.breakpoint = !n.breakpoint;
    // Update bp indicator on pipe node
    n.pipeEl.querySelector('.wfg-bp-indicator')?.classList.toggle('on', n.breakpoint);
    if (this._onBreakpointToggle) this._onBreakpointToggle(nodeId, n.breakpoint);
    // Refresh detail so button label updates
    this._showDetail(nodeId);
  }

  // ── Status updates ────────────────────────────────────────────────────────

  setNodeStatus(nodeId, status) {
    const n = this._nodes[nodeId];
    if (!n) return;
    n.data.status = status;
    // Update pipe node CSS class
    const pipe = n.pipeEl;
    Object.values(STATUS_CLASS).forEach(c => pipe.classList.remove(c));
    pipe.classList.add(STATUS_CLASS[status] || STATUS_CLASS.pending);
    // Refresh detail if active
    if (this._activeDetailId === nodeId) this._showDetail(nodeId);
  }

  setNodePaused(nodeId) {
    this.setNodeStatus(nodeId, 'paused');
    // Auto-select paused node
    this._showDetail(nodeId);
    // Scroll pipeline node into view
    this._nodes[nodeId]?.pipeEl.scrollIntoView({ inline: 'center', behavior: 'smooth' });
  }

  setNodeDone(nodeId, status, summary, artifacts) {
    const n = this._nodes[nodeId];
    if (!n) return;
    n.data.status   = status;
    n.data.summary  = summary || '';
    n.data.artifacts = artifacts || [];
    this.setNodeStatus(nodeId, status);
  }

  updateNodeInstruction(nodeId, instruction) {
    const n = this._nodes[nodeId];
    if (!n) return;
    n.data.instruction = instruction;
    if (this._activeDetailId === nodeId) this._showDetail(nodeId);
  }

  /**
   * Visualise a self-healing loop-back: the workflow rolled back from
   * `fromId` to the upstream `toId` to re-run with a corrective instruction.
   * Shows an iteration badge on the target node and pulses it.
   */
  markLoopback(fromId, toId, info = {}) {
    const target = this._nodes[toId];
    if (!target) return;
    target.data.loopback = {
      iteration: info.iteration, reason: info.reason,
      from: fromId, instruction: info.instruction,
    };

    // Iteration badge on the target pipe node.
    let badge = target.pipeEl.querySelector('.wfg-loop-badge');
    if (!badge) {
      badge = document.createElement('div');
      badge.className = 'wfg-loop-badge';
      badge.style.cssText =
        'position:absolute;top:-6px;right:-6px;min-width:18px;height:18px;' +
        'padding:0 4px;border-radius:9px;background:#f5a623;color:#1a1200;' +
        'font:700 11px/18px monospace;text-align:center;z-index:3;' +
        'box-shadow:0 0 6px rgba(245,166,35,.7)';
      target.pipeEl.style.position = 'relative';
      target.pipeEl.appendChild(badge);
    }
    badge.textContent = `↺${info.iteration || ''}`;
    badge.title = `自愈回退 #${info.iteration || ''}：${info.reason || ''}`;

    // Pulse + scroll into view so the user notices the rollback.
    target.pipeEl.animate(
      [{ filter: 'brightness(1)' }, { filter: 'brightness(1.8)' }, { filter: 'brightness(1)' }],
      { duration: 700, iterations: 2 },
    );
    target.pipeEl.scrollIntoView({ inline: 'center', behavior: 'smooth' });
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function statusLabel(status) {
  return {
    pending:     '等待中',
    running:     '运行中',
    success:     '已完成',
    failed:      '失败',
    skipped:     '已跳过',
    interrupted: '已中断',
    paused:      '断点暂停',
  }[status] || status;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
