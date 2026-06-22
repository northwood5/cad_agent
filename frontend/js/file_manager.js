/**
 * file_manager.js — VS Code-style project file sidebar.
 *
 * Shows all projects for the current user as an expandable tree.
 * Each project expands to a recursive file/directory tree fetched from
 * GET /api/projects/{id}/files.
 *
 * Features
 * ────────
 * • Click a project row to expand/collapse it (and switch to it).
 * • Checkboxes on files allow multi-select; a sticky toolbar shows the
 *   selection count and a "删除所选" button.
 * • Each file row is draggable for the text-preview tab (Tab 4).
 * • Individual delete (×) button on hover for files and projects.
 */
export class FileManager {
  /**
   * @param {HTMLElement} rootEl      - #fm-tree-root container
   * @param {HTMLElement} refreshBtn  - #fm-refresh button
   * @param {{ onSwitchProject(id): void, onProjectDeleted(id): void }} callbacks
   */
  constructor(rootEl, refreshBtn, { onSwitchProject, onProjectDeleted } = {}) {
    this._root = rootEl;
    this._onSwitchProject  = onSwitchProject  || null;
    this._onProjectDeleted = onProjectDeleted || null;

    this._userId    = null;
    this._projects  = [];
    this._expanded  = new Set();   // expanded project ids
    this._fileCache = new Map();   // projectId → file-tree array
    this._activeId  = null;

    // Multi-select state: Set of strings "projectId::path"
    this._selected = new Set();

    // Sticky batch-delete toolbar (created once, shown/hidden)
    this._toolbar = this._createToolbar();
    this._root.parentElement.insertBefore(this._toolbar, this._root);

    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => this._refresh());
    }
  }

  // ── Public API ──────────────────────────────────────────────────────────────

  async loadUser(userId) {
    this._userId = userId;
    await this._fetchProjects();
    this._render();
  }

  setActiveProject(projectId) {
    this._activeId = projectId;
    if (!this._expanded.has(projectId)) {
      this._expanded.add(projectId);
      this._fetchFiles(projectId).then(() => this._render());
    } else {
      this._render();
    }
  }

  async refreshProject(projectId) {
    await this._fetchFiles(projectId);
    this._render();
  }

  async reload(userId) {
    if (userId != null) this._userId = userId;
    await this._fetchProjects();
    this._render();
  }

  // ── Toolbar ────────────────────────────────────────────────────────────────

  _createToolbar() {
    const bar = document.createElement('div');
    bar.id = 'fm-batch-bar';
    bar.className = 'fm-batch-bar hidden';
    bar.innerHTML = `
      <span class="fm-batch-count"></span>
      <button class="fm-batch-del-btn">🗑 删除所选</button>
      <button class="fm-batch-cancel-btn">取消</button>`;

    bar.querySelector('.fm-batch-del-btn').addEventListener('click', () => this._deleteSelected());
    bar.querySelector('.fm-batch-cancel-btn').addEventListener('click', () => {
      this._selected.clear();
      this._updateToolbar();
      this._render();
    });
    return bar;
  }

  _updateToolbar() {
    const n = this._selected.size;
    if (n === 0) {
      this._toolbar.classList.add('hidden');
    } else {
      this._toolbar.classList.remove('hidden');
      this._toolbar.querySelector('.fm-batch-count').textContent = `已选 ${n} 项`;
    }
  }

  // ── Internal data fetching ─────────────────────────────────────────────────

  async _refresh() {
    if (this._userId == null) return;
    await this._fetchProjects();
    await Promise.all([...this._expanded].map(id => this._fetchFiles(id)));
    this._render();
  }

  async _fetchProjects() {
    if (this._userId == null) return;
    try {
      const res = await fetch(`/api/users/${this._userId}/projects`);
      const data = await res.json();
      this._projects = data.projects || [];
    } catch (e) {
      console.warn('[FileManager] fetchProjects:', e.message);
    }
  }

  async _fetchFiles(projectId) {
    try {
      const res = await fetch(`/api/projects/${projectId}/files`);
      const data = await res.json();
      this._fileCache.set(projectId, data.files || []);
    } catch (e) {
      console.warn('[FileManager] fetchFiles:', e.message);
    }
  }

  // ── Delete actions ─────────────────────────────────────────────────────────

  async _deleteProject(projectId) {
    if (!confirm('确定删除该项目及其所有文件？此操作不可撤销。')) return;
    try {
      await fetch(`/api/projects/${projectId}`, { method: 'DELETE' });
    } catch (e) {
      console.warn('[FileManager] deleteProject:', e.message);
    }
    this._projects = this._projects.filter(p => p.id !== projectId);
    this._fileCache.delete(projectId);
    this._expanded.delete(projectId);
    // Remove any selections from this project.
    for (const key of [...this._selected]) {
      if (key.startsWith(`${projectId}::`)) this._selected.delete(key);
    }
    this._updateToolbar();
    this._render();
    if (this._onProjectDeleted) this._onProjectDeleted(projectId);
  }

  async _deleteFile(projectId, filePath) {
    const name = filePath.split('/').pop();
    if (!confirm(`确定删除「${name}」？`)) return;
    try {
      await fetch(
        `/api/projects/${projectId}/files?path=${encodeURIComponent(filePath)}`,
        { method: 'DELETE' }
      );
    } catch (e) {
      console.warn('[FileManager] deleteFile:', e.message);
    }
    this._selected.delete(`${projectId}::${filePath}`);
    this._updateToolbar();
    await this._fetchFiles(projectId);
    this._render();
  }

  async _deleteSelected() {
    if (this._selected.size === 0) return;
    if (!confirm(`确定删除选中的 ${this._selected.size} 个文件？`)) return;

    // Group by project for efficient re-fetch.
    const byProject = new Map();
    for (const key of this._selected) {
      const [pid, ...rest] = key.split('::');
      const path = rest.join('::');
      if (!byProject.has(pid)) byProject.set(pid, []);
      byProject.get(pid).push(path);
    }

    // Delete in parallel.
    await Promise.all(
      [...byProject.entries()].flatMap(([pid, paths]) =>
        paths.map(path =>
          fetch(`/api/projects/${pid}/files?path=${encodeURIComponent(path)}`,
                { method: 'DELETE' }).catch(e => console.warn('[FileManager]', e.message))
        )
      )
    );

    this._selected.clear();
    this._updateToolbar();

    // Refresh affected projects.
    await Promise.all([...byProject.keys()].map(pid => this._fetchFiles(Number(pid))));
    this._render();
  }

  // ── Rendering ─────────────────────────────────────────────────────────────

  _render() {
    this._root.innerHTML = '';

    if (this._projects.length === 0) {
      const el = document.createElement('div');
      el.className = 'fm-empty';
      el.textContent = '暂无项目';
      this._root.appendChild(el);
      return;
    }

    for (const project of this._projects) {
      this._root.appendChild(this._buildProjectEl(project));
    }
  }

  _buildProjectEl(project) {
    const wrap = document.createElement('div');
    wrap.className = 'fm-project';

    const isActive   = project.id === this._activeId;
    const isExpanded = this._expanded.has(project.id);

    // ── Project row ──────────────────────────────────────────────────────────
    const row = document.createElement('div');
    row.className = `fm-project-row${isActive ? ' fm-active' : ''}`;

    const chevron = document.createElement('span');
    chevron.className = 'fm-chevron';
    chevron.textContent = isExpanded ? '▼' : '▶';

    const label = document.createElement('span');
    label.className = 'fm-project-label';
    label.textContent = project.name;
    label.title = project.name;

    const del = document.createElement('button');
    del.className = 'fm-del-btn';
    del.textContent = '🗑';
    del.title = '删除项目';
    del.addEventListener('click', e => { e.stopPropagation(); this._deleteProject(project.id); });

    row.appendChild(chevron);
    row.appendChild(label);
    row.appendChild(del);

    row.addEventListener('click', () => {
      if (this._expanded.has(project.id)) {
        this._expanded.delete(project.id);
      } else {
        this._expanded.add(project.id);
        if (!this._fileCache.has(project.id)) {
          this._fetchFiles(project.id).then(() => this._render());
        }
      }
      if (project.id !== this._activeId && this._onSwitchProject) {
        this._onSwitchProject(project.id);
      }
      this._render();
    });

    wrap.appendChild(row);

    // ── File tree ────────────────────────────────────────────────────────────
    if (isExpanded) {
      const treeEl = document.createElement('div');
      treeEl.className = 'fm-tree';

      const files = this._fileCache.get(project.id);
      if (!files) {
        const msg = document.createElement('div');
        msg.className = 'fm-tree-empty';
        msg.textContent = '加载中…';
        treeEl.appendChild(msg);
      } else if (files.length === 0) {
        const msg = document.createElement('div');
        msg.className = 'fm-tree-empty';
        msg.textContent = '暂无文件';
        treeEl.appendChild(msg);
      } else {
        this._buildNodes(treeEl, files, project.id, 1);
      }

      wrap.appendChild(treeEl);
    }

    return wrap;
  }

  _buildNodes(container, nodes, projectId, depth) {
    for (const node of nodes) {
      if (node.type === 'dir') {
        const row = document.createElement('div');
        row.className = 'fm-dir-row';
        row.style.paddingLeft = `${8 + depth * 12}px`;
        row.textContent = `📁 ${node.name}`;
        container.appendChild(row);
        if (node.children && node.children.length > 0) {
          this._buildNodes(container, node.children, projectId, depth + 1);
        }
      } else {
        container.appendChild(this._buildFileRow(node, projectId, depth));
      }
    }
  }

  _buildFileRow(node, projectId, depth) {
    const selKey = `${projectId}::${node.path}`;
    const isSelected = this._selected.has(selKey);

    const row = document.createElement('div');
    row.className = `fm-file-row${isSelected ? ' fm-selected' : ''}`;
    row.style.paddingLeft = `${8 + depth * 12}px`;
    row.draggable = true;

    // Checkbox
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'fm-checkbox';
    cb.checked = isSelected;
    cb.addEventListener('change', e => {
      e.stopPropagation();
      if (cb.checked) {
        this._selected.add(selKey);
      } else {
        this._selected.delete(selKey);
      }
      row.classList.toggle('fm-selected', cb.checked);
      this._updateToolbar();
    });

    // File name
    const nameEl = document.createElement('span');
    nameEl.className = 'fm-file-name';
    nameEl.textContent = `${this._fileIcon(node.ext)} ${node.name}`;
    nameEl.title = node.path;

    // Individual delete button (visible on hover)
    const del = document.createElement('button');
    del.className = 'fm-del-btn fm-del-file';
    del.textContent = '×';
    del.title = '删除';
    del.addEventListener('click', e => {
      e.stopPropagation();
      this._deleteFile(projectId, node.path);
    });

    row.appendChild(cb);
    row.appendChild(nameEl);
    row.appendChild(del);

    // Drag-and-drop payload for text preview
    const dragPayload = JSON.stringify({
      projectId,
      path: node.path,
      name: node.name,
      ext:  node.ext,
    });
    row.addEventListener('dragstart', e => {
      e.dataTransfer.effectAllowed = 'copy';
      e.dataTransfer.setData('application/x-cad-file', dragPayload);
      row.classList.add('fm-dragging');
    });
    row.addEventListener('dragend', () => row.classList.remove('fm-dragging'));

    return row;
  }

  _fileIcon(ext) {
    const MAP = {
      '.py':   '🐍',
      '.step': '📐', '.stp': '📐',
      '.stl':  '🧊', '.obj': '🧊',
      '.msh':  '🕸',
      '.inp':  '📋',
      '.log':  '📃',
      '.txt':  '📄',
      '.json': '{}',
      '.yaml': '📝', '.yml': '📝',
    };
    return MAP[ext] || '📄';
  }
}
