"""校时：NTP 对齐，算出本机时钟相对标准时间的偏差。

为什么需要：抢购拼的是"什么时候发请求/点鼠标"。本机时钟差几百毫秒，
到点判断就整体偏移。实测这台机器慢 550ms。

算法用标准 NTP 四时间戳：
    t1 本机发出    t2 服务器收到    t3 服务器发出    t4 本机收到
    offset = ((t2-t1) + (t3-t4)) / 2
    delay  = (t4-t1) - (t3-t2)
多服务器各采几次，取 delay 最小的那次（网络抖动最小，offset 最准）。
"""
import socket
import struct
import threading
import time

_NTP_DELTA = 2208988800  # 1900-01-01 到 1970-01-01 的秒数
_lock = threading.Lock()
_state = {"offset": 0.0, "delay": None, "server": None, "at": None, "samples": []}

# 校时结果的有效期。超过这个时长还拿旧 offset 当"准确"是危险的：机器
# 休眠/被改系统时间之后，几百毫秒的旧偏差会把倒计时整体推偏。过期就
# 退回本机时钟，并让 info() 标出 stale（审查 S23）。
OFFSET_TTL_S = 6 * 3600


def _query(host, timeout=2.0):
    pkt = b"\x1b" + 47 * b"\x00"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        t1 = time.time()
        s.sendto(pkt, (host, 123))
        data, _ = s.recvfrom(1024)
        t4 = time.time()
    finally:
        s.close()
    if len(data) < 48:
        raise ValueError("短包")
    def ts(off):
        sec, frac = struct.unpack("!II", data[off:off + 8])
        return sec - _NTP_DELTA + frac / 2 ** 32
    t2, t3 = ts(32), ts(40)
    offset = ((t2 - t1) + (t3 - t4)) / 2
    delay = (t4 - t1) - (t3 - t2)
    return {"offset": offset, "delay": max(0.0, delay),
            "stratum": data[1], "server": host}


def sync(servers=None, samples=4, timeout=2.0):
    """返回 {offset, delay, server, samples:[...], ok, error}"""
    servers = servers or ["ntp.aliyun.com", "ntp.tencent.com", "cn.pool.ntp.org"]
    got = []
    errors = []
    for host in servers:
        for _ in range(max(1, samples)):
            try:
                got.append(_query(host, timeout))
            except (socket.timeout, OSError, ValueError) as e:
                errors.append(f"{host}: {type(e).__name__}")
                break  # 这个服务器不通就别重复试了
    if not got:
        with _lock:
            _state.update(offset=0.0, delay=None, server=None, at=time.time(), samples=[])
        return {"ok": False, "error": "所有 NTP 服务器都不通", "tried": errors,
                "offset": 0.0, "delay": None, "server": None, "samples": []}
    best = min(got, key=lambda x: x["delay"])
    samples_out = [{"server": g["server"], "offset": round(g["offset"], 6),
                    "delay": round(g["delay"], 6)} for g in got]
    with _lock:
        _state.update(offset=best["offset"], delay=best["delay"],
                      server=best["server"], at=time.time(),
                      samples=samples_out)
    return {"ok": True, "offset": best["offset"], "delay": best["delay"],
            "server": best["server"], "stratum": best["stratum"],
            # 用刚建好的局部 list，不要在锁外摸 _state["samples"]：
            # 下一次 sync() 会把它整个换掉（审查 S23）
            "at": time.time(), "samples": samples_out, "tried": errors}


def offset():
    """当前可用的时钟偏差。

    过期就返回 0.0 —— 旧偏差比没有偏差更糟：机器休眠/改时间之后，
    它会把倒计时整体推偏，而且越跑越偏（审查 S23）。
    """
    with _lock:
        at = _state.get("at")
        if at is None or (time.time() - at) > OFFSET_TTL_S:
            return 0.0
        return float(_state.get("offset") or 0.0)


def info():
    """校时状态快照。samples 深拷贝一份，外面迭代时不会被下一轮 sync 换掉。"""
    with _lock:
        out = dict(_state)
        out["samples"] = [dict(s) for s in (_state.get("samples") or [])]
    at = out.get("at")
    out["stale"] = bool(at is None or (time.time() - at) > OFFSET_TTL_S)
    if out["stale"]:
        out["offset"] = 0.0
    return out


def true_now():
    """校时后的当前时间戳。抢购倒计时用这个，不用 time.time()。"""
    return time.time() + offset()
