/**
 * user.js — lightweight user + project management (no passwords).
 *
 * - Resolves a username (localStorage or prompt) -> user_id via /api/users/login
 * - Loads the user's projects, creates a default one if none exist
 * - Drives the header user chip + project <select> + new-project button
 * - Fires onProjectChange(projectId) whenever the active project switches,
 *   so main.js can (re)connect the WebSocket and load that project's history.
 */
export class UserManager {
  constructor() {
    this._chip = document.getElementById('user-chip');
    this._nameEl = document.getElementById('user-name');
    this._select = document.getElementById('project-select');
    this._newBtn = document.getElementById('btn-new-project');

    this.userId = null;
    this.username = null;
    this.projectId = null;
    this._projects = [];
    this._onProjectChange = null;

    this._chip.addEventListener('click', () => this._changeUser());
    this._newBtn.addEventListener('click', () => this._createProjectPrompt());
    this._select.addEventListener('change', () => {
      this.projectId = parseInt(this._select.value, 10);
      localStorage.setItem('cax_project_id', String(this.projectId));
      this._fire();
    });
  }

  onProjectChange(fn) { this._onProjectChange = fn; }

  async init() {
    const stored = localStorage.getItem('cax_username');
    const username = stored || (prompt('请输入用户名：') || '默认用户').trim() || '默认用户';
    await this._login(username);
  }

  async _login(username) {
    const res = await fetch('/api/users/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username }),
    });
    const user = await res.json();
    this.userId = user.id;
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

    // Restore last-used project if it belongs to this user, else newest.
    const last = parseInt(localStorage.getItem('cax_project_id') || '', 10);
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
      opt.value = String(p.id);
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
    this.projectId = project.id;
    this._select.value = String(project.id);
    localStorage.setItem('cax_project_id', String(project.id));
    if (fire) this._fire();
    else this._fire();   // always notify so WS connects on first load
    return project;
  }

  async _createProjectPrompt() {
    const name = (prompt('新项目名称：', '未命名项目') || '').trim();
    if (!name) return;
    await this._createProject(name);
  }

  async _changeUser() {
    const name = (prompt('切换用户名：', this.username || '') || '').trim();
    if (!name || name === this.username) return;
    localStorage.setItem('cax_username', name);
    localStorage.removeItem('cax_project_id');
    await this._login(name);
  }

  _fire() {
    if (this._onProjectChange && this.projectId != null) {
      this._onProjectChange(this.projectId);
    }
  }
}
