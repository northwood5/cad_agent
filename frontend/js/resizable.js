/**
 * resizable.js — drag-handle resize for the three panel boundaries.
 *
 * Handles:
 *   #resize-sidebar  ←→  sidebar | viewer
 *   #resize-chat     ←→  viewer  | chat
 *   #resize-trace    ↕   work-area | trace
 *
 * Sizes are persisted to localStorage so they survive page reloads.
 */

const LS_SIDEBAR = 'panel-sidebar-w';
const LS_CHAT    = 'panel-chat-w';
const LS_TRACE   = 'panel-trace-h';

export function initResizable() {
  const sidebar = document.getElementById('file-sidebar');
  const chat    = document.getElementById('chat-panel');
  const trace   = document.getElementById('trace-panel');

  // Restore saved sizes
  const savedSidebar = localStorage.getItem(LS_SIDEBAR);
  const savedChat    = localStorage.getItem(LS_CHAT);
  const savedTrace   = localStorage.getItem(LS_TRACE);
  if (savedSidebar && sidebar) sidebar.style.width  = savedSidebar;
  if (savedChat    && chat)    chat.style.width     = savedChat;
  if (savedTrace   && trace)   trace.style.height   = savedTrace;

  // sidebar ↔ viewer  (drag right → widen sidebar)
  setupHDrag('resize-sidebar', dx => {
    if (!sidebar) return;
    const w = clamp(sidebar.offsetWidth + dx, 120, 520);
    sidebar.style.width = w + 'px';
    localStorage.setItem(LS_SIDEBAR, w + 'px');
  });

  // viewer ↔ chat  (drag right → shrink chat)
  setupHDrag('resize-chat', dx => {
    if (!chat) return;
    const w = clamp(chat.offsetWidth - dx, 240, 700);
    chat.style.width = w + 'px';
    localStorage.setItem(LS_CHAT, w + 'px');
  });

  // work-area ↔ trace  (drag down → shrink trace)
  setupVDrag('resize-trace', dy => {
    if (!trace) return;
    const h = clamp(trace.offsetHeight - dy, 50, 520);
    trace.style.height = h + 'px';
    localStorage.setItem(LS_TRACE, h + 'px');
  });
}

function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v));
}

function setupHDrag(id, onDelta) {
  const handle = document.getElementById(id);
  if (!handle) return;

  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    let lastX = e.clientX;
    handle.classList.add('dragging');
    document.body.style.cursor = 'ew-resize';
    document.body.style.userSelect = 'none';

    const onMove = e => {
      const dx = e.clientX - lastX;
      lastX = e.clientX;
      onDelta(dx);
    };
    const onUp = () => {
      handle.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

function setupVDrag(id, onDelta) {
  const handle = document.getElementById(id);
  if (!handle) return;

  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    let lastY = e.clientY;
    handle.classList.add('dragging');
    document.body.style.cursor = 'ns-resize';
    document.body.style.userSelect = 'none';

    const onMove = e => {
      const dy = e.clientY - lastY;
      lastY = e.clientY;
      onDelta(dy);
    };
    const onUp = () => {
      handle.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}
