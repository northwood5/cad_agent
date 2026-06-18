/**
 * tabs.js — switch between the three graphics-area tabs
 * (workflow / viewer / scripts) and show a notification dot when a
 * background tab gets new content.
 */
export class Tabs {
  constructor() {
    this._btns = [...document.querySelectorAll('.tab-btn')];
    this._panes = {
      workflow: document.getElementById('tab-workflow'),
      viewer:   document.getElementById('tab-viewer'),
      scripts:  document.getElementById('tab-scripts'),
    };
    this._current = 'workflow';
    this._onSwitch = null;

    this._btns.forEach(btn => {
      btn.addEventListener('click', () => this.show(btn.dataset.tab));
    });
  }

  /** Register a callback fired after a tab becomes active. */
  onSwitch(fn) { this._onSwitch = fn; }

  show(name) {
    if (!this._panes[name]) return;
    this._current = name;
    this._btns.forEach(b => {
      const active = b.dataset.tab === name;
      b.classList.toggle('active', active);
      if (active) this._clearDot(b);
    });
    Object.entries(this._panes).forEach(([key, el]) => {
      el.classList.toggle('active', key === name);
    });
    if (this._onSwitch) this._onSwitch(name);
  }

  get current() { return this._current; }

  /** Show a notification dot on a tab if it isn't the active one. */
  notify(name) {
    if (name === this._current) return;
    const btn = this._btns.find(b => b.dataset.tab === name);
    if (btn && !btn.querySelector('.tab-dot')) {
      const dot = document.createElement('span');
      dot.className = 'tab-dot';
      btn.appendChild(dot);
    }
  }

  _clearDot(btn) {
    const dot = btn.querySelector('.tab-dot');
    if (dot) dot.remove();
  }
}
