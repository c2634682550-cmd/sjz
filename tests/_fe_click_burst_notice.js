/* 无头 DOM 垫片：执行 web/index.html 里真实的 CLICK-BURST-NOTICE 脚本，
   验证「连点与补刀互斥」提示显示在 #blk-fire-burst 下方，保存回包里的
   warnings 也能直接显示。只实现脚本用到的那几个 DOM 方法。

   用法：node _fe_click_burst_notice.js <项目根>
*/

const fs = require('fs');
const path = require('path');

const rootDir = process.argv[2];
const html = fs.readFileSync(path.join(rootDir, 'web', 'index.html'), 'utf8');
const m = html.match(/<!-- CLICK-BURST-NOTICE BEGIN -->\s*<script>([\s\S]*?)<\/script>/);
if (!m) {
  console.error('CLICK-BURST-NOTICE 脚本块没找到');
  process.exit(2);
}
const code = m[1];

const registry = [];
const listeners = [];

class ClassList {
  constructor(el) { this.el = el; this.set = new Set(); }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  contains(c) { return this.set.has(c); }
}

class El {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.style = {};
    this.attrs = {};
    this.id = '';
    this.value = '';
    this.textContent = '';
    this.classList = new ClassList(this);
    registry.push(this);
  }
  appendChild(c) {
    if (c.parentElement) {
      const i = c.parentElement.children.indexOf(c);
      if (i >= 0) c.parentElement.children.splice(i, 1);
    }
    c.parentElement = this;
    this.children.push(c);
    return c;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) {
    if (k === 'aria-checked' || k === 'aria-pressed') {
      return this.classList.contains('on') ? 'true' : null;
    }
    return this.attrs[k] == null ? null : this.attrs[k];
  }
  matchesDataSw(name) { return this.dataset.sw === name; }
  querySelector(sel) {
    const mm = sel.match(/^\[data-sw="(.+)"\]$/);
    if (!mm) return null;
    const want = mm[1];
    let hit = null;
    const walk = (n) => {
      for (const ch of n.children) {
        if (hit) return;
        if (ch.matchesDataSw(want)) { hit = ch; return; }
        walk(ch);
      }
    };
    walk(this);
    return hit;
  }
}

function findById(id) {
  return registry.find((e) => e.id === id) || null;
}

global.window = global;
global.document = {
  getElementById(id) { return findById(id); },
  createElement(tag) { return new El(tag); },
  querySelector(sel) {
    const mm = sel.match(/^\[data-sw="(.+)"\]$/);
    if (!mm) return null;
    return registry.find((e) => e.matchesDataSw(mm[1])) || null;
  },
  addEventListener(ev, cb) { listeners.push([ev, cb]); },
};
global.MutationObserver = class {
  constructor(cb) { this.cb = cb; }
  observe() { }
};

// ---- 构造初始界面：连点区块 + 补刀开关 ----
const box = new El('div');
box.id = 'swFire';
const sec = new El('section');
sec.id = 'blk-fire-burst';
sec.dataset.feature = 'click.burst';
box.appendChild(sec);

function row(sw, val, isSelect) {
  const r = new El('div');
  r.classList.add('setrow');
  const c = new El(isSelect ? 'select' : 'input');
  c.dataset.sw = sw;
  c.value = val;
  r.appendChild(c);
  return r;
}
const modeRow = row('click.burst.mode', 'burst', true);
sec.appendChild(modeRow);
sec.appendChild(row('click.burst.hold_ms', '100', false));
sec.appendChild(row('click.burst.interval_ms', '5', false));

const follow = new El('button');
follow.dataset.sw = 'buy.followup.enabled';
follow.classList.add('on');

// ---- 保存回包假接口：/api/switches 返回 warnings ----
global.fetch = function (url, opts) {
  const body = { ok: true, groups: [], warnings: ['连点与补刀互斥，当前按连点执行'] };
  const res = {
    ok: true,
    clone() { return res; },
    json() { return Promise.resolve(body); },
  };
  return Promise.resolve(res);
};

const tick = () => new Promise((r) => setImmediate(r));

(async () => {
  (0, eval)(code);
  await tick();
  await tick();

  const api = global.clickBurstNotice;
  const mode = modeRow.querySelector('[data-sw="click.burst.mode"]');

  api.refresh();
  let warn = findById('clickBurstWarn');
  const shownWhenConflict = warn ? warn.style.display : null;

  // 连点仍开、补刀关：不再冲突 → 隐藏
  follow.classList.remove('on');
  api.refresh();
  const hiddenWhenClear = warn ? warn.style.display : null;

  // 保存回包里带 warnings：即使控件状态还没刷新也要显示原因
  follow.classList.add('on');
  await global.fetch('/api/switches', { method: 'POST' });
  await tick();
  await tick();
  warn = findById('clickBurstWarn');
  const shownAfterSave = warn ? warn.style.display : null;
  const saveText = warn ? warn.textContent : '';

  const ok = !!warn
    && api.text.startsWith('连点与补刀互斥，当前按连点执行')
    && shownWhenConflict === 'block'
    && hiddenWhenClear === 'none'
    && shownAfterSave === 'block'
    && saveText.startsWith('连点与补刀互斥')
    && warn.parentElement === sec;

  console.log(JSON.stringify({
    ok,
    text: api.text,
    shown_when_conflict: shownWhenConflict,
    hidden_when_clear: hiddenWhenClear,
    shown_after_save: shownAfterSave,
    save_text: saveText,
    in_burst_section: !!warn && warn.parentElement === sec,
  }));
})();
