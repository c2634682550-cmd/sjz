"""前端冒烟：在 Node 里跑一遍页面的脚本。

为什么需要它：界面上的毛病（脚本报错、动态区域空白、按钮点了没反应）
单测抓不到 —— 只有脚本真的执行过才显形。这里把 index.html 里那段
<script> 抠出来，配一个极简 DOM 桩，喂上假接口数据跑一遍，然后检查
「必须由脚本填出来」的区域是不是真的有内容。

它不能替代真浏览器，但能挡住 90% 的「改完界面白屏 / 按钮没绑上」。
用法：python tools/probe_frontend.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "web", "index.html")

SHIM = r"""
const IDS = new Set(__IDS__);
const CALLS = [];
let FAILED = null;

function stub(id) {
  const el = {
    id: id || '', _html: '', textContent: '', className: '', value: '',
    disabled: false, checked: true, title: '', dataset: {}, style: {},
    scrollHeight: 0, scrollTop: 0, clientHeight: 0,
    classList: {
      add(){}, remove(){}, contains(){ return false; },
      toggle(){},
    },
    addEventListener(t, fn){ (this._h = this._h || {})[t] = fn; },
    removeEventListener(){},
    appendChild(c){ (this.children = this.children || []).push(c); return c; },
    querySelector(){ return stub(''); },
    querySelectorAll(){ return []; },
    closest(){ return null; },
    focus(){}, blur(){}, click(){},
    get firstChild(){ return (this._first = this._first || {textContent: ''}); },
  };
  Object.defineProperty(el, 'innerHTML', {
    get(){ return this._html; },
    set(v){ this._html = String(v); this._first = {textContent: ''}; },
  });
  return el;
}

const ELEMENTS = {};
IDS.forEach(id => { ELEMENTS[id] = stub(id); });

global.document = {
  querySelector(sel) {
    if (typeof sel === 'string' && sel[0] === '#') {
      const id = sel.slice(1);
      if (!IDS.has(id)) return null;          // 真页面里也是 null，能炸出 bug
      return ELEMENTS[id];
    }
    return stub(sel);
  },
  querySelectorAll(sel) {
    if (sel === '#tabs button') {
      return ['run', 'queue', 'records', 'settings'].map(p => {
        const b = stub('');
        b.dataset = {page: p};
        return b;
      });
    }
    return [];
  },
  createElement(){ return stub(''); },
  addEventListener(){},
  title: '',
};
global.window = global;
global.location = {search: '?frozen=1', href: 'http://127.0.0.1/'};
global.setInterval = () => 0;
global.clearInterval = () => 0;

const FIXTURES = __FIXTURES__;

global.fetch = async (path) => {
  CALLS.push(String(path));
  const key = String(path).split('?')[0].replace(/^\/api\//, '');
  const data = FIXTURES[key];
  if (data === undefined) {
    return {ok: false, status: 404, text: async () => 'not found', json: async () => ({})};
  }
  return {ok: true, status: 200, text: async () => JSON.stringify(data),
          json: async () => data};
};

process.on('uncaughtException', e => { FAILED = e; });
process.on('unhandledRejection', e => { FAILED = e; });
"""

CHECKS = r"""
(async () => {
  await new Promise(r => setTimeout(r, 200));     // 等 boot() 的 await 链跑完
  const bad = [];
  const text = el => String((el && ((el._first && el._first.textContent) ||
                                    el._html || el.textContent)) || '');

  if (FAILED) bad.push('脚本抛异常：' + (FAILED && FAILED.message));
  if (!CALLS.some(u => u.includes('/api/switches'))) bad.push('没请求开关');
  if (!CALLS.some(u => u.includes('/api/preflight'))) bad.push('没请求启动前检查');
  if (text(document.querySelector('#switchGroups')).indexOf('swgroup') < 0)
    bad.push('开关面板没渲染出来');
  if (text(document.querySelector('#preflightList')).indexOf('check') < 0)
    bad.push('环境检查没渲染出来');
  if (text(document.querySelector('#notice')).length === 0)
    bad.push('状态条没文案');
  if (String(document.querySelector('#targetName').textContent).length === 0)
    bad.push('目标名没初始化');
  if (text(document.querySelector('#logbox')).indexOf('<div>') < 0)
    bad.push('日志区没渲染');

  // 点一次「开始抢购」：后端说失败时，原因必须留在状态条上。
  // 以前这里会写进状态栏、紧接着被 poll() 覆盖回「空闲」—— 用户看到的就是
  // 「点了没反应」，这是这次要防的回归。
  const btn = document.querySelector('#btnStart');
  const handler = btn && btn._h && btn._h.click;
  if (!handler) {
    bad.push('「开始抢购」没绑事件');
  } else {
    handler();
    await new Promise(r => setTimeout(r, 300));
    const shown = text(document.querySelector('#notice'));
    if (shown.indexOf('缺少模板') < 0) {
      bad.push('启动失败的原因没显示出来，状态条是：' + shown.slice(0, 60));
    }
  }

  if (bad.length) {
    console.log('FAILED_JSON:' + JSON.stringify(bad));
    process.exit(1);
  }
  console.log('OK_JSON:' + JSON.stringify({calls: CALLS.length}));
})();
"""

FIXTURES = {
    "config": {"regions": {}, "console": {"min_price": 100, "max_price": 2000,
                                         "quota": 1, "workflow": "countdown"},
               "templates_ready": {"buy": True, "confirm": True}},
    "switches": {"ok": True, "groups": [
        {"name": "执行", "items": [
            {"key": "env.dry_run", "label": "演练模式", "desc": "不点鼠标",
             "kind": "bool", "value": True},
            {"key": "run.mode", "label": "执行模式", "desc": "",
             "kind": "select", "value": "auto",
             "options": [["auto", "自动"], ["dry", "演练"]]},
        ]},
    ]},
    "scheduler": {"running": False, "active": None, "tasks": [], "log": [
        {"t": "10:00:00", "level": "info", "msg": "待机"}],
        "engine": {"state": "idle", "running": False, "dry_run": True,
                   "stats": {"scans": 0, "hz": 0, "clicks": 0}, "rows": [],
                   "log": [{"t": "10:00:00", "level": "info", "msg": "待机"}]},
        "clock": {}},
    "skins": {"ok": True, "seasons": [
        {"season": "S1", "season_name": "第一季", "tiers": {"传说": [
            {"name": "AK-12·群星", "weapon": "AK-12", "gun": "步枪", "skin": "群星"}]}}]},
    "preflight": {"ok": True, "checks": [
        {"id": "page", "label": "游戏窗口", "ok": True, "detail": "1920×1080"},
        {"id": "ocr", "label": "OCR 识别", "ok": True, "detail": "就绪"},
        {"id": "clock", "label": "时间校准", "ok": False, "warn": True,
         "detail": "还没校过时"},
        {"id": "integrity", "label": "程序权限", "ok": True, "detail": "普通权限"},
    ]},
    "tasks": {"tasks": []},
    "timesync": {"ok": True, "offset": 0.001, "server": "ntp"},
    # 「开始抢购」走到这里：模拟「任务启动即失败」那种情况，
    # 界面上必须把原因显示出来
    "scheduler/start": {"ok": False,
                        "msg": "缺少模板：confirm（先在游戏里重新截一次再开始）"},
    "tasks/add": {"ok": True, "task": {"id": "aaaa1111"}, "tasks": []},
}


def main():
    node = shutil.which("node")
    if not node:
        print("没装 node，跳过前端冒烟")
        return 0

    html = open(HTML, encoding="utf-8").read()
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    if not m:
        print("index.html 里找不到 <script>")
        return 1
    script = m.group(1)
    ids = sorted(set(re.findall(r'id="([^"]+)"', html)))

    js = (SHIM.replace("__IDS__", json.dumps(ids))
              .replace("__FIXTURES__", json.dumps(FIXTURES, ensure_ascii=False))
          + "\n" + script + "\n" + CHECKS)

    path = os.path.join(tempfile.mkdtemp(prefix="sjz-fe-"), "probe.js")
    with open(path, "w", encoding="utf-8") as f:
        f.write(js)

    r = subprocess.run([node, path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    out = (r.stdout or "") + (r.stderr or "")

    print(f"--- 页面脚本（{len(script.splitlines())} 行）---")
    if "OK_JSON:" in out:
        info = json.loads(out.split("OK_JSON:", 1)[1].splitlines()[0])
        print(f"  OK   脚本跑通，共 {info['calls']} 次接口调用")
        print("  OK   开关 / 环境检查 / 状态条 / 日志区都渲染出来了")
        print("\n前端冒烟通过")
        return 0

    if "FAILED_JSON:" in out:
        bad = json.loads(out.split("FAILED_JSON:", 1)[1].splitlines()[0])
        for b in bad:
            print("  FAIL " + b)
    else:
        print("  FAIL 脚本没跑起来")
        print(out[-1500:])
    return 1


if __name__ == "__main__":
    sys.exit(main())
