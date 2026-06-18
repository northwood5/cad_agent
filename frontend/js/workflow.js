/**
 * workflow.js — render the orchestration workflow (Tab 1).
 *
 * Consumes the backend workflow events:
 *   workflow_plan       { run_id, user_request, nodes:[{id,agent,title,instruction,depends_on,status}] }
 *   workflow_node_start { run_id, node_id, agent, title }
 *   workflow_node_done  { run_id, node_id, status, summary, artifacts:[{filename,kind,url}] }
 *   workflow_done       { run_id, status }
 */

const STATUS_ICON = {
  pending: '⏳',
  running: '🔄',
  success: '✅',
  failed:  '❌',
  skipped: '⏭️',
};

const AGENT_LABEL = { cad: 'CAD', mesh: 'MESH', cae: 'CAE' };

export class WorkflowView {
  constructor(listEl, placeholderEl) {
    this._list = listEl;
    this._ph = placeholderEl;
    this._nodes = {};        // node_id -> { data, el refs }
  }

  clear() {
    this._list.innerHTML = '';
    this._nodes = {};
    if (this._ph) this._ph.classList.remove('hidden');
  }

  /** Render a fresh plan (replaces any previous workflow). */
  renderPlan(plan) {
    this.clear();
    if (this._ph) this._ph.classList.add('hidden');
    const total = plan.nodes.length;
    plan.nodes.forEach((node, i) => this._addNode(node, i + 1, total));
  }

  _addNode(node, seq, total) {
    const wrap = document.createElement('div');
    wrap.className = `wf-node ${node.status || 'pending'}`;

    const agent = (node.agent || 'cad').toLowerCase();
    const badgeCls = `badge-${agent}`;
    const agentLabel = AGENT_LABEL[agent] || agent.toUpperCase();

    wrap.innerHTML = `
      <div class="wf-rail">
        <div class="wf-bullet">${seq}</div>
        <div class="wf-connector"></div>
      </div>
      <div class="wf-body">
        <div class="wf-card">
          <div class="wf-card-head">
            <span class="wf-agent-badge ${badgeCls}">${agentLabel}</span>
            <span class="wf-title">${escapeHtml(node.title || '步骤')}</span>
            <span class="wf-status-icon">${STATUS_ICON[node.status] || STATUS_ICON.pending}</span>
          </div>
          <div class="wf-instruction">${escapeHtml(node.instruction || '')}</div>
          <div class="wf-summary hidden"></div>
          <div class="wf-artifacts"></div>
        </div>
      </div>
    `;
    this._list.appendChild(wrap);
    this._nodes[node.id] = {
      data: node,
      wrap,
      bullet: wrap.querySelector('.wf-bullet'),
      statusIcon: wrap.querySelector('.wf-status-icon'),
      summary: wrap.querySelector('.wf-summary'),
      artifacts: wrap.querySelector('.wf-artifacts'),
    };
  }

  setNodeStatus(nodeId, status) {
    const n = this._nodes[nodeId];
    if (!n) return;
    n.wrap.className = `wf-node ${status}`;
    n.statusIcon.textContent = STATUS_ICON[status] || STATUS_ICON.pending;
    if (status === 'success') n.bullet.textContent = '✓';
    else if (status === 'failed') n.bullet.textContent = '!';
    this._scroll(n.wrap);
  }

  setNodeDone(nodeId, status, summary, artifacts) {
    this.setNodeStatus(nodeId, status);
    const n = this._nodes[nodeId];
    if (!n) return;
    if (summary) {
      n.summary.textContent = summary;
      n.summary.classList.remove('hidden');
    }
    if (artifacts && artifacts.length) {
      n.artifacts.innerHTML = '';
      artifacts.forEach(a => {
        const link = document.createElement('a');
        link.className = 'wf-artifact';
        link.href = a.url;
        link.download = a.filename;
        link.textContent = `⬇ ${a.filename}`;
        n.artifacts.appendChild(link);
      });
    }
  }

  _scroll(el) {
    el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
