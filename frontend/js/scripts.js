/**
 * scripts.js — render the CAx script log (Tab 3).
 *
 * All generated scripts are appended into ONE fixed-height scrollable text
 * window (separated by labelled dividers) instead of one card per script,
 * so the view never fills up with shrinking panels.
 *
 * Consumes:
 *   script_generated { node_id, agent, software, language, filename, content }
 * and can hydrate from GET /api/projects/{pid}/scripts on project load.
 */
export class ScriptsView {
  constructor(listEl, placeholderEl) {
    this._list = listEl;
    this._ph = placeholderEl;
    this._pre = null;
    this._countEl = null;
    this._scripts = [];
    this._seen = new Set();
  }

  clear() {
    this._list.innerHTML = '';
    if (this._ph) {
      this._list.appendChild(this._ph);
      this._ph.classList.remove('hidden');
    }
    this._pre = null;
    this._countEl = null;
    this._scripts = [];
    this._seen = new Set();
  }

  _key(script) {
    return [script.agent, script.software, script.filename,
            (script.content || '').length].join('|');
  }

  get count() { return this._scripts.length; }

  _ensureView() {
    if (this._pre) return;
    if (this._ph) this._ph.classList.add('hidden');

    const bar = document.createElement('div');
    bar.className = 'scripts-actionbar';
    bar.innerHTML = `
      <span class="scripts-count">0 个脚本</span>
      <div class="script-actions">
        <button class="btn-sm js-copy-all">复制全部</button>
        <button class="btn-sm js-dl-all">下载全部</button>
      </div>
    `;
    const pre = document.createElement('pre');
    pre.className = 'scripts-text';

    this._list.appendChild(bar);
    this._list.appendChild(pre);
    this._pre = pre;
    this._countEl = bar.querySelector('.scripts-count');

    bar.querySelector('.js-copy-all').addEventListener('click', (e) => {
      navigator.clipboard?.writeText(this._pre.textContent || '');
      const b = e.currentTarget; const o = b.textContent;
      b.textContent = '已复制'; setTimeout(() => b.textContent = o, 1200);
    });
    bar.querySelector('.js-dl-all').addEventListener('click', () => {
      const blob = new Blob([this._pre.textContent || ''], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'cax_scripts.txt';
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  /** Append one script to the single text window (newest at the bottom). */
  add(script) {
    // Skip duplicates so events replayed on reconnect don't double up scripts
    // already restored from the DB via hydrate().
    const key = this._key(script);
    if (this._seen.has(key)) return;
    this._seen.add(key);

    this._ensureView();
    this._scripts.push(script);
    const idx = this._scripts.length;

    const software = (script.software || 'cax').toUpperCase();
    const meta = [script.filename, script.language].filter(Boolean).join('  ·  ');
    const rule = '─'.repeat(64);
    const header =
      `${idx === 1 ? '' : '\n'}${rule}\n` +
      `# [${idx}] ${software}  ·  ${meta || '脚本'}\n` +
      `${rule}\n`;

    this._pre.textContent += header + (script.content || '') + '\n';
    if (this._countEl) this._countEl.textContent = `${idx} 个脚本`;
    this._pre.scrollTop = this._pre.scrollHeight;
  }

  /** Replace the view with history from the backend (oldest first → newest at bottom). */
  hydrate(scripts) {
    this.clear();
    [...scripts].reverse().forEach(s => this.add(s));
  }
}
