/**
 * agent_trace.js — 实时渲染 AgentScope 事件流到推理面板。
 *
 * 后端事件类型（AgentScope 2.x 字段）：
 *   thinking_start/delta/end
 *   text_start/delta/end
 *   tool_call_start  { tool, id }
 *   tool_call_delta  { delta, id }
 *   tool_call_end    { args, id }
 *   tool_result_start  { tool, id }
 *   tool_result_delta  { text, id }
 *   tool_result_end    { id, state, result }
 *   model_ready      { filename, url }
 *   error            { message }
 */

export class AgentTrace {
  constructor(logEl, dotEl) {
    this._log  = logEl;
    this._dot  = dotEl;
    // streaming span refs keyed by tool_call_id or type
    this._spans = {};
    this._thinkEl = null;
    this._textEl  = null;
  }

  // ── Public helpers ────────────────────────────────────────────────────────

  addInfo(text) {
    this._append('INFO', 'tag-info', text);
  }

  clear() {
    this._log.innerHTML = '';
    this._spans = {};
    this._thinkEl = null;
    this._textEl  = null;
  }

  setActive(on) {
    this._dot.classList.toggle('active', on);
  }

  onAgentStart() {
    this.setActive(true);
    this._spans = {};
    this._thinkEl = null;
    this._textEl  = null;
  }

  onAgentDone() {
    this.setActive(false);
    this._append('INFO', 'tag-info', '── 回复完成 ──');
  }

  // ── Event dispatcher ────────────────────────────────────────────────────

  onEvent(evt) {
    const { type } = evt;

    switch (type) {

      // ---- Thinking ----
      case 'thinking_start':
        this._thinkEl = this._append('THINK', 'tag-think', '');
        break;
      case 'thinking_delta':
        if (this._thinkEl) {
          this._thinkEl.textContent += evt.text;
          this._scroll();
        }
        break;
      case 'thinking_end':
        this._thinkEl = null;
        break;

      // ---- Text ----
      case 'text_start':
        this._textEl = this._append('TEXT', 'tag-text', '');
        break;
      case 'text_delta':
        if (this._textEl) {
          this._textEl.textContent += evt.text;
          this._scroll();
        }
        break;
      case 'text_end':
        this._textEl = null;
        break;

      // ---- Tool call ----
      case 'tool_call_start': {
        const span = this._append('TOOL▶', 'tag-tool', `${evt.tool}(`);
        this._spans[`call_${evt.id}`] = { span, tool: evt.tool, args: '' };
        break;
      }
      case 'tool_call_delta': {
        const s = this._spans[`call_${evt.id}`];
        if (s) {
          s.args += evt.delta;
          s.span.textContent = `${s.tool}(${s.args}`;
          this._scroll();
        }
        break;
      }
      case 'tool_call_end': {
        const s = this._spans[`call_${evt.id}`];
        if (s) {
          let prettyArgs = s.args || '{}';
          try { prettyArgs = JSON.stringify(JSON.parse(prettyArgs)); } catch (_) {}
          s.span.textContent = `${s.tool}  ${prettyArgs}`;
          delete this._spans[`call_${evt.id}`];
        }
        break;
      }

      // ---- Tool result ----
      case 'tool_result_start': {
        const span = this._append('RESULT', 'tag-result', '');
        this._spans[`res_${evt.id}`] = { span, buf: '' };
        break;
      }
      case 'tool_result_delta': {
        const s = this._spans[`res_${evt.id}`];
        if (s) {
          s.buf += evt.text;
          s.span.textContent = s.buf;
          this._scroll();
        }
        break;
      }
      case 'tool_result_end': {
        const s = this._spans[`res_${evt.id}`];
        const resultText = evt.result || (s ? s.buf : '');
        if (s) {
          this._renderResult(s.span, resultText, evt.state);
          delete this._spans[`res_${evt.id}`];
        }
        break;
      }

      // ---- Model ready ----
      case 'model_ready':
        this._append('MODEL', 'tag-info', `3D 文件: ${evt.filename}`);
        break;

      // ---- Error ----
      case 'error':
        this._append('ERROR', 'tag-error', evt.message);
        break;

      default:
        break;
    }
  }

  // ── Private ───────────────────────────────────────────────────────────────

  _renderResult(span, text, state) {
    try {
      const parsed = JSON.parse(text);
      if (parsed.success === false) {
        span.textContent = `ERROR: ${parsed.error}`;
        span.style.color = 'var(--red)';
      } else {
        const summary = Object.entries(parsed)
          .filter(([k]) => k !== 'success')
          .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
          .join('  ');
        span.textContent = summary || '✓';
        span.style.color = 'var(--green)';
      }
    } catch (_) {
      span.textContent = text || state || '✓';
      span.style.color = 'var(--green)';
    }
  }

  /** Append a row and return its content <span>. */
  _append(tag, tagClass, content) {
    const row = document.createElement('div');
    row.className = 'trace-item';

    const t = document.createElement('span');
    t.className = `trace-tag ${tagClass}`;
    t.textContent = tag;

    const c = document.createElement('span');
    c.className = 'trace-content';
    c.textContent = content;

    row.appendChild(t);
    row.appendChild(c);
    this._log.appendChild(row);
    this._scroll();
    return c;
  }

  _scroll() {
    this._log.scrollTop = this._log.scrollHeight;
  }
}
