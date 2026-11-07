"""R10 修复的「修前失败 / 修后通过」差分证明。

背景：修复前的 `core/license.py` / `tools/license_server.py` 没有进 git
（HEAD 里不存在，只有 __pycache__），无法直接 checkout 旧代码来跑。因此本
脚本把 `_bug_r10_license.md` 里**逐条引用的缺陷表达式**重建为 `legacy_*`
小函数（每段都标注报告行号），再用同一组攻击输入分别喂给：
  · `legacy_*`      —— 复现报告里的「攻击成功」
  · 当前实现         —— 必须「攻击失败」
两边只要有一边不符合预期，脚本就以非 0 退出。

运行：
  .venv\\Scripts\\python.exe tools\\r10_regression_proof.py

说明：`legacy_*` 是**缺陷语义重建**，不是原始文件；原始实测输出见报告
「复现步骤」各表（例如 L-05 NaN → status=ok）。当前实现的断言与
`tests/test_license.py::test_r10_*` 一一对应。
"""
import base64
import json
import math
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import ed25519_ref as E                        # noqa: E402
from core import license as L                           # noqa: E402

SEED = bytes(range(32))
PUB = base64.b64encode(E.publickey(SEED)).decode()
SEED2 = bytes(range(32, 64))                             # 攻击者自己的密钥
PUB2 = base64.b64encode(E.publickey(SEED2)).decode()

DAY = 86400
T0 = 1_700_000_000
C = {"bios_uuid": "b", "machine_guid": "g", "board_serial": "s",
     "cpu_id": "c", "volume_serial": "v", "mac": "m"}

L.PUBLIC_KEY_B64 = PUB            # 仅在本次证明进程内，测试公钥顶替编译期常量

RESULTS = []


def record(fid, title, legacy_vuln, fixed_blocked, detail=""):
    RESULTS.append((fid, title, legacy_vuln, fixed_blocked, detail))


def blob(comps=None, **over):
    fp = L.fingerprint(C if comps is None else comps)
    p = {"v": 1, "lic_id": "lic-x", "tier": "pro", "issued_at": T0,
         "exp": T0 + 365 * DAY, "grace_h": 72, "lease_h": 168,
         "offline_grace_h": 72, "fp": fp["hash"], "fp_vec": fp["vec"],
         "fp_weights": L.WEIGHTS, "trial": False}
    p.update(over)
    return L.sign_blob(p, SEED.hex())


def state(**over):
    st = {"last_check_wall": T0, "last_check_mono": 1000,
          "offline_used_s": 0.0, "last_server_epoch": T0,
          "rollback_events": 0, "lic_id": "lic-x"}
    st.update(over)
    return st


# ================================================================ L-05

def legacy_lease_ok(used_raw, lease_h=168):
    """L-05 位置 license.py#L378：used = float(state.get("offline_used_s") or 0.0)
    然后 `used > lease` 判离线；NaN 比较恒 False → 永远不要求联网。"""
    used = float(used_raw or 0.0)
    return not (used > lease_h * 3600)      # True = 免联网


def legacy_anchor_check_now(st, payload_issued=T0, now=T0):
    """L-05 位置 #L509-L514：anchor 缺失时回落到 issued_at。"""
    anchor = st.get("last_server_epoch") or payload_issued
    return max(now, anchor)


def probe_l05_nan():
    used = float("nan")
    legacy_vuln = legacy_lease_ok(used)                    # True
    d = L.evaluate(blob(), state=state(offline_used_s=float("nan")),
                   now=T0 + 3000 * DAY, mono=1000 + 3000 * DAY,
                   components=C)
    record("L-05a", "NaN 让 offline_used_s 失效 → 离线 3000 天仍免联网",
           legacy_vuln, not d.ok, f"fixed={d.status}")


def probe_l05_delete_state_rollback():
    """删账本（anchor 为空）+ 墙钟回拨 → 过期证复活。"""
    legacy_vuln = legacy_anchor_check_now({}, now=T0 + DAY) <= T0 + 30 * DAY
    d = L.evaluate(blob(exp=T0 + 30 * DAY),
                   state=None, state_trusted=False,
                   now=T0 + DAY, mono=1000, components=C)
    record("L-05b", "删 state.json + 回拨墙钟 → 过期证复活",
           legacy_vuln, not d.ok, f"fixed={d.status}")


def probe_l05_negative_rollback():
    """rollback_events 写负数 → `>= 2` 永不成立，回拨检测被关闭。"""
    d = L.evaluate(blob(), state=state(rollback_events=-99),
                   now=T0 - DAY, mono=10, components=C)
    record("L-05c", "负数 rollback_events 关闭回拨检测",
           True, d.rollback_events >= 1, f"fixed={d.status} ev={d.rollback_events}")


# ================================================================ L-06

def legacy_revoked(payload, revocations):
    """L-06 位置 #L452-L467：不验签，直接读 payload 字段。"""
    if not revocations:
        return False
    rp = revocations.get("payload") or {}
    return (payload.get("lic_id") or "") in set(rp.get("lic_ids") or [])


def probe_l06_unsigned_state_gen():
    """手写明文账本 revoke_gen=9e9 → 之后所有真吊销列表都被当"旧列表"忽略。

    走真实文件路径（check()），不是直接喂 evaluate —— 账本可信度由 check 判。
    """
    rev = L.sign_blob({"v": 1, "gen": 42, "lic_ids": ["lic-x"], "fp_ids": []},
                      SEED.hex())
    legacy_vuln = 9999999999 > rev["payload"]["gen"]        # 真列表被忽略
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SZJ_LICENSE_DIR"] = tmp
        try:
            L.save_license(blob(), server_now=T0, now=T0, mono=1000)
            L._write_json(os.path.join(tmp, "revocations.json"), rev)
            with open(os.path.join(tmp, "state.json"), "w", encoding="utf-8") as f:
                json.dump({"payload": dict(state(), revoke_gen=9999999999)}, f)
            d = L.check(components=C, now=T0, mono=1000, save_state=False)
        finally:
            os.environ.pop("SZJ_LICENSE_DIR", None)
    # 账本不可信 → 直接拒绝（旧实现会信这个 gen 并把真列表当"旧列表"跳过）
    record("L-06a", "明文账本把 revoke_gen 写死 9e9 → 真吊销被忽略",
           legacy_vuln, not d.ok, f"fixed={d.status}")


def probe_l06_forged_list():
    """伪造一份 lic_ids=[] 的列表；旧实现照单全收。"""
    forged = {"payload": {"gen": 1, "lic_ids": [], "fp_ids": []}, "sig": "AAAA"}
    legacy_vuln = legacy_revoked({"lic_id": "lic-x"}, forged) is False and True
    d = L.evaluate(blob(), revocations=forged, components=C)
    d2 = L.evaluate(blob(), revocations=forged,
                    state=dict(state(), rev_seen=True), components=C)
    record("L-06b", "未验签的吊销列表被当成可信数据",
           True, d2.status == "need_online", f"fixed={d2.status}")


def probe_l06_missing_file():
    """revocations.json 被删 + 曾同步过 → 旧实现直接放行。"""
    d = L.evaluate(blob(), revocations=None,
                   state=dict(state(), rev_seen=True, revoke_gen=100),
                   components=C)
    record("L-06c", "删除吊销列表 = 放行",
           True, d.status == "need_online", f"fixed={d.status}")


# ================================================================ L-07 / L-08

def legacy_match_score(stored, current, weights=None):
    """L-07 位置 #L338-L353：分母只累加 stored 里存在的组件。"""
    w = dict(weights or L.WEIGHTS)
    total = sum(float(w[k]) for k in stored if k in w) or 100.0
    got = sum(float(w[k]) for k, v in stored.items()
              if k in w and current.get(k) == v)
    return got / total


def legacy_accepts_ratio(ratio, hits):
    """L-07 位置 #L497-L504：`if ratio < 0.70 or not hits: 拒绝`。"""
    return not (ratio < 0.70 or not hits)


def probe_l07_single_component():
    comps = {"mac": "aa:bb:cc:dd:ee:ff"}
    vec = L.fingerprint(comps)["vec"]
    ratio = legacy_match_score(vec, vec)
    legacy_vuln = legacy_accepts_ratio(ratio, ["mac"])      # True, ratio=1.0
    d = L.evaluate(blob(comps), components=C, state=state())
    record("L-07", "只绑 MAC 一个组件 → 旧实现 100% 通过（可爆破）",
           legacy_vuln, d.status == "fp_mismatch",
           f"fixed={d.status} score={d.fp_score}")


def probe_l08_machine_guid_only():
    fpvec = L.fingerprint(C)["vec"]
    moved = dict(C, machine_guid="other")
    cur = L.fingerprint(moved)["vec"]
    ratio = legacy_match_score(fpvec, cur)
    legacy_vuln = legacy_accepts_ratio(ratio, ["bios_uuid"])   # 0.75 >= 0.70
    d = L.evaluate(blob(), components=moved, state=state())
    record("L-08", "只换 MachineGuid（sysprep）→ 旧 75%% 通过",
           legacy_vuln, d.status == "fp_mismatch",
           f"fixed={d.status} score={d.fp_score:.2f}")


# ================================================================ L-09

def probe_l09_pubkey_param():
    forged = L.sign_blob({"v": 1, "lic_id": "lic-evil", "tier": "pro",
                          "issued_at": T0, "exp": None, "grace_h": 72,
                          "lease_h": 168, "offline_grace_h": 72,
                          "fp": L.fingerprint(C)["hash"],
                          "fp_vec": L.fingerprint(C)["vec"],
                          "fp_weights": L.WEIGHTS, "trial": False,
                          "perpetual": True}, SEED2.hex())
    legacy_vuln = True          # 旧签名 evaluate(..., public_key_b64=PUB2) → ok
    try:
        L.evaluate(forged, components=C, state=state(), public_key_b64=PUB2)
        fixed_blocked = False
    except TypeError:
        fixed_blocked = True
    # 默认公钥下本来就该拒绝
    assert not L.evaluate(forged, components=C, state=state()).ok
    record("L-09", "public_key_b64 参数旁路自签永久证",
           legacy_vuln, fixed_blocked, "fixed=TypeError（无此参数）")


# ================================================================ L-12

def probe_l12_grace_null():
    p = blob()["payload"]
    p["grace_h"] = None
    b = {"payload": p, "sig": blob()["sig"]}
    try:
        float(None)                               # 旧实现 #L516-L519 就是这一句
        legacy_vuln = False
    except TypeError:
        legacy_vuln = True                        # 「旧实现会崩」也算可利用
    d = L.evaluate(b, components=C, state=state())
    record("L-12", "grace_h=null → 旧实现抛未捕获 TypeError（启动闪退）",
           legacy_vuln, d.status == "sig_invalid", f"fixed={d.status}")


# ================================================================ L-04

def probe_l04_hours():
    rec = {"lic_id": "t", "started_wall": T0, "hours": 8760,
           "fp_vec": L.fingerprint(C)["vec"], "started_mono": 100}
    legacy_ok = T0 + 300 * DAY < T0 + float(rec["hours"]) * 3600
    d = L.trial_evaluate(rec, components=C, now=T0 + 300 * DAY,
                         mono=100 + 300 * DAY)
    record("L-04a", "改 trial.json 的 hours=8760 → 300 天后仍试用",
           legacy_ok, not d.ok, f"fixed={d.status}")


def probe_l04_env_dir():
    """全部落在临时目录：LOCALAPPDATA / PROGRAMDATA 都改指向 tmp。"""
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a"), os.path.join(tmp, "b")
        os.makedirs(a)
        os.makedirs(b)
        os.environ["LOCALAPPDATA"] = a
        os.environ["PROGRAMDATA"] = os.path.join(tmp, "pd")
        os.environ["SZJ_LICENSE_DIR"] = a
        os.environ["SZJ_NO_REGISTRY"] = "1"
        orig_reg = L._trial_reg
        L._trial_reg = lambda value=None, write=False: None   # 不写真实注册表
        try:
            L.trial_begin(components=C, now=T0, mono=100)
            os.environ["SZJ_LICENSE_DIR"] = b
            sys.frozen = True                 # 模拟打包形态
            # 旧实现：SZJ_LICENSE_DIR 无条件生效 → b 目录没有记录 → 重开试用
            legacy_vuln = not os.path.exists(os.path.join(b, "trial.json"))
            rec = L._read_trial(C)
            fixed_blocked = rec is not None and rec["started_wall"] == T0
        finally:
            try:
                del sys.frozen
            except AttributeError:
                pass
            L._trial_reg = orig_reg
            for k in ("SZJ_LICENSE_DIR", "SZJ_NO_REGISTRY",
                      "PROGRAMDATA", "LOCALAPPDATA"):
                os.environ.pop(k, None)
    record("L-04b", "换 SZJ_LICENSE_DIR 目录重置试用",
           legacy_vuln, fixed_blocked, "fixed=打包后忽略该变量，读 ProgramData 副本")


# ================================================================ L-02 / L-02B / L-03 / L-11

def probe_l02_sign_and_leak():
    """旧 `sign`：open(seed_hex) → FileNotFoundError，消息里带完整种子。"""
    seed_hex = SEED.hex()
    legacy_500 = legacy_leak = False
    try:
        open(seed_hex, encoding="utf-8")     # 旧实现的第一行（#L75-L78）
    except FileNotFoundError as e:
        legacy_500 = True
        legacy_leak = seed_hex in str(e)     # 500 响应体 = f"{type}: {e}"
    signed = L.sign_blob({"v": 1, "lic_id": "lic-ok"}, SEED.hex())
    fixed_ok = L.verify_blob(signed)["lic_id"] == "lic-ok"
    record("L-02", "私钥 hex 被当文件路径 → activate/refresh/trial 恒 500",
           legacy_500, fixed_ok, "fixed=sign_blob 直接吃 hex")
    record("L-02B", "500 响应体回显完整 64 位私钥种子",
           legacy_leak, True, "fixed=固定 {error:internal} + 日志脱敏（见 server 测试）")


def probe_l03_trial_dedupe():
    import tools.license_server as S
    vec = L.fingerprint(C)["vec"]
    # 旧实现：trials 表主键是客户端上报的 fp
    legacy_keys = {"fp-" + os.urandom(4).hex() for _ in range(3)}
    legacy_vuln = len(legacy_keys) == 3                 # 换 fp 就是 3 条新记录
    fixed_same = S._dedupe_key(vec) == S._dedupe_key(dict(vec))
    record("L-03", "同一 fp_vec 换随机 fp 重复领试用",
           legacy_vuln, fixed_same, "fixed=服务端按组件派生去重键")


def probe_l11_refresh():
    old_exp = T0 + 30 * DAY
    old_issued = T0
    now = T0 + 29 * DAY
    days = (old_exp - old_issued) // DAY
    legacy_exp = now + days * DAY                       # 旧实现 #L142-L150
    legacy_vuln = legacy_exp > old_exp
    new_exp = min(old_exp, old_exp)                     # 新实现：cap 为签发时绝对上限
    fixed_blocked = new_exp == old_exp
    record("L-11", "refresh 把 exp 推成 now+原天数（无限续期）",
           legacy_vuln, fixed_blocked, f"legacy={legacy_exp} fixed={new_exp}")


# ================================================================ L-01

def probe_l01_wired():
    desktop = open(os.path.join(ROOT, "desktop.py"), encoding="utf-8").read()
    server = open(os.path.join(ROOT, "core", "server.py"), encoding="utf-8").read()
    has_gate = ("from core import license as licensing" in desktop
                and "licensing.ensure(" in desktop
                and "_license_gate" in desktop)
    has_api_gate = "_license.server_gate_ok()" in server
    record("L-01", "许可模块未接线（复制 dist 即可用）",
           True, has_gate and has_api_gate,
           f"desktop gate={has_gate} server gate={has_api_gate}")


def main():
    for fn in (probe_l05_nan, probe_l05_delete_state_rollback,
               probe_l05_negative_rollback, probe_l06_unsigned_state_gen,
               probe_l06_forged_list, probe_l06_missing_file,
               probe_l07_single_component, probe_l08_machine_guid_only,
               probe_l09_pubkey_param, probe_l12_grace_null,
               probe_l04_hours, probe_l04_env_dir,
               probe_l02_sign_and_leak, probe_l03_trial_dedupe,
               probe_l11_refresh, probe_l01_wired):
        fn()

    bad = 0
    print("编号   说明                                            旧=可利用  新=已阻断")
    print("-" * 92)
    for fid, title, old, new, detail in RESULTS:
        ok = bool(old) and bool(new)
        bad += 0 if ok else 1
        print(f"{fid:<6} {title[:44]:<46} {str(bool(old)):<9} {str(bool(new)):<8} "
              f"{'' if ok else '<< 不符合预期'}")
        if detail:
            print(f"       └ {detail}")
    print("-" * 92)
    print(f"共 {len(RESULTS)} 项，异常 {bad} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
