"""时间同步降级态的接口字段 + 前端提示条。

- 接口只加不改：`/api/scheduler` 与 `/api/ctrl/status` 都带 `time_sync` 对象
  （`degraded` / `source_count` / `offset_ms` / `warning`）。
- 前端只追加自己的区块：降级 → 顶部出现低调提示条；严格模式 → 不出现。
"""
import json
import pathlib
import re
import shutil
import subprocess
import types

import pytest

from core import server as S

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = ROOT / "web" / "index.html"


def _sample(server, ip, off, delay=0.01):
    return {"server": server, "ip": ip, "offset": off, "delay": delay}


def _block(monkeypatch, **ts):
    monkeypatch.setattr(S.timesync, "info", lambda: ts)
    return S._time_sync_block()


def test_degraded_block_fields_are_complete(monkeypatch):
    b = _block(
        monkeypatch, degraded=True, offset=0.1234,
        samples=[_sample("ntp1.example", "203.0.113.9", 0.120),
                 _sample("ntp1.example", "203.0.113.9", 0.126),
                 {"server": "median", "ip": None, "offset": 0.123}],
        warning="单源降级校时：仅 1 个 NTP 来源")
    assert set(b) == {"degraded", "source_count", "offset_ms", "warning"}
    assert b["degraded"] is True
    assert b["source_count"] == 1          # median 伪样本不算来源
    assert b["offset_ms"] == 123.4
    assert b["warning"].startswith("单源降级校时")


def test_strict_block_has_no_degraded_signal(monkeypatch):
    b = _block(
        monkeypatch, degraded=False, offset=0.0,
        samples=[_sample("a", "198.51.100.1", 0.001),
                 _sample("b", "203.0.113.2", -0.001)],
        warning="")
    assert b["degraded"] is False
    assert b["source_count"] == 2
    assert b["offset_ms"] == 0.0
    assert b["warning"] == ""


def test_ctrl_status_carries_time_sync(monkeypatch):
    monkeypatch.setattr(S._scheduler, "status", lambda: {})
    monkeypatch.setattr(S._license, "summary", lambda: {})
    monkeypatch.setattr(S, "_time_sync_block",
                        lambda: {"degraded": True, "source_count": 1,
                                 "offset_ms": 88.8, "warning": "单源"})
    st = S.Handler._ctrl_status(types.SimpleNamespace())
    assert st["time_sync"]["degraded"] is True
    assert st["time_sync"]["offset_ms"] == 88.8


# ---------------------------------------------------------------- 前端
def _banner_js():
    text = HTML.read_text(encoding="utf-8")
    m = re.search(r'<script id="tsync-banner-js">(.*?)</script>', text, re.S)
    assert m, "缺少追加的 tsync-banner 脚本块"
    return text, m.group(1)


def test_frontend_block_is_appended_after_shared_area():
    text, _ = _banner_js()
    assert text.index("TME-SYNC-BANNER BEGIN") > text.index("FEATURE-BLOCKS END")
    assert 'id="tsyncBanner"' in text


def _eval_js(expr, extra=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过前端行为用例")
    js = _banner_js()[1]
    prog = (js + "\n;console.log(JSON.stringify((function(){"
            + extra + "return " + expr + "})()))")
    out = subprocess.run([node, "-e", prog], capture_output=True,
                         text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_frontend_banner_appears_when_degraded():
    got = _eval_js(
        "r",
        "var seen={style:{},textContent:'',title:'',"
        "removeAttribute:function(k){delete this[k];}};"
        "var ts={degraded:true,source_count:1,offset_ms:123.4,warning:'单源'};"
        "globalThis.__tsyncBanner.paint(seen,ts);"
        "var r={text:seen.textContent,display:seen.style.display};")
    assert got["display"] == "block"
    assert got["text"] == "时间同步：降级校时（仅 1 个来源，偏差 123.4 ms）"


def test_frontend_banner_hidden_in_strict_mode():
    got = _eval_js(
        "r",
        "var seen={style:{},textContent:'x',title:'t',"
        "removeAttribute:function(k){delete this[k];}};"
        "globalThis.__tsyncBanner.paint(seen,{degraded:false,source_count:2,"
        "offset_ms:0,warning:''});"
        "var r={text:seen.textContent,display:seen.style.display};")
    assert got["display"] == "none"
    assert got["text"] == ""
