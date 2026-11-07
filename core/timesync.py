"""校时：NTP 对齐，算出本机时钟相对标准时间的偏差。

为什么需要：抢购拼的是"什么时候发请求/点鼠标"。本机时钟差几百毫秒，
到点判断就整体偏移。实测这台机器慢 550ms。

算法用标准 NTP 四时间戳：
    t1 本机发出    t2 服务器收到    t3 服务器发出    t4 本机收到
    offset = ((t2-t1) + (t3-t4)) / 2
    delay  = (t4-t1) - (t3-t2)
多服务器各采几次，做中位数 + 离群剔除（见 `sync`）。

审查 R03-N01：一条 48 字节全零报文就能算出 −126 年的偏移，而且 delay=0
让它成为"最佳样本"。这里把应答校验补齐：长度、mode、stratum、origin 回显、
时间戳非零、|offset| 上限、delay 上限。任何一条不过就丢弃该样本；**丢弃
只影响这次采样，绝不动已经生效的校时状态**。

审查 R03-N05：有效期 TTL 以前用壁钟记，系统时间一回拨旧偏移就永不过期，
往前跳又立刻失效。现在锚点同时记壁钟和单调时钟，TTL 走单调轴；壁钟只用
来算 offset 本身，另加一条"壁钟被回拨"的显式失效判据。
"""
import logging
import math
import os
import random
import socket
import struct
import threading
import time

_NTP_DELTA = 2208988800  # 1900-01-01 到 1970-01-01 的秒数
_lock = threading.Lock()
_log = logging.getLogger("dafors.timesync")
_state = {"offset": 0.0, "delay": None, "server": None, "at": None,
          "at_mono": None, "samples": [], "last_error": "",
          "degraded": False, "warning": "", "drift_ppm": 0.0}

# 自动校时（server 的手动入口与后台循环共用这把锁，避免两路 NTP 同时跑）。
sync_lock = threading.Lock()

# ---- 自动实时校准（2026-11 需求：不再让用户点按钮）----
# 首轮在进程启动时自动跑；之后按 interval 持续重校（默认 45s，30~60s 档）。
# 失败按 1/2/5/15/30 秒退避，到顶后落到固定间隔继续试；游戏窗口一出现
# 立刻补校一次。整个过程只做 UDP 收发，不建窗口、不抢焦点。
AUTO_INTERVAL_DEFAULT_S = 45.0
AUTO_INTERVAL_MIN_S = 15.0
AUTO_INTERVAL_MAX_S = 300.0
AUTO_BACKOFF_S = (1.0, 2.0, 5.0, 15.0, 30.0)
AUTO_FAIL_FIXED_S = 60.0       # 退避到顶后的固定重试间隔
AUTO_WINDOW_POLL_S = 5.0       # 游戏窗口探测节奏（EnumWindows，毫秒级）
DRIFT_MIN_GAP_S = 10.0         # 两次成功校准间隔太短时不算漂移率（噪声）
EXTRAPOLATE_MAX_S = 900.0      # 漂移外推上限；再久就退回最后一次实测 offset

# 校时结果的有效期。超过这个时长还拿旧 offset 当"准确"是危险的：机器
# 休眠/被改系统时间之后，几百毫秒的旧偏差会把倒计时整体推偏。过期就
# 退回本机时钟，并让 info() 标出 stale（审查 N05）。现在按单调时钟计。
OFFSET_TTL_S = 6 * 3600

# 应答合理性上限（审查 N01）
MAX_OFFSET_S = 86400.0      # 和标准时间差超过一天，一定是脏包/错配主机
MAX_DELAY_S = 5.0           # 单次往返超过 5 秒，offset 已经没有参考价值
MIN_SAMPLES = 3             # 少于 3 个有效样本不做更新（中位数没有意义）
MAX_DEVIATION_S = 60.0      # 偏离中位数超过 60 秒的样本按离群丢弃
CLOCK_BACKSTEP_S = 2.0      # 壁钟往回走超过这个量 = 有人在改时间

# 采样重试策略（审查 R2）。以前单次丢包就 `break` 放弃整台服务器，再叠加
# MIN_SAMPLES=3：只要三台各丢一个包，这一轮就一个有效样本都没有，校时长期
# 不更新。现在单个包丢了只记一次失败并重试/轮换，连续失败才暂时放弃该主机。
MAX_CONSEC_FAIL = 3         # 同一台服务器连续失败几次才本轮放弃
MAX_TOTAL_ATTEMPTS = 16     # 单轮 sync 的总请求上限（3 台×4 次基准 + 4 次重试余量）
BACKOFF_BASE_S = 0.1        # 首次重试前等 0.1s，之后 0.2 / 0.4 …
BACKOFF_MAX_S = 0.5         # 退避上限

# ---- 单源带偏防护（审查 N5/N6/N7）----
# 以前的判据只看"单个包合不合理"：4 个样本全来自同一台伪造源、一起偏 +40000s，
# 照样能通过 |offset| ≤ 86400 与 MIN_SAMPLES=3 —— 一致性反而是伪造的特征。
# 现在按"来源独立性 + 偏差量级 + 单调时钟漂移"三条交叉验证。
MAX_PLAUSIBLE_OFFSET_S = 300.0   # 正常机器偏差是毫秒~秒级，5 分钟已是上限
RELAX_MAX_OFFSET_S = 86400.0     # 显式放宽后的硬上限（仍不许超过 24 小时）
MAX_DRIFT_PPM = 500.0            # 单调时钟漂移率上限（数百 ppm 量级）
DRIFT_ABS_TOL_S = 0.6            # 漂移交叉验证的绝对容差（短间隔的测量噪声）
ALLOW_LARGE_OFFSET_ENV = "DAFORS_NTP_ALLOW_LARGE_OFFSET"

# ---- 单源降级（黑盒复核：不能把只配一台 NTP 的用户直接判死）----
# 多源优先；拿不到多源时允许"单源降级校时"，但必须同时满足：
#   |offset| ≤ 300s、与单调时钟漂移一致、连续两次采样偏差稳定。
DEFAULT_SERVERS = (
    "ntp.aliyun.com",       # 阿里云
    "ntp.tencent.com",      # 腾讯云
    "cn.pool.ntp.org",      # NTP Pool（中国区，多台）
    "time.amazonaws.com",   # AWS Time Sync
    "ntp.ntsc.ac.cn",       # 国家授时中心
)
ONLY_CONFIGURED_ENV = "DAFORS_NTP_ONLY_CONFIGURED"
DEGRADED_STABLE_S = 1.0     # 降级校时：连续样本互相偏离不超过 1 秒


def _to_ntp(t):
    """秒（1970 起）→ NTP (秒, 小数)。"""
    sec = int(math.floor(t))
    frac = int((t - sec) * (1 << 32))
    return (sec + _NTP_DELTA) & 0xFFFFFFFF, frac & 0xFFFFFFFF


def _from_ntp(sec, frac):
    """NTP 字段 → 秒（1970 起）。"""
    return sec - _NTP_DELTA + frac / 2 ** 32


def _query(host, timeout=2.0):
    """发一次 NTP 请求并校验应答。不合格抛 ValueError（调用方丢弃该样本）。

    请求里带上**本次的发送时间戳**（含随机低位），应答的 origin 字段必须
    原样带回来。全零报文、错配主机、蜜罐都会在这里被挡住。
    """
    pkt = bytearray(48)
    pkt[0] = 0x1b                       # LI=0, VN=3, mode=3(client)
    t1 = time.time()
    sec1, frac1 = _to_ntp(t1)
    # 低 16 位掺随机数：两台机器同一秒发请求时也不会撞上同一个 origin，
    # 顺带让"回显一串固定值"的伪造报文过不了匹配。
    frac1 = (frac1 ^ (random.getrandbits(16) << 16)) & 0xFFFFFFFF
    expected_origin = (sec1, frac1)
    struct.pack_into("!II", pkt, 40, sec1, frac1)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(bytes(pkt), (host, 123))
        data, _ = s.recvfrom(1024)
        t4 = time.time()
    finally:
        s.close()

    if len(data) < 48:
        raise ValueError(f"短包（{len(data)} 字节，至少 48）")
    mode = data[0] & 0x7
    if mode not in (4, 5):               # 4=server, 5=broadcast
        raise ValueError(f"mode={mode} 不是服务端应答")
    stratum = data[1]
    if not 1 <= stratum <= 15:
        # 0 = KoD（ Kiss-o'-Death，服务器明确拒绝）；16~255 = 保留/无效
        raise ValueError(f"stratum={stratum} 不可信（有效范围 1..15）")
    origin = struct.unpack("!II", data[24:32])
    if origin != expected_origin:
        raise ValueError("origin 时间戳与本次请求不匹配（伪造/错配应答）")

    t2 = _from_ntp(*struct.unpack("!II", data[32:40]))
    t3 = _from_ntp(*struct.unpack("!II", data[40:48]))
    if t2 <= 0 or t3 <= 0:
        raise ValueError("服务器时间戳为 0（全零/无效应答）")
    if t3 < t2 - 1.0:
        raise ValueError("服务器发送时间早于接收时间")

    offset = ((t2 - t1) + (t3 - t4)) / 2
    delay = (t4 - t1) - (t3 - t2)
    if not math.isfinite(offset) or abs(offset) > MAX_OFFSET_S:
        raise ValueError(f"偏移 {offset!r} 超出 ±{MAX_OFFSET_S:.0f}s，丢弃")
    if not math.isfinite(delay) or abs(delay) > MAX_DELAY_S:
        raise ValueError(f"往返延迟 {delay!r} 不合理，丢弃")
    # 本机/同网段时 (t4-t1) 可能略小于 (t3-t2)，算出微小负值。那是测量噪声，
    # 不是坏包；夹到 0 保留样本，但绝对值超过上限的照样丢。
    delay = max(0.0, delay)
    return {"offset": offset, "delay": delay,
            "stratum": stratum, "server": host}


def _median(values):
    vals = sorted(values)
    n = len(vals)
    if not n:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _note_error(msg):
    """只改错误说明，不碰 offset / at / samples（异常不得污染时钟状态）。"""
    with _lock:
        _state["last_error"] = msg


def _resolve_ip(host):
    """主机名 → IPv4；解析不出来返回 None（退化成按主机名去重）。"""
    getaddrinfo = getattr(socket, "getaddrinfo", None)
    if getaddrinfo is None:
        return None
    try:
        infos = getaddrinfo(host, 123, socket.AF_INET, socket.SOCK_DGRAM)
    except (OSError, TypeError, AttributeError):
        return None
    for info in infos or []:
        addr = info[4][0] if len(info) > 4 else None
        if addr:
            return addr
    return None


def _dedupe_by_source(samples):
    """同一 IP / 同一主机名只算一个样本（审查 N5：同源复制不是多源）。"""
    seen = {}
    for s in samples:
        key = s.get("ip") or s.get("server")
        seen.setdefault(key, s)
    return list(seen.values())


def _allow_large_offset(flag=None):
    """是否允许超出分钟级的偏差（必须显式确认）。"""
    if flag is None:
        raw = os.environ.get(ALLOW_LARGE_OFFSET_ENV, "")
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")
    return bool(flag)


def _only_configured(flag=None):
    """用户是否显式声明「只用配置里这几台」（此时不再自动补齐默认源）。"""
    if flag is None:
        raw = os.environ.get(ONLY_CONFIGURED_ENV, "")
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")
    return bool(flag)


def _fill_servers(servers, only_configured):
    """内置源自动补齐：没配 → 全用默认；只配一台 → 补到多源。

    只有显式声明「只用这些」时才保留单源（走降级路径）。
    """
    servers = [str(s) for s in (servers or []) if s]
    if not servers:
        return [] if only_configured else list(DEFAULT_SERVERS)
    if len(servers) == 1 and not only_configured:
        only = servers[0]
        return [only] + [s for s in DEFAULT_SERVERS if s != only]
    return servers


def _drift_conflict(new_offset, now_wall, now_mono):
    """用单调时钟漂移率交叉验证新偏差（审查 N7）。

    offset = 标准时间 - 本机时间。两次校时之间，本机壁钟比单调轴多走了
    Δ，offset 就应当正好少 Δ；实测变化量跟不上这个量，说明要么壁钟被人
    改过，要么外部源在说谎。返回空串表示通过。
    """
    with _lock:
        prev_at = _state.get("at")
        prev_mono = _state.get("at_mono")
        prev_offset = float(_state.get("offset") or 0.0)
    if prev_at is None or prev_mono is None:
        return ""
    mono_delta = now_mono - prev_mono
    if mono_delta <= 0:
        return ""
    wall_delta = now_wall - prev_at
    expected_change = mono_delta - wall_delta
    actual_change = new_offset - prev_offset
    budget = max(DRIFT_ABS_TOL_S, abs(mono_delta) * MAX_DRIFT_PPM / 1e6 * 3.0)
    if abs(actual_change - expected_change) > budget:
        return (f"新偏差 {new_offset:.3f}s 与单调时钟漂移矛盾："
                f"期间壁钟走 {wall_delta:.1f}s、单调时钟走 {mono_delta:.1f}s，"
                f"偏移应变化 {expected_change:.1f}s，实测变化 {actual_change:.1f}s"
                f"（超过 {budget:.1f}s 容差）")
    return ""


def _sample_view(g):
    return {"server": g["server"], "ip": g.get("ip"),
            "offset": round(g["offset"], 6), "delay": round(g["delay"], 6)}


def _fail(msg, errors, attempts, got):
    """统一构造失败返回：保留上次生效的校时状态，只更新错误说明。"""
    _note_error(msg)
    with _lock:
        cur = float(_state.get("offset") or 0.0)
        delay = _state.get("delay")
        server = _state.get("server")
    return {"ok": False, "error": msg, "tried": errors, "offset": cur,
            "delay": delay, "server": server, "attempts": attempts,
            "degraded": False,
            "samples": [_sample_view(g) for g in got]}


def sync(servers=None, samples=4, timeout=2.0, allow_large_offset=None,
         only_configured=None):
    """返回 {offset, delay, server, samples:[...], independent, degraded, ok, error}

    **多源严格路径**（审查 N5/N6/N7）：至少 MIN_SAMPLES 个独立来源，且覆盖
    ≥2 个 /16 网段：
      1. 按解析后的 IP 去重 —— 同一台机器换几个域名也算一个样本；
      2. 同一 /16 内换 IP 也算同一来源；
      3. 偏差超过分钟级上限直接判不可信（除非显式放宽）；
      4. 全部样本高度一致但数值离谱 = 同源伪造，不因"一致"而采纳；
      5. 与单调时钟漂移交叉验证，壁钟被改动/源在说谎都拒收。

    **单源降级路径**（黑盒复核要求，不能把单源用户判死）：只有 1 个独立来源
    且只配了 1 个主机时允许同步，但必须同时满足 |offset| ≤ 300s、与单调
    时钟漂移一致、连续两个样本偏差稳定；结果标记 degraded=True 并写警告
    日志，info() 会把它带给界面。多个域名塌缩到同一 IP 仍按 DNS 投毒拒绝。

    服务器清单没配或只配一台时自动补齐 DEFAULT_SERVERS（≥4 个来源）；
    显式声明只用配置里那几台（参数 only_configured=True 或环境变量
    DAFORS_NTP_ONLY_CONFIGURED=1）才走单源降级。
    """
    servers = _fill_servers(servers, _only_configured(only_configured))
    if not servers:
        return _fail("没有可用的 NTP 服务器（已声明只用配置项但配置为空）",
                     [], 0, [])
    try:
        samples = max(1, int(samples))
    except (TypeError, ValueError):
        samples = 4
    allow = _allow_large_offset(allow_large_offset)

    got = []
    errors = []
    attempts = 0
    for host in servers:
        ip = _resolve_ip(host)
        ok_here = 0
        fails = 0
        # 目标仍是每台 samples 个**有效**样本：丢包不占样本名额，只占重试
        # 次数（总请求数由 MAX_TOTAL_ATTEMPTS 封顶），所以一次丢包不会让
        # 这台服务器的样本变少，更不会整台放弃（审查 R2）。
        while ok_here < samples:
            if attempts >= MAX_TOTAL_ATTEMPTS:
                break
            attempts += 1
            try:
                one = _query(host, timeout)
                one.setdefault("ip", ip)
                got.append(one)
                ok_here += 1
                fails = 0
            except (socket.timeout, OSError, ValueError) as e:
                fails += 1
                errors.append(f"{host}: {type(e).__name__}: {e}")
                if fails >= MAX_CONSEC_FAIL:
                    errors.append(f"{host}: 连续 {fails} 次失败，本轮放弃该主机")
                    break
                # 单次丢包只算一次失败：等一小段再打同一个主机，而不是直接
                # 放弃整台。退避随连续失败次数翻倍，封顶 BACKOFF_MAX_S。
                time.sleep(min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (fails - 1)))

    if not got:
        return _fail("所有 NTP 服务器都不通或应答不可信", errors, attempts, got)

    indep = _dedupe_by_source(got)
    ips = [s.get("ip") for s in indep]
    nets = ({".".join(ip.split(".")[:2]) for ip in ips}
            if all(ips) else set())
    strict = (len(indep) >= MIN_SAMPLES
              and (not all(ips) or len(nets) >= 2))
    if not strict:
        # 只有"真的只配了 1 个主机、只解析出 1 个来源"才允许降级；
        # 多个域名塌缩到同一 IP 仍然按 DNS 投毒拒绝。
        single_host = len({s["server"] for s in got}) == 1
        if not (single_host and len(indep) == 1):
            if len(indep) < MIN_SAMPLES:
                msg = (f"独立来源只有 {len(indep)} 个（有效样本 {len(got)} 个，"
                       f"需要 ≥{MIN_SAMPLES} 个来自不同 IP）：同一来源的重复"
                       f"应答不能当多源，保留上一次校时")
            else:
                msg = (f"所有 NTP 来源都解析到同一 /16 网段"
                       f"（{sorted(nets)[0]}），疑似 DNS 投毒/单源伪造，"
                       f"保留上一次校时")
            return _fail(msg, errors, attempts, got)
    degraded = not strict

    # 降级路径本来就只有一个来源，用全部连续样本来取中位数更稳；严格路径
    # 只用去重后的独立样本，避免同源重复应答抬高权重。
    pool = got if degraded else indep
    med = _median([g["offset"] for g in pool])
    kept = [g for g in pool if abs(g["offset"] - med) <= MAX_DEVIATION_S]
    if not kept:
        kept = pool
    best_offset = _median([g["offset"] for g in kept])
    spread = max(g["offset"] for g in pool) - min(g["offset"] for g in pool)

    # 降级路径的 300s 是硬上限，不随 allow_large_offset 放宽。
    limit = MAX_PLAUSIBLE_OFFSET_S if degraded else (
        RELAX_MAX_OFFSET_S if allow else MAX_PLAUSIBLE_OFFSET_S)
    if abs(best_offset) > limit:
        msg = (f"偏差 {best_offset:.1f}s 超出可信范围 ±{limit:.0f}s"
               f"（{len(indep)} 个独立来源）")
        if spread <= MAX_DEVIATION_S:
            msg += "；所有来源高度一致但绝对值异常大 —— 同源伪造的典型特征"
        if not allow and not degraded:
            msg += f"；确需放宽请显式设置环境变量 {ALLOW_LARGE_OFFSET_ENV}=1"
        return _fail(msg, errors, attempts, got)

    now_wall, now_mono = time.time(), time.monotonic()
    conflict = _drift_conflict(best_offset, now_wall, now_mono)
    if conflict:
        return _fail(conflict, errors, attempts, got)

    if degraded:
        # 单源降级必须有两个连续样本互相印证，单包不校时。
        got_spread = (max(g["offset"] for g in got)
                      - min(g["offset"] for g in got))
        if len(got) < 2 or got_spread > DEGRADED_STABLE_S:
            return _fail(f"单源降级校时需要至少 2 个连续样本且偏差稳定"
                         f"（现有 {len(got)} 个，最大偏离 {got_spread:.3f}s"
                         f" > {DEGRADED_STABLE_S:.1f}s），保留上一次校时",
                         errors, attempts, got)

    best = min(kept, key=lambda g: g["delay"])
    samples_out = [_sample_view(g) for g in got]
    samples_out.append({"server": "median", "ip": None,
                        "offset": round(best_offset, 6),
                        "delay": round(best["delay"], 6),
                        "kept": len(kept), "independent": len(indep)})
    # 两次成功校准之间的线性漂移率：offset 的变化量 / 单调时钟走过的时间。
    # 自动循环靠它把下一次校准前的 offset 外推出来（见 `_offset_estimate`），
    # 壁钟跳变不会被带进倒计时。
    drift = _drift_estimate(best_offset, now_mono)
    warning = ""
    if degraded:
        warning = (f"单源降级校时：仅 1 个 NTP 来源（{best['server']}），"
                   f"偏差 {best_offset:.3f}s，已用 {len(got)} 个稳定样本确认；"
                   f"建议配置多个不同 NTP 服务器")
        _log.warning(warning)
    with _lock:
        _state.update(offset=best_offset, delay=best["delay"],
                      server=best["server"], at=now_wall,
                      at_mono=now_mono, drift_ppm=drift,
                      samples=samples_out, last_error="",
                      degraded=degraded, warning=warning)
        stamp = _state["at"]
    return {"ok": True, "offset": best_offset, "delay": best["delay"],
            "server": best["server"], "stratum": best["stratum"],
            "at": stamp, "samples": samples_out, "tried": errors,
            "attempts": attempts, "independent": len(indep),
            "degraded": degraded, "warning": warning}


def _drift_estimate(new_offset, now_mono):
    """按相邻两次成功校准估计本机单调时钟的速率误差（ppm）。

    offset = 标准时间 − 本机壁钟。两次校准之间壁钟比标准时间快 r，
    offset 就会减少 r × 间隔，所以 r = −Δoffset / Δmono。间隔太短
    （< DRIFT_MIN_GAP_S）时测量噪声占主导，保留上一次估计；超上限
    （MAX_DRIFT_PPM）的估计按噪声丢弃，不污染外推。
    """
    with _lock:
        prev_offset = float(_state.get("offset") or 0.0)
        prev_mono = _state.get("at_mono")
        prev_drift = float(_state.get("drift_ppm") or 0.0)
    if prev_mono is None:
        return prev_drift
    gap = now_mono - prev_mono
    if gap < DRIFT_MIN_GAP_S:
        return prev_drift
    raw = -(float(new_offset) - prev_offset) / gap * 1e6
    if not math.isfinite(raw) or abs(raw) > MAX_DRIFT_PPM:
        return prev_drift
    # 两次估计做一点平滑，避免一次 NTP 抖动把外推斜率带偏。
    return 0.5 * (prev_drift + raw)


def _calibration_view():
    """已生效校准的精简快照（给状态接口 / preflight 用，不递归调 info）。"""
    reason = _stale_reason()
    with _lock:
        at = _state.get("at")
        off = float(_state.get("offset") or 0.0)
        server = _state.get("server")
        drift = float(_state.get("drift_ppm") or 0.0)
        samples = [s for s in (_state.get("samples") or [])
                   if isinstance(s, dict) and s.get("server") != "median"]
    usable = at is not None and not reason
    return {"calibrated": usable,
            "last_ok_at": at,
            "server": server,
            "offset": 0.0 if reason else off,
            "offset_ms": round((0.0 if reason else off) * 1000.0, 3),
            "samples": len(samples),
            "drift_ppm": round(drift, 3),
            "stale": bool(reason),
            "stale_reason": reason}


def _offset_estimate(mono_now):
    """当前时刻的 offset 外推值（不做过期判断；调用方另行判断）。"""
    with _lock:
        off = float(_state.get("offset") or 0.0)
        at_mono = _state.get("at_mono")
        drift = float(_state.get("drift_ppm") or 0.0)
    if at_mono is None:
        return off
    dt = mono_now - at_mono
    if dt <= 0.0 or dt > EXTRAPOLATE_MAX_S:
        return off
    return off - drift * 1e-6 * dt


def _stale_reason():
    """返回过期/不可信的原因；空串表示可用。调用方持锁时不要调。"""
    with _lock:
        at = _state.get("at")
        at_mono = _state.get("at_mono")
    if at is None:
        return "还没校时"
    if at_mono is None:
        # 兼容旧状态（/测试注入）：只有壁钟锚点，按壁钟判 TTL。
        age = time.time() - at
        if age < -CLOCK_BACKSTEP_S:
            return "系统时钟被回拨"
        if age > OFFSET_TTL_S:
            return "校时结果已过期"
        return ""
    # 正常路径：TTL 只认单调时钟，壁钟跳动不影响"已经过了多久"。
    age_mono = time.monotonic() - at_mono
    if age_mono < 0:
        return "单调时钟回退"
    if age_mono > OFFSET_TTL_S:
        return "校时结果已过期"
    # 壁钟往回走说明有人在改时间：旧 offset 是相对旧时钟算的，不再可信。
    if time.time() - at < -CLOCK_BACKSTEP_S:
        return "系统时钟被回拨"
    return ""


def offset():
    """当前可用的时钟偏差（两次校准之间按漂移率外推）。

    过期/不可信就返回 0.0 —— 旧偏差比没有偏差更糟：机器休眠/改时间之后，
    它会把倒计时整体推偏，而且越跑越偏（审查 N05）。外推只在本轮 TTL 与
    `EXTRAPOLATE_MAX_S` 之内生效，超时退回最后一次实测值。
    """
    if _stale_reason():
        return 0.0
    return _offset_estimate(time.monotonic())


def info():
    """校时状态快照。samples 深拷贝一份，外面迭代时不会被下一轮 sync 换掉。

    除原有字段外追加：`calibrated` / `offset_ms` / `drift_ppm` / `auto`。
    `auto` 是实时校准的调度状态（来源、样本数、最近一次、下一次计划、
    连续失败次数与最后失败原因），preflight 和状态接口直接读它。
    """
    reason = _stale_reason()
    with _lock:
        out = dict(_state)
        out["samples"] = [dict(s) for s in (_state.get("samples") or [])]
    out["stale"] = bool(reason)
    out["stale_reason"] = reason
    out["calibrated"] = out.get("at") is not None and not reason
    if out["stale"]:
        out["offset"] = 0.0
    try:
        out["offset_ms"] = round(float(out.get("offset") or 0.0) * 1000.0, 3)
    except (TypeError, ValueError, OverflowError):
        out["offset_ms"] = 0.0
    if not math.isfinite(out["offset_ms"]):
        out["offset_ms"] = 0.0
    try:
        out["auto"] = _auto.status()
    except Exception:                     # noqa: BLE001
        out["auto"] = {}
    return out


def true_now():
    """校时后的当前时间戳。抢购倒计时用这个，不用 time.time()。

    以**单调时钟锚点**推进：true_now = 锚点壁钟 + 单调流逝 + 外推 offset。
    这样系统时钟被 NTP 步进/手动改时间时，倒计时不会被瞬间带偏（R03-N05）。
    没有可用锚点（从未校准/已过期）才退回 time.time()。
    """
    if _stale_reason():
        return time.time()
    mono = time.monotonic()
    with _lock:
        at = _state.get("at")
        at_mono = _state.get("at_mono")
        off = float(_state.get("offset") or 0.0)
        drift = float(_state.get("drift_ppm") or 0.0)
    if at is None or at_mono is None:
        return time.time() + off
    dt = mono - at_mono
    if dt < 0.0:                     # 单调时钟回退：退回壁钟路径
        return time.time() + off
    if dt > EXTRAPOLATE_MAX_S:       # 太久没校准，别拿漂移外推当准
        return time.time() + off
    return float(at) + dt + off - drift * 1e-6 * dt


def _clamp_interval(value):
    """自动重校间隔：默认 45s，允许 15~300s；脏值退回默认。"""
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return AUTO_INTERVAL_DEFAULT_S
    if not math.isfinite(v):
        return AUTO_INTERVAL_DEFAULT_S
    return max(AUTO_INTERVAL_MIN_S, min(AUTO_INTERVAL_MAX_S, v))


def _retry_delay(failures, interval_s):
    """第 N 次连续失败后的等待：1/2/5/15/30s，到顶后固定 60s。"""
    n = max(1, int(failures or 1))
    if n <= len(AUTO_BACKOFF_S):
        return AUTO_BACKOFF_S[n - 1]
    return AUTO_FAIL_FIXED_S


class _AutoSync:
    """后台实时校准循环：启动即校，之后按间隔重校，失败退避自动重试。

    - 只做 UDP（`sync()`），不建窗口、不抢焦点；
    - `busy()` 为真时每次让路 2s（抢购开火/点击期间不与 OCR、点击抢资源）；
    - `window_probe()` 每 5s 探一次，游戏窗口从无到有时立刻补校一次；
    - 线程 daemon + stop 事件，进程退出时不需要等它。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._wake_reason = ""
        self._running = False
        self._enabled = False
        self._state = "idle"
        self._servers = None
        self._samples = 4
        self._interval_s = AUTO_INTERVAL_DEFAULT_S
        self._notify = None
        self._busy = None
        self._window_probe = None
        self._failures = 0
        self._last_error = ""
        self._last_trigger = "startup"
        self._last_attempt_at = None
        self._last_duration_ms = 0.0
        self._next_mono = None
        self._next_wait_s = 0.0
        self._first_ready = threading.Event()

    # ---- 生命周期 ----

    def start(self, servers=None, samples=4, interval_s=None,
              notify=None, busy=None, window_probe=None):
        """启动后台循环；重复调用只是更新参数并立刻补校一次。"""
        with self._lock:
            self._interval_s = _clamp_interval(interval_s)
            if servers is not None:
                self._servers = [str(s) for s in servers if s]
            try:
                self._samples = max(1, int(samples or 4))
            except (TypeError, ValueError):
                self._samples = 4
            if notify is not None:
                self._notify = notify
            if busy is not None:
                self._busy = busy
            if window_probe is not None:
                self._window_probe = window_probe
            self._enabled = True
            alive = self._thread is not None and self._thread.is_alive()
            if alive:
                self._wake_reason = "manual"
                self._wake.set()
            if alive:
                return False
            self._stop.clear()
            self._wake.clear()
            self._wake_reason = ""
            self._first_ready.clear()
            self._running = True
            self._state = "starting"
            self._thread = threading.Thread(target=self._run,
                                            name="time-autosync", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout=3.0):
        with self._lock:
            t = self._thread
            self._running = False
            self._enabled = False
        self._stop.set()
        self._wake.set()
        if t is not None and t.is_alive() \
                and t is not threading.current_thread():
            t.join(timeout)
        with self._lock:
            if t is not None and not t.is_alive():
                self._thread = None
            self._state = "stopped"
            self._next_mono = None

    def trigger(self, reason="manual"):
        """立刻补校一次（手动按钮 / 游戏窗口出现）。"""
        with self._lock:
            if not self._running:
                return False
            self._wake_reason = reason
        self._wake.set()
        return True

    def note_result(self, res):
        """手动入口的校时结果同步给调度状态：成功清零失败计数，失败续计。"""
        with self._lock:
            if res.get("ok"):
                self._failures = 0
                self._last_error = ""
            else:
                self._failures += 1
                self._last_error = str(res.get("error") or "校准失败")

    def wait_first(self, timeout=3.0):
        """等首轮校准结束（preflight 用它换「已校准」文案）。"""
        if not self._running:
            return False
        if self._first_ready.is_set():
            return True
        return self._first_ready.wait(max(0.0, float(timeout)))

    # ---- 状态 ----

    def status(self):
        with self._lock:
            running = self._running
            enabled = self._enabled
            state = self._state
            interval = self._interval_s
            failures = self._failures
            err = self._last_error
            last_attempt = self._last_attempt_at
            duration = self._last_duration_ms
            next_mono = self._next_mono
            next_wait = self._next_wait_s
            trigger = self._last_trigger
        cal = _calibration_view()
        now_mono = time.monotonic()
        next_in = (max(0.0, next_mono - now_mono)
                   if next_mono is not None else None)
        cal_ok = bool(cal.get("calibrated"))
        source = "ntp" if cal_ok else "system"
        return {
            "enabled": bool(enabled),
            "running": bool(running),
            "state": state,
            "calibrated": cal_ok,
            "source": source,
            "source_label": "网络校时" if cal_ok else "仅系统时间",
            "offset_ms": cal.get("offset_ms", 0.0),
            "drift_ppm": cal.get("drift_ppm", 0.0),
            "samples": cal.get("samples", 0),
            "last_ok_at": cal.get("last_ok_at"),
            "last_ok_age_s": (round(max(0.0, time.time() - cal["last_ok_at"]), 1)
                              if cal.get("last_ok_at") else None),
            "last_attempt_at": last_attempt,
            "last_duration_ms": duration,
            "next_at": (round(time.time() + next_in, 1)
                        if next_in is not None else None),
            "next_in_s": (round(next_in, 1) if next_in is not None else None),
            "interval_s": interval,
            "retry_interval_s": next_wait,
            "consecutive_failures": failures,
            "last_error": err,
            "last_trigger": trigger,
        }

    # ---- 内部 ----

    def _run(self):
        trigger = "startup"
        try:
            while not self._stop.is_set():
                # 抢购开火期间让路：不和点击 / OCR 抢资源（只等，不自旋）。
                if self._busy is not None:
                    try:
                        if self._busy():
                            wr = self._wait(2.0, poll_window=False)
                            if wr == "stop":
                                break
                            if wr:
                                trigger = wr
                            continue
                    except Exception:         # noqa: BLE001
                        pass
                with self._lock:
                    self._state = "syncing"
                    self._last_trigger = trigger
                    self._last_attempt_at = time.time()
                t0 = time.monotonic()
                res = self._sync_once()
                duration = (time.monotonic() - t0) * 1000.0
                with self._lock:
                    self._last_duration_ms = round(duration, 1)
                    if res.get("ok"):
                        self._failures = 0
                        self._last_error = ""
                        wait_s = self._interval_s
                    else:
                        self._failures += 1
                        self._last_error = str(res.get("error") or "校准失败")
                        wait_s = _retry_delay(self._failures, self._interval_s)
                    self._state = "waiting"
                    self._next_wait_s = wait_s
                    self._next_mono = time.monotonic() + wait_s
                    notify = self._notify
                self._first_ready.set()
                if notify is not None:
                    try:
                        notify(res, trigger)
                    except Exception:         # noqa: BLE001
                        pass
                reason = self._wait(wait_s, poll_window=True)
                if reason == "stop":
                    break
                trigger = reason or "interval"
        finally:
            with self._lock:
                self._running = False
                if self._state != "stopped":
                    self._state = "stopped"
                self._next_mono = None

    def _sync_once(self):
        # 手动入口与自动循环共用同一把锁，避免两路 NTP 同时采样本。
        if not sync_lock.acquire(timeout=10.0):
            return {"ok": False, "error": "另一个校时还在进行"}
        try:
            with self._lock:
                servers = list(self._servers) if self._servers else None
                samples = self._samples
            return sync(servers=servers, samples=samples)
        except Exception as e:                    # noqa: BLE001
            _note_error(f"{type(e).__name__}: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            sync_lock.release()

    def _window_present(self):
        if self._window_probe is None:
            return False
        try:
            return bool(self._window_probe())
        except Exception:                         # noqa: BLE001
            return False

    def _wait(self, seconds, poll_window=True):
        """可被 stop/trigger 打断的等待；窗口出现时返回 "window"。"""
        deadline = time.monotonic() + max(0.0, float(seconds))
        last_poll = time.monotonic()
        prev_win = self._window_present() if poll_window else None
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= deadline:
                return ""
            if poll_window and now - last_poll >= AUTO_WINDOW_POLL_S:
                last_poll = now
                cur = self._window_present()
                if cur and not prev_win:
                    return "window"
                prev_win = cur
            self._wake.wait(min(0.5, max(0.0, deadline - now)))
            if self._wake.is_set():
                self._wake.clear()
                with self._lock:
                    reason = self._wake_reason
                    self._wake_reason = ""
                if self._stop.is_set():
                    return "stop"
                if reason:
                    return reason
        return "stop"


_auto = _AutoSync()


def auto_start(servers=None, samples=4, interval_s=None,
               notify=None, busy=None, window_probe=None):
    """启动实时校准（幂等）。返回 True=新起了线程，False=已在跑并补校一次。"""
    return _auto.start(servers=servers, samples=samples, interval_s=interval_s,
                       notify=notify, busy=busy, window_probe=window_probe)


def auto_stop(timeout=3.0):
    _auto.stop(timeout=timeout)


def auto_status():
    return _auto.status()


def auto_trigger(reason="manual"):
    """立刻补校一次；后台没在跑时返回 False（由调用方走同步路径）。"""
    return _auto.trigger(reason=reason)


def auto_wait_first(timeout=3.0):
    """等首轮自动校准完成；没在跑/已完成立刻返回。"""
    return _auto.wait_first(timeout)


def auto_note_result(res):
    """手动「立即重校」的结果回灌给后台调度（成功就清零失败计数）。"""
    try:
        _auto.note_result(res)
    except Exception:                             # noqa: BLE001
        pass


def reset():
    """把模块级校时状态清回初始值（测试隔离用，生产不调用）。

    `_state` 是模块级单例：`sync()` 写、`offset()/info()` 读。测试里只要
    前一个用例校过时（哪怕用的是假 socket），后一个用例 `info()` 就会读到
    上一轮的 offset / samples / server —— 随机顺序下表现成「时间同步用例
    时绿时红」，顺序跑却全绿。测试侧由 `tests/order_isolation.py` 的
    autouse 夹具在每个用例前调用；真实进程只校一次时，这份状态本来就该
    一直保留，所以不能用它做业务重置。
    """
    with _lock:
        _state.update(offset=0.0, delay=None, server=None, at=None,
                      at_mono=None, samples=[], last_error="",
                      degraded=False, warning="", drift_ppm=0.0)
