/**
 * text_preview.js — Tab 4 file preview pane.
 *
 * Accepts files dragged from the file sidebar (application/x-cad-file MIME)
 * and fetches their content from GET /api/projects/{id}/file-content?path=…
 *
 * Supported encodings returned by the backend:
 *   text   → <pre> monospace display
 *   image  → <img> element (PNG/JPG/GIF/SVG/WebP)
 *   html   → sandboxed <iframe srcdoc>
 *   binary → "cannot preview" notice
 */
export class TextPreview {
  /**
   * @param {HTMLElement} dropZoneEl  - #preview-drop-zone
   * @param {HTMLElement} emptyEl     - #preview-empty placeholder
   */
  constructor(dropZoneEl, emptyEl) {
    this._zone = dropZoneEl;
    this._empty = emptyEl;
    this._setupDrop();
  }

  // ── Public API ──────────────────────────────────────────────────────────────

  /** Load a file directly (e.g. when dropped on the tab button). */
  async loadFile(fileInfo) {
    await this._load(fileInfo);
  }

  // ── Drop-zone ────────────────────────────────────────────────────────────────

  _setupDrop() {
    this._zone.addEventListener('dragover', e => {
      const hasCadFile = e.dataTransfer.types.includes('application/x-cad-file');
      if (!hasCadFile) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
      this._zone.classList.add('drag-over');
    });

    this._zone.addEventListener('dragleave', e => {
      if (!this._zone.contains(e.relatedTarget)) {
        this._zone.classList.remove('drag-over');
      }
    });

    this._zone.addEventListener('drop', async e => {
      e.preventDefault();
      this._zone.classList.remove('drag-over');
      const raw = e.dataTransfer.getData('application/x-cad-file');
      if (!raw) return;
      try {
        await this._load(JSON.parse(raw));
      } catch (err) {
        console.warn('[TextPreview] drop error:', err);
      }
    });
  }

  // ── Loading ──────────────────────────────────────────────────────────────────

  async _load({ projectId, path, name, ext }) {
    this._showLoading();
    try {
      const url = `/api/projects/${projectId}/file-content?path=${encodeURIComponent(path)}`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      switch (data.encoding) {
        case 'image':
          this._showImage(name, path, data.url);
          break;
        case 'html':
          this._showHtml(name, path, data.content || '');
          break;
        case 'text':
          this._showText(name, path, data.content || '');
          break;
        default:
          this._showBinary(name, path);
      }
    } catch (err) {
      this._showError(name, err.message);
    }
  }

  // ── Rendering helpers ─────────────────────────────────────────────────────────

  _showLoading() {
    this._clearContent();
    const el = document.createElement('div');
    el.className = 'preview-loading';
    el.textContent = '加载中…';
    this._zone.appendChild(el);
  }

  _showImage(name, path, imageUrl) {
    this._clearContent();
    const header = this._makeHeader(name, path);
    const wrap = document.createElement('div');
    wrap.className = 'preview-image-wrap';
    const img = document.createElement('img');
    img.className = 'preview-image';
    img.src = imageUrl;
    img.alt = name;
    img.onerror = () => {
      wrap.innerHTML = '';
      const msg = document.createElement('div');
      msg.className = 'preview-binary-msg';
      msg.textContent = `⚠ 图片加载失败：${name}`;
      wrap.appendChild(msg);
    };
    wrap.appendChild(img);
    this._zone.appendChild(header);
    this._zone.appendChild(wrap);
  }

  _showHtml(name, path, content) {
    this._clearContent();
    const header = this._makeHeader(name, path);
    const iframe = document.createElement('iframe');
    iframe.className = 'preview-iframe';
    iframe.sandbox = 'allow-scripts allow-same-origin';
    iframe.srcdoc = content;
    this._zone.appendChild(header);
    this._zone.appendChild(iframe);
  }

  _showText(name, path, content) {
    this._clearContent();
    const header = this._makeHeader(name, path);
    const pre = document.createElement('pre');
    pre.className = 'preview-content';
    pre.textContent = content;
    this._zone.appendChild(header);
    this._zone.appendChild(pre);
  }

  _showBinary(name, path) {
    this._clearContent();
    const header = this._makeHeader(name, path);
    const msg = document.createElement('div');
    msg.className = 'preview-binary-msg';
    msg.textContent = '⚠ 二进制文件，无法以文本形式预览';
    this._zone.appendChild(header);
    this._zone.appendChild(msg);
  }

  _showError(name, msg) {
    this._clearContent();
    const el = document.createElement('div');
    el.className = 'preview-binary-msg';
    el.textContent = `加载失败 (${name})：${msg}`;
    this._zone.appendChild(el);
  }

  _makeHeader(name, path) {
    const header = document.createElement('div');
    header.className = 'preview-file-header';
    const nameEl = document.createElement('span');
    nameEl.className = 'preview-filename';
    nameEl.textContent = name;
    const pathEl = document.createElement('span');
    pathEl.className = 'preview-filepath';
    pathEl.textContent = path;
    header.appendChild(nameEl);
    header.appendChild(pathEl);
    return header;
  }

  _clearContent() {
    [...this._zone.children].forEach(child => {
      if (child !== this._empty) child.remove();
    });
    if (this._empty) this._empty.style.display = 'none';
  }
}
