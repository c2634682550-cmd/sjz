/* 抢购悬浮窗：只读宿主内存快照，1 秒一次，只改变化的文本节点 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var CACHE = {};
  var lastTs = 0;          /* 最近事件的绝对时间，用来算相对时间 */
  var lastReceipt = 0;     /* 最近一次拿到该事件的时间（宿主没给时间戳时兜底） */
  var busy = false;
  var RATE_N = 30;

  function setTxt(id, v) {
    v = (v === null || v === undefined) ? '' : String(v);
    if (CACHE[id] === v) return;
    CACHE[id] = v;
    var el = $(id);
    if (el) el.textContent = v;
  }

  function bridge() {
    return (window.pywebview && window.pywebview.api) || null;
  }

  /* 宿主容器是否存在：pywebview 已注入，或 WebView2 容器本身在。
     用来区分「等待宿主」（窗口在、接口没注册）和「未连接」（主程序没跑）。 */
  function hostPresent() {
    return !!(window.pywebview || (window.chrome && window.chrome.webview));
  }

  var tier = '';
  function setTier(t) {
    if (tier === t) return;
    tier = t;
    document.body.dataset.conn = t;
    var btn = $('stop');
    if (btn) btn.disabled = (t !== 'connected');
  }
  function markConnected() {
    setTier('connected');
    setTxt('conn', '已连接');
    var el = $('conn');
    if (el) el.classList.remove('off');
  }
  function notReady(target, last, connText) {
    setTier(hostPresent() ? 'waiting' : 'offline');
    var el = $('conn');
    if (el) el.classList.add('off');
    setTxt('conn', connText);
    var badge = $('badge');
    if (badge) { badge.dataset.s = 'idle'; badge.setAttribute('aria-label', '状态：未就绪'); }
    setTxt('stateTxt', '未就绪');
    setTxt('target', target);
    setTxt('price', '—');
    setTxt('scanned', '—');
    setTxt('matched', '—');
    setTxt('fired', '—');
    setTxt('hotkey', '—');
    setTxt('license', '授权 · 未就绪');
    setTxt('last', last);
    setTxt('ago', '—');
    setTxt('taskIdx', '—');
    setTxt('matchMs', '—');
    setTxt('nextWin', '—');
    paintEvents([]);
    paintRate([]);
  }
  function markWaiting() { notReady('等待宿主…', '正在等待宿主就绪', '正在等待宿主就绪'); }
  function markOffline() { notReady('未连接宿主', '主程序未运行', '未连接 · 主程序未运行'); }

  /* 状态归一：宿主可能给英文 key、也可能给中文；最后统一成 key，
     显示时再取中文标签（之前直接把标签又当 key 查了一次，界面上出现 "firing"/"idle" 这种英文）。 */
  var LABEL = { idle: '待命', scanning: '搜索中', firing: '抢购中', paused: '已暂停', error: '异常' };
  var ALIAS = {
    '待命': 'idle', '空闲': 'idle',
    '扫描': 'scanning', '搜索': 'scanning', '搜索中': 'scanning', '监控': 'scanning',
    '开火': 'firing', '抢购': 'firing', '抢购中': 'firing',
    '暂停': 'paused', '已暂停': 'paused',
    '异常': 'error', '错误': 'error'
  };
  function normState(v) {
    if (typeof v !== 'string') return 'idle';
    if (LABEL[v]) return v;
    var k = ALIAS[v];
    return (k && LABEL[k]) ? k : 'idle';
  }

  function num(v) {
    var n = Number(v);
    return isFinite(n) ? n : 0;
  }

  function money(v) {
    if (v === null || v === undefined || v === '') return '—';
    if (typeof v === 'number' || (typeof v === 'string' && /^\d+(\.\d+)?$/.test(v))) {
      return '¥' + (Math.round(Number(v) * 100) / 100).toLocaleString('zh-CN');
    }
    return String(v);
  }

  function pick(obj, keys) {
    if (!obj || typeof obj !== 'object') return undefined;
    for (var i = 0; i < keys.length; i++) {
      if (obj[keys[i]] !== undefined && obj[keys[i]] !== null) return obj[keys[i]];
    }
    return undefined;
  }

  function relTime(ts, receipt) {
    var base = ts ? (ts > 1e12 ? ts : ts * 1000) : receipt;
    if (!base) return '—';
    var d = Math.max(0, Math.floor((Date.now() - base) / 1000));
    if (d < 2) return '刚刚';
    if (d < 60) return d + ' 秒前';
    if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
    if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
    return Math.floor(d / 86400) + ' 天前';
  }

  function dur(s) {
    s = Math.max(0, Math.round(Number(s) || 0));
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60), r = s % 60;
    return m + ':' + (r < 10 ? '0' + r : r);
  }

  /* 最近 3 条事件：宿主给 events（倒序）就用它，没给就退回 last_event 一条 */
  function paintEvents(list) {
    var box = $('events');
    if (!box) return;
    list = Array.isArray(list) ? list.slice(0, 3) : [];
    var sig = list.map(function (e) {
      return (e && e.ts ? e.ts : '') + '|' + (e && e.text ? e.text : '');
    }).join('#');
    if (CACHE.eventsSig === sig) return;
    CACHE.eventsSig = sig;
    box.innerHTML = '';
    for (var i = 0; i < list.length; i++) {
      var e = list[i] || {};
      var li = document.createElement('li');
      if (e.level) li.setAttribute('data-lv', String(e.level));
      var t = document.createElement('span');
      t.className = 'etxt';
      t.textContent = e.text ? String(e.text) : '—';
      var a = document.createElement('span');
      a.className = 'eago';
      a.textContent = relTime(e.ts);
      li.appendChild(t);
      li.appendChild(a);
      box.appendChild(li);
    }
  }

  /* 迷你柱：静态渲染，只改高度（不跑动画、不加 transition） */
  function paintRate(arr) {
    var box = $('rate');
    if (!box) return;
    if (box.children.length !== RATE_N) {
      box.innerHTML = '';
      for (var k = 0; k < RATE_N; k++) box.appendChild(document.createElement('span'));
    }
    var a = (Array.isArray(arr) && arr.length) ? arr : [];
    var sig = a.join(',');
    if (CACHE.rateSig === sig) return;
    CACHE.rateSig = sig;
    var max = 1;
    for (var i = 0; i < a.length; i++) {
      var v0 = num(a[i]);
      if (v0 > max) max = v0;
    }
    for (var j = 0; j < RATE_N; j++) {
      var v = num(a[j]);
      var bar = box.children[j];
      var h = v > 0 ? Math.max(2, Math.round(v / max * 22)) : 1;
      bar.style.height = h + 'px';
      bar.setAttribute('data-z', v > 0 ? '0' : '1');
      bar.title = v + '/s';
    }
  }

  function paint(r) {
    markConnected();
    r = r && typeof r === 'object' ? r : {};
    var st = normState(pick(r, ['state', 'status', 'phase']));
    var badge = $('badge');
    if (badge) {
      badge.dataset.s = st;
      badge.setAttribute('aria-label', '状态：' + LABEL[st]);
    }
    setTxt('stateTxt', LABEL[st]);

    var hk = pick(r, ['hotkey', 'hotkey_spec', 'stop_hotkey']);
    setTxt('hotkey', hk || '—');

    var target = pick(r, ['target', 'target_name', 'item', 'item_name', 'sku_name', 'name']);
    setTxt('target', target || (st === 'idle' ? '等待目标' : '识别中…'));
    setTxt('price', money(pick(r, ['price', 'target_price', 'expect_price'])));

    var stats = r.stats || r.counters || r.counts || {};
    setTxt('scanned', num(pick(r, ['scanned', 'scan_count']) !== undefined ? pick(r, ['scanned', 'scan_count']) : pick(stats, ['scanned', 'scan'])));
    setTxt('matched', num(pick(r, ['matched', 'match_count']) !== undefined ? pick(r, ['matched', 'match_count']) : pick(stats, ['matched', 'match'])));
    setTxt('fired', num(pick(r, ['fired', 'fired_count', 'bought']) !== undefined ? pick(r, ['fired', 'fired_count', 'bought']) : pick(stats, ['fired', 'buy'])));

    /* 事件：优先 events[]，退回单条 last_event */
    var evs = Array.isArray(r.events) ? r.events.filter(function (x) { return x && (x.text || x.msg); }) : [];
    var ev, evTs;
    if (evs.length) {
      ev = evs[0];
      evTs = ev.ts;
    } else {
      ev = r.last_event !== undefined ? r.last_event : (r.event !== undefined ? r.event : r.last);
      evTs = (ev && typeof ev === 'object') ? pick(ev, ['ts', 'at', 'time', 'timestamp']) : undefined;
      if (evTs === undefined) evTs = pick(r, ['last_event_ts', 'last_event_at']);
    }
    var evText = typeof ev === 'string' ? ev : (ev && typeof ev === 'object'
      ? pick(ev, ['text', 'msg', 'message', 'desc']) : undefined);
    if (evText) {
      if (CACHE.last !== String(evText)) lastReceipt = Date.now();
      setTxt('last', evText);
      if (evTs !== undefined && Number(evTs)) lastTs = Number(evTs);
    } else {
      setTxt('last', st === 'error' ? '宿主报告异常' : '等待事件');
      lastTs = 0;
      lastReceipt = 0;
    }
    setTxt('ago', lastTs || lastReceipt ? relTime(lastTs, lastReceipt) : '—');

    paintEvents(evs);
    paintRate(r.scan_rate);

    /* 展开态：任务序号 / 平均匹配耗时 / 下次窗口或重试倒计时 */
    var ti = num(r.task_index), tt = num(r.task_total);
    setTxt('taskIdx', (tt > 1 && ti > 0) ? (ti + '/' + tt) : '—');
    var ms = r.avg_match_ms;
    setTxt('matchMs', (ms === null || ms === undefined || !isFinite(Number(ms))) ? '—' : Math.round(Number(ms)) + ' ms');
    var nr = r.next_retry_s, nw = r.next_window_s;
    if (nr !== null && nr !== undefined && isFinite(Number(nr)) && Number(nr) > 0) {
      setTxt('nextWin', '重试 ' + dur(nr));
    } else if (nw !== null && nw !== undefined && isFinite(Number(nw)) && Number(nw) > 0) {
      setTxt('nextWin', '开抢 ' + dur(nw));
    } else {
      setTxt('nextWin', '—');
    }

    var days = pick(r, ['license_days', 'license_remaining_days', 'days_left']);
    if (days === undefined && r.license && typeof r.license === 'object') {
      days = pick(r.license, ['days', 'remaining_days', 'days_left']);
    }
    if (typeof days === 'number' && isFinite(days)) {
      setTxt('license', days > 0 ? '授权 ' + days + ' 天' : '授权已到期');
    } else {
      setTxt('license', days ? ('授权 ' + days) : '授权 —');
    }
  }

  /* ---------- 展开 / 收起（点标题栏或箭头；状态记 localStorage） ---------- */
  var EXP_KEY = 'sjz.overlay.expanded';

  function isExpanded() {
    return document.body.classList.contains('expanded');
  }

  function applyExpanded(on, persist) {
    on = !!on;
    document.body.classList.toggle('expanded', on);
    var more = $('more');
    if (more) more.hidden = !on;
    var col = $('collapse');
    if (col) {
      col.setAttribute('aria-expanded', on ? 'true' : 'false');
      col.setAttribute('aria-label', on ? '收起详情' : '展开详情');
    }
    var ic = $('collapseIcon');
    if (ic) ic.setAttribute('d', on ? 'M4 10l4-4 4 4' : 'M4 6l4 4 4-4');
    /* 窗口高度由宿主改：JS 改不了 WebView 的窗口尺寸 */
    var api = bridge();
    if (api && typeof api.overlay_size === 'function') {
      try { Promise.resolve(api.overlay_size(on)).catch(function () {}); } catch (e) { /* 老宿主没有就算了 */ }
    }
    if (persist) {
      try { localStorage.setItem(EXP_KEY, on ? '1' : '0'); } catch (e) { /* 隐私模式 */ }
    }
  }

  var bar = $('bar');
  if (bar) bar.addEventListener('click', function (e) {
    if (e.target && e.target.closest && e.target.closest('button')) return;
    applyExpanded(!isExpanded(), true);
  });

  var colBtn = $('collapse');
  if (colBtn) colBtn.addEventListener('click', function (e) {
    if (e) e.stopPropagation();
    applyExpanded(!isExpanded(), true);
  });

  /* 双击标题栏打开主窗：宿主没有对应 API，先不做（不造假）。
     等宿主补上 show_main 之类的接口再加这一支。 */

  var retryAt = 0;      /* 退避：没过这个时间点就不发 IPC */
  var retryTimer = 0;   /* 退避用独立定时器精确到点，别等 1s 节拍（实测会漂成 3/5/8） */
  var backoff = 1000;
  var hadApi = false;

  function clearRetry() {
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = 0; }
  }

  function tick() {
    var api = bridge();
    var hasApi = !!(api && typeof api.get_overlay_state === 'function');
    if (!hasApi) {
      hadApi = false;
      retryAt = 0;
      backoff = 1000;
      clearRetry();
      if (hostPresent()) markWaiting(); else markOffline();
      return;
    }
    /* 接口刚刚注册：立刻试一次，不等退避窗口 */
    if (!hadApi) { hadApi = true; retryAt = 0; backoff = 1000; clearRetry(); }
    if (Date.now() < retryAt) return;
    if (busy) return;
    busy = true;
    Promise.resolve(api.get_overlay_state()).then(function (r) {
      paint(r);
      retryAt = 0;
      backoff = 1000;
      clearRetry();
    }).catch(function () {
      if (hostPresent()) markWaiting(); else markOffline();
      /* 先按当前档位等（1→2→4→8），再翻倍；到 8s 封顶后维持 8s */
      var wait = backoff;
      retryAt = Date.now() + wait;
      if (!retryTimer) {
        retryTimer = setTimeout(function () {
          retryTimer = 0;
          retryAt = 0;
          tick();
        }, wait);
      }
      backoff = Math.min(backoff * 2, 8000);
    }).then(function () {
      busy = false;
    });
  }

  /* 1 秒一次驱动；失败时按 1→2→4→8 秒退避，只有到点才真正调宿主 */
  function boot() {
    var saved = false;
    try { saved = localStorage.getItem(EXP_KEY) === '1'; } catch (e) { /* 隐私模式 */ }
    applyExpanded(saved, false);
    tick();
  }

  setInterval(tick, 1000);
  boot();
  document.addEventListener('pywebviewready', function () {
    hadApi = false;
    retryAt = 0;
    backoff = 1000;
    /* 宿主就绪后补一次尺寸同步（页面可能在接口注册前就已经恢复过展开态） */
    applyExpanded(isExpanded(), false);
    tick();
  });

  /* 停止：与 F8 走同一条 purchase_stop，不在悬浮窗里另开逻辑 */
  var stopBtn = $('stop');
  if (stopBtn) stopBtn.addEventListener('click', function (e) {
    if (e) e.stopPropagation();
    var api = bridge();
    if (!api || typeof api.purchase_stop !== 'function') {
      if (hostPresent()) markWaiting(); else markOffline();
      return;
    }
    stopBtn.disabled = true;
    Promise.resolve(api.purchase_stop()).then(function (r) {
      if (r === false) {
        setTxt('last', '停止没有成功：宿主返回失败');
      } else {
        setTxt('last', '已发送停止');
        lastReceipt = Date.now();
        lastTs = 0;
        setTxt('ago', '刚刚');
        var badge = $('badge');
        if (badge) badge.dataset.s = 'paused';
        setTxt('stateTxt', '已暂停');
      }
    }).catch(function (e2) {
      setTxt('last', '停止失败：' + (e2 && e2.message ? e2.message : String(e2)));
    }).then(function () {
      setTimeout(function () { stopBtn.disabled = false; }, 1200);
    });
  });
})();
