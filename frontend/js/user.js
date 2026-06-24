/**
 * user.js — lightweight user + project management (no passwords).
 *
 * - Resolves a username (localStorage or inline modal) -> user_id via /api/users/login
 * - Loads the user's projects, creates a default one if none exist
 * - Drives the header user chip + project <select> + new-project button
 * - Fires onProjectChange(projectId) whenever the active project switches,
 *   so main.js can (re)connect the WebSocket and load that project's history.
 */
export class UserManager {
  constructor() {
    this._chip    = document.getElementById('user-chip');
    this._nameEl  = document.getElementById('user-name');
    this._select  = document.getElementById('project-select');
    this._newBtn  = document.getElementById('btn-new-project');

    // New-project modal elements
    this._npModal   = document.getElementById('modal-new-project');
    this._npInput   = document.getElementById('new-project-name');
    this._npConfirm = document.getElementById('new-project-confirm');
    this._npCancel  = document.getElementById('new-project-cancel');
    this._npClose   = document.getElementById('new-project-close');

    this.userId   = null;
    this.username = null;
    this.projectId = null;
    this._projects = [];
    this._onProjectChange = null;
    this._npResolve = null;   // resolve handle for the open modal promise

    this._chip.addEventListener('click', () => this._changeUser());
    this._newBtn.addEventListener('click', () => this._createProjectPrompt());

    this._select.addEventListener('change', () => {
      this.projectId = parseInt(this._select.value, 10);
      localStorage.setItem('cax_project_id', String(this.projectId));
      this._fire();
    });

    // Wire up modal buttons
    const closeModal = () => {
      this._npModal.classList.remove('open');
      if (this._npResolve) { this._npResolve(null); this._npResolve = null; }
    };
    this._npClose.addEventListener('click', closeModal);
    this._npCancel.addEventListener('click', closeModal);
    this._npModal.addEventListener('click', e => { if (e.target === this._npModal) closeModal(); });

    this._npConfirm.addEventListener('click', () => {
      const name = (this._npInput.value || '').trim();
      this._npModal.classList.remove('open');
      if (this._npResolve) { this._npResolve(name || null); this._npResolve = null; }
    });
    this._npInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') this._npConfirm.click();
      if (e.key === 'Escape') this._npClose.click();
    });
  }

  onProjectChange(fn) { this._onProjectChange = fn; }

  async init() {
    const stored = localStorage.getItem('cax_username');
    let username = stored;
    if (!username) {
      username = await this._promptUsername();
      if (!username) username = '默认用户';
    }
    await this._login(username);
  }

  // ── Inline modals (replacing browser prompt()) ──────────────────────────────

  /** Show the new-project modal and return the entered name (or null if cancelled). */
  _openNewProjectModal(defaultName = '未命名项目') {
    this._npInput.value = defaultName;
    this._npModal.classList.add('open');
    requestAnimationFrame(() => {
      this._npInput.focus();
      this._npInput.select();
    });
    return new Promise(resolve => { this._npResolve = resolve; });
  }

  /** For username input we build a temporary inline modal on the fly. */
  _promptUsername() {
    return new Promise(resolve => {
      // Reuse the new-project modal structure but with a different title.
      const overlay = document.createElement('div');
      overlay.className = 'modal-overlay open';
      overlay.innerHTML = `
        <div class="modal" style="width:360px">
          <div class="modal-title-row">
            <h2>请输入用户名</h2>
          </div>
          <div class="form-row">
            <label>用户名</label>
            <input id="_tmp-username-input" type="text" placeholder="默认用户" autocomplete="off" />
          </div>
          <div class="modal-actions">
            <button class="btn btn-primary" id="_tmp-username-ok">确认</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      const input = overlay.querySelector('#_tmp-username-input');
      const okBtn = overlay.querySelector('#_tmp-username-ok');
      requestAnimationFrame(() => input.focus());
      const done = () => {
        const val = (input.value || '').trim() || '默认用户';
        overlay.remove();
        resolve(val);
      };
      okBtn.addEventListener('click', done);
      input.addEventListener('keydown', e => { if (e.key === 'Enter') done(); });
    });
  }

  // ── Auth / project loading ───────────────────────────────────────────────────

  async _login(username) {
    const res = await fetch('/api/users/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username }),
    });
    const user = await res.json();
    this.userId   = user.id;
    this.username = user.username;
    localStorage.setItem('cax_username', user.username);
    this._nameEl.textContent = user.username;

    await this._loadProjects();
  }

  async _loadProjects() {
    const res = await fetch(`/api/users/${this.userId}/projects`);
    const data = await res.json();
    this._projects = data.projects || [];

    if (this._projects.length === 0) {
      await this._createProject('默认项目', false);
      return;
    }
    this._renderProjectOptions();

    const last  = parseInt(localStorage.getItem('cax_project_id') || '', 10);
    const match = this._projects.find(p => p.id === last);
    this.projectId = match ? match.id : this._projects[0].id;
    this._select.value = String(this.projectId);
    localStorage.setItem('cax_project_id', String(this.projectId));
    this._fire();
  }

  _renderProjectOptions() {
    this._select.innerHTML = '';
    this._projects.forEach(p => {
      const opt = document.createElement('option');
      opt.value    = String(p.id);
      opt.textContent = p.name;
      this._select.appendChild(opt);
    });
  }

  async _createProject(name, fire = true) {
    const res = await fetch(`/api/users/${this.userId}/projects`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const project = await res.json();
    this._projects.unshift(project);
    this._renderProjectOptions();
    this.projectId     = project.id;
    this._select.value = String(project.id);
    localStorage.setItem('cax_project_id', String(project.id));
    this._fire();   // always fire so WS connects
    return project;
  }

  async _createProjectPrompt() {
    const name = await this._openNewProjectModal();
    if (!name) return;
    await this._createProject(name);
  }

  async _changeUser() {
    const newName = await this._promptChangeUser();
    if (!newName || newName === this.username) return;
    localStorage.setItem('cax_username', newName);
    localStorage.removeItem('cax_project_id');
    await this._login(newName);
  }

  _promptChangeUser() {
    return new Promise(resolve => {
      const overlay = document.createElement('div');
      overlay.className = 'modal-overlay open';
      overlay.innerHTML = `
        <div class="modal" style="width:360px">
          <div class="modal-title-row">
            <h2>切换用户</h2>
            <span class="modal-close" id="_tmp-user-close">×</span>
          </div>
          <div class="form-row">
            <label>用户名</label>
            <input id="_tmp-user-input" type="text" value="${this.username || ''}" autocomplete="off" />
          </div>
          <div class="modal-actions">
            <button class="btn" id="_tmp-user-cancel">取消</button>
            <button class="btn btn-primary" id="_tmp-user-ok">确认</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      const input  = overlay.querySelector('#_tmp-user-input');
      const okBtn  = overlay.querySelector('#_tmp-user-ok');
      const cancel = () => { overlay.remove(); resolve(null); };
      requestAnimationFrame(() => { input.focus(); input.select(); });
      overlay.querySelector('#_tmp-user-close').addEventListener('click', cancel);
      overlay.querySelector('#_tmp-user-cancel').addEventListener('click', cancel);
      overlay.addEventListener('click', e => { if (e.target === overlay) cancel(); });
      const done = () => {
        const val = (input.value || '').trim();
        overlay.remove();
        resolve(val || null);
      };
      okBtn.addEventListener('click', done);
      input.addEventListener('keydown', e => {
        if (e.key === 'Enter') done();
        if (e.key === 'Escape') cancel();
      });
    });
  }

  // ── Public helpers ───────────────────────────────────────────────────────────

  /** Re-fetch projects list (e.g. after external deletion). */
  async reloadProjects() {
    await this._loadProjects();
  }

  /** Programmatically switch to a project (called by the file sidebar). */
  switchToProject(projectId) {
    const project = this._projects.find(p => p.id === projectId);
    if (!project) return;
    this.projectId     = projectId;
    this._select.value = String(projectId);
    localStorage.setItem('cax_project_id', String(projectId));
    this._fire();
  }

  _fire() {
    if (this._onProjectChange && this.projectId != null) {
      this._onProjectChange(this.projectId);
    }
  }
}
