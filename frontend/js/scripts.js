/**
 * scripts.js — render the CAx script log (Tab 3).
 *
 * Consumes:
 *   script_generated { node_id, agent, software, language, filename, content }
 * and can hydrate from GET /api/projects/{pid}/scripts on project load.
 */
export class ScriptsView {
  constructor(listEl, placeholderEl) {
    this._list = listEl;
    this._ph = placeholderEl;
    this._count = 0;
  }

  clear() {
    this._list.innerHTML = '';
    if (this._ph) {
      this._list.appendChild(this._ph);
      this._ph.classList.remove('hidden');
    }
    this._count = 0;
  }

  get count() { return this._count; }

  /** Append one script card (newest at top). */
  add(script) {
    if (this._ph) this._ph.classList.add('hidden');
    const card = document.createElement('div');
    card.className = 'script-card';

    const software = (script.software || 'cax').toUpperCase();
    const meta = [script.filename, script.language].filter(Boolean).join('  ·  ');

    const head = document.createElement('div');
    head.className = 'script-head';
    head.innerHTML = `
      <span class="script-software">${escapeHtml(software)}</span>
      <span class="script-meta">${escapeHtml(meta || '脚本')}</span>
      <div class="script-actions">
        <button class="btn-sm js-copy">复制</button>
        <button class="btn-sm js-dl">下载</button>
      </div>
    `;

    const body = document.createElement('pre');
    body.className = 'script-body';
    body.textContent = script.content || '';

    card.appendChild(head);
    card.appendChild(body);
    this._list.insertBefore(card, this._list.firstChild);
    this._count++;

    head.querySelector('.js-copy').addEventListener('click', () => {
      navigator.clipboard?.writeText(script.content || '');
      const b = head.querySelector('.js-copy');
      const orig = b.textContent; b.textContent = '已复制'; setTimeout(() => b.textContent = orig, 1200);
    });
    head.querySelector('.js-dl').addEventListener('click', () => {
      const blob = new Blob([script.content || ''], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = script.filename || `script.${script.language || 'txt'}`;
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  /** Replace the list with history from the backend (oldest first → newest on top). */
  hydrate(scripts) {
    this.clear();
    // backend returns newest first; add() prepends, so iterate reversed
    [...scripts].reverse().forEach(s => this.add(s));
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
