/**
 * chat.js  —  Chat panel: message history + streaming agent reply.
 * Exposes: ChatPanel class
 */

export class ChatPanel {
  constructor(messagesEl) {
    this._msgs = messagesEl;
    this._streamBubble = null;
    this._streamText = '';
  }

  _bubble(role, text) {
    const wrap = document.createElement('div');
    wrap.className = `msg ${role}`;

    const label = document.createElement('div');
    label.className = 'msg-label';
    label.textContent = role === 'user' ? 'You' : 'CAD Agent';

    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    bubble.textContent = text;

    wrap.appendChild(label);
    wrap.appendChild(bubble);
    this._msgs.appendChild(wrap);
    this._msgs.scrollTop = this._msgs.scrollHeight;
    return { wrap, bubble };
  }

  addUser(text) {
    this._bubble('user', text);
  }

  startAgentStream() {
    this._streamText = '';
    const { bubble } = this._bubble('agent', '');
    bubble.classList.add('streaming');
    this._streamBubble = bubble;
  }

  appendAgentText(text) {
    if (!this._streamBubble) return;
    this._streamText += text;
    this._streamBubble.textContent = this._streamText;
    this._msgs.scrollTop = this._msgs.scrollHeight;
  }

  finaliseAgentStream() {
    if (!this._streamBubble) return;
    this._streamBubble.classList.remove('streaming');
    this._streamBubble = null;
  }

  addSystem(text, type = 'info') {
    const el = document.createElement('div');
    el.style.cssText =
      'font-size:12px;color:var(--text-muted);text-align:center;padding:4px 0;';
    el.textContent = text;
    this._msgs.appendChild(el);
    this._msgs.scrollTop = this._msgs.scrollHeight;
  }

  clear() {
    this._msgs.innerHTML = '';
    this._streamBubble = null;
    this._streamText = '';
    this.addSystem('新会话已开始');
  }
}
