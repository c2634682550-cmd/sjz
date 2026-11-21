"""NTP 被本机代理 fake-ip 接管时的判定。

Clash/Mihomo 的 fake-ip 默认段是 198.18.0.0/15。NTP 域名全解析到该段时：
- 结论必须是「走代理、校时不可用、已用系统时间」，不报 DNS 投毒也不替它担保「不是投毒」；
- 不向 fake-ip 发 NTP 请求（省掉每轮十几秒白等）；
- 自动循环低频重试，且同一状态只通知一次。
"""
import time

from core import timesync as T


def test_proxy_ranges_recognized():
    for ip in ("198.18.0.1", "198.19.255.254", "127.0.0.1", "0.0.0.0",
               "::1", "fd00::1", "fc00::1"):
        assert T._is_proxy_ip(ip), ip
    for ip in ("203.0.113.9", "8.8.8.8", "2001:4860:4860::8888",
               None, "", "not-an-ip"):
        assert not T._is_proxy_ip(ip), ip


def test_all_fake_ip_degrades_without_poison(monkeypatch):
    fake = {"a": "198.18.1.28", "b": "198.18.0.90",
            "c": "198.18.1.30", "d": "198.18.1.29"}
    monkeypatch.setattr(T, "_resolve_ip", lambda h: fake[h])
    queried = []
    monkeypatch.setattr(T, "_query",
                        lambda h, t: queried.append(h) or {})
    res = T.sync(servers=list(fake), samples=2)
    assert res["ok"] is False
    assert res["proxy"] is True
    assert res["attempts"] == 0
    assert "走代理" in res["error"]
    assert "已用系统时间" in res["error"]
    assert "不采信" in res["error"]
    assert "投毒" not in res["error"]       # 不下结论，只陈述解析落点
    assert "198.18.0.90" in res["error"]
    assert "疑似" not in res["error"]
    assert queried == []                    # 不在 fake-ip 上浪费 UDP 请求


def test_mixed_fake_and_real_skips_fake(monkeypatch):
    mapping = {"fake": "198.18.1.1", "a": "203.0.113.1",
               "b": "198.51.100.1", "c": "203.0.114.1"}
    monkeypatch.setattr(T, "_resolve_ip", lambda h: mapping[h])
    seen = []

    def _q(host, timeout):
        seen.append(host)
        return {"server": host, "offset": 0.01, "delay": 0.02, "stratum": 2}

    monkeypatch.setattr(T, "_query", _q)
    res = T.sync(servers=list(mapping), samples=2)
    assert res["ok"] is True
    assert res["proxy"] is False
    assert "fake" not in seen               # 只跳过 fake-ip 那台


def test_auto_proxy_backoff_notifies_once(monkeypatch):
    monkeypatch.setattr(T, "PROXY_RETRY_S", 0.05)
    a = T._AutoSync()
    monkeypatch.setattr(a, "_sync_once", lambda: {
        "ok": False, "proxy": True,
        "error": "校时不可用：走代理，已用系统时间"})
    calls = []
    a.start(servers=[], samples=1, interval_s=0.05,
            notify=lambda res, trig: calls.append(trig))
    time.sleep(0.6)
    st = a.status()
    a.stop()
    assert st["proxy"] is True
    assert st["retry_interval_s"] == 0.05
    assert st["detail"] == "校时不可用：走代理，已用系统时间"
    assert len(calls) <= 1                  # 进入代理态后不再刷日志
