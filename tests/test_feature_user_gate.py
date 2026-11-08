"""人工操作让路（UserGate）—— 不动真实光标，只用回调链验证语义。

1. 有人工活动 → gate 必须停住（≈1.5s），并且只记一条暂停；
2. 静默期满 → 立刻放行，且不重复等待；
3. 期间状态不丢（同一闸门对象、暂停段计数不变）；
4. 连续活动不叠加出多条暂停段；样本数 = 暂停段数。
"""
import time

from core.input import UserGate


def test_activity_pauses_injection_for_quiet_window():
    g = UserGate(quiet_s=0.25)          # 用短静默期跑用例，语义相同
    g.note()
    t0 = time.perf_counter()
    waited = g.gate()
    assert waited >= 200, f"注入没停住：{waited:.1f}ms"
    assert g.pauses == 1
    assert g.samples and 0 <= g.samples[0] < 200


def test_resume_after_quiet_needs_no_second_wait():
    g = UserGate(quiet_s=0.15)
    g.note()
    g.gate()
    t0 = time.perf_counter()
    waited = g.gate()
    assert waited <= 1.0, f"静默期满还在等：{waited:.1f}ms"
    assert g.pauses == 1, "没有新动作却多记了一段暂停"


def test_repeated_activity_one_pause_per_burst():
    g = UserGate(quiet_s=0.12)
    for _ in range(5):
        g.note()
        time.sleep(0.01)
    g.gate()
    assert g.pauses == 1, f"一次连续动作被拆成 {g.pauses} 段"
    assert g.events == 5


def test_state_survives_pause():
    g = UserGate(quiet_s=0.1)
    g.note()
    wait1 = g.gate()
    g.note()
    wait2 = g.gate()
    assert wait1 >= 80 and wait2 >= 80
    assert g.quiet_s == 0.1, "闸门参数被改写了"
    assert g.waited_ms >= wait1 + wait2


def test_stats_shape():
    g = UserGate()
    g.note()
    g.gate()
    st = g.stats()
    assert st["quiet_s"] == 1.5
    assert st["samples"] == 1 and st["p50_ms"] is not None
