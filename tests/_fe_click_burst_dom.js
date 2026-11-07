/* 无头 DOM 垫片：执行 web/index.html 里真实的 FEATURE-BLOCKS 分组脚本，
   验证 click.burst.* 会被归进 #blk-fire-burst，并模拟一次重新渲染复验。

   只实现脚本用到的那几个 DOM 方法，不启浏览器、不联网。
   用法：node _fe_click_burst_dom.js <项目根> <keys.json>
*/

const fs = require('fs');
const path = require('path');

const root = process.argv[2];
const keysFile = process.argv[3];
const keys = JSON.parse(fs.readFileSync(keysFile, 'utf8'));

const html = fs.readFileSync(path.join(root, 'web', 'index.html'), 'utf8');
const m = html.match(/<!-- FEATURE-BLOCKS BEGIN -->\s*<script>([\s\S]*?)<\/script>/);
if (!m) {
  console.error('FEATURE-BLOCKS 脚本块没找到');
  process.exit(2);
}
const code = m[1];

const observers = [];

class El {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.id = '';
    this.cls = '';
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
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => {
      for (const ch of n.children) {
        if (sel === '.setrow' && ch.cls === 'setrow') out.push(ch);
        walk(ch);
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) {
    if (sel === '[data-sw]') {
      for (const ch of this.children) if (ch.dataset.sw != null) return ch;
    }
    return null;
  }
  closest(sel) {
    let n = this;
    while (n) {
      if (sel.indexOf('section') === 0 && n.tag === 'section' && n.dataset.feature != null) return n;
      n = n.parentElement;
    }
    return null;
  }
}

const box = new El('div');
box.id = 'swFire';

function findById(node, id) {
  for (const ch of node.children) {
    if (ch.id === id) return ch;
    const hit = findById(ch, id);
    if (hit) return hit;
  }
  return null;
}

global.document = {
  getElementById(id) { return id === 'swFire' ? box : findById(box, id); },
  createElement(tag) { return new El(tag); },
};
global.MutationObserver = class {
  constructor(cb) { this.cb = cb; }
  observe() { observers.push(this.cb); }
};

function rowsOf(list) {
  return list.map((k) => {
    const r = new El('div');
    r.cls = 'setrow';
    const c = new El('label');
    c.dataset.sw = k;
    c.dataset.kind = 'int';
    r.appendChild(c);
    return r;
  });
}

function render(list) {
  box.children.slice().forEach((c) => { c.parentElement = null; });
  box.children = [];
  rowsOf(list).forEach((r) => box.appendChild(r));
  observers.forEach((cb) => cb());
}

const tick = () => new Promise((r) => setImmediate(r));

(async () => {
  render(keys);
  (0, eval)(code);
  await tick();
  await tick();

  const sec = findById(box, 'blk-fire-burst');
  const collected = sec
    ? sec.querySelectorAll('.setrow').map((r) => r.querySelector('[data-sw]').dataset.sw)
    : [];
  const feature = sec ? sec.dataset.feature : null;

  // 模拟 renderSettingsSwitches 把 #swFire innerHTML 换掉 → 必须自动重新归位
  render(keys);
  await tick();
  await tick();
  const sec2 = findById(box, 'blk-fire-burst');
  const afterRender = sec2
    ? sec2.querySelectorAll('.setrow').map((r) => r.querySelector('[data-sw]').dataset.sw)
    : [];

  const burstKeys = keys.filter((k) => k.startsWith('click.burst.'));
  const ok = collected.length === burstKeys.length
    && collected.every((k) => k.startsWith('click.burst.'))
    && feature === 'click.burst'
    && afterRender.length === burstKeys.length;

  console.log(JSON.stringify({ ok, burst: collected, feature, after_render: afterRender }));
})();
