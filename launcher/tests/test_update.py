"""更新链路：正常包通过；哈希不符 / 签名不符 / 路径穿越 都必须拒。

所有落地只发生在 pytest 的 tmp_path 沙箱里：启动器从
<tmp>/sand/launcher/SjzLauncherTest.exe 跑，AppPaths.Root 就是 <tmp>/sand。
"""
import ctypes
import hashlib
import json
import zipfile
from ctypes import wintypes

import pytest

from conftest import run_sandbox, set_config, sign


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def write_version(sb, zip_path, sha, version="9.9.9", tamper=None):
    payload = {"v": 1, "version": version, "url": str(zip_path),
               "sha256": sha, "notes": "测试包"}
    blob = sign(payload)
    if tamper:
        blob["payload"] = dict(blob["payload"], **tamper)     # 签完再改
    path = sb["root"] / "version.json"
    path.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    set_config(sb, update_url=str(path))
    return path


@pytest.fixture
def sb(sandbox):
    set_config(sandbox, update_url="")
    return sandbox


def test_update_applies_after_signature_and_hash_check(sb, fp):
    pkg = sb["root"] / "pkg.zip"
    _, sha = make_zip(pkg, {"payload.txt": "hello", "newdir/marker.txt": "v2"})
    write_version(sb, pkg, sha)
    code, events, raw = run_sandbox(sb, ["--update-only"])
    ev = [e for e in events if e.get("status")]
    assert code == 0 and ev, raw
    r = ev[0]
    assert r["status"] == "updated" and r["ok"] is True, raw
    assert (sb["root"] / "newdir" / "marker.txt").read_text() == "v2"
    assert (sb["root"] / "payload.txt").read_text() == "hello"
    # 受保护目录原样保留
    assert (sb["root"] / "launcher" / "SjzLauncherTest.exe").exists()
    assert (sb["data"] / "update_backup").exists()


def test_update_rejects_hash_mismatch(sb, fp):
    pkg = sb["root"] / "pkg.zip"
    _, sha = make_zip(pkg, {"boom.txt": "x"})
    bad = "0" * 64
    write_version(sb, pkg, bad)
    code, events, raw = run_sandbox(sb, ["--update-only"])
    r = [e for e in events if e.get("status")][0]
    assert code == 1 and r["status"] == "rejected", raw
    assert "SHA-256" in r["reason"] or "哈希" in r["reason"]
    assert not (sb["root"] / "boom.txt").exists()
    # 期望值没错、只是对不上，顺带证明比较确实生效
    assert sha != bad


def test_update_rejects_signature_mismatch(sb, fp):
    pkg = sb["root"] / "pkg.zip"
    _, sha = make_zip(pkg, {"boom.txt": "x"})
    write_version(sb, pkg, sha, tamper={"version": "6.6.6"})
    code, events, raw = run_sandbox(sb, ["--update-only"])
    r = [e for e in events if e.get("status")][0]
    assert code == 1 and r["status"] == "rejected", raw
    assert "验签失败" in r["reason"]
    assert not (sb["root"] / "boom.txt").exists()


def test_update_rejects_zip_slip(sb, fp):
    pkg = sb["root"] / "evil.zip"
    _, sha = make_zip(pkg, {"..\\evil.txt": "pwn", "ok.txt": "fine"})
    write_version(sb, pkg, sha)
    code, events, raw = run_sandbox(sb, ["--update-only"])
    r = [e for e in events if e.get("status")][0]
    assert code == 1 and r["status"] == "failed", raw
    assert "路径穿越" in r["reason"]
    assert not (sb["root"].parent / "evil.txt").exists()
    assert not (sb["root"] / "evil.txt").exists()
    assert not (sb["root"] / "ok.txt").exists()          # 整包放弃，不半落地


def test_update_never_runs_before_license(sb, fp):
    """无卡密时更新必须停在验签之前：不下载、不落地。"""
    pkg = sb["root"] / "pkg.zip"
    _, sha = make_zip(pkg, {"never.txt": "should not land"})
    write_version(sb, pkg, sha)
    code, events, raw = run_sandbox(
        sb, ["--headless", "--seconds", "1", "--ignore-preflight-fail"])
    by = {e["event"]: e for e in events}
    assert code != 0, raw
    assert by["license"]["ok"] is False and by["license"]["status"] == "none"
    assert "update" not in by or by["update"].get("status") not in ("updated",)
    assert not (sb["root"] / "never.txt").exists()


# ---------------------------------------------------------------- 失败演练

def test_download_interrupted_leaves_old_version_runnable(sb, fp):
    """下载得到的包不完整（截断的 zip）：哈希能对上也没用，解压必须失败，
    旧版一个字节都不能动，仍可继续用。"""
    good = sb["root"] / "pkg.zip"
    make_zip(good, {"payload.txt": "v2"})
    (sb["root"] / "payload.txt").write_text("v1", encoding="utf-8")
    half = sb["root"] / "half.zip"
    half.write_bytes(good.read_bytes()[: max(8, len(good.read_bytes()) // 2)])
    sha_half = hashlib.sha256(half.read_bytes()).hexdigest()   # 哈希一致，模拟传输截断
    write_version(sb, half, sha_half)

    code, events, raw = run_sandbox(sb, ["--update-only"])
    r = [e for e in events if e.get("status")][0]
    assert code == 1 and r["status"] == "failed", raw
    assert (sb["root"] / "payload.txt").read_text(encoding="utf-8") == "v1"
    assert not (sb["root"] / "newdir").exists()
    # 半包不能留在数据目录里冒充完整包
    left = list(sb["data"].glob("update_*.zip")) + list(sb["data"].glob("update_*.part"))
    assert not left, left

    # 再跑一次也是同样的失败：没有留下脏状态影响下一次
    code2, events2, raw2 = run_sandbox(sb, ["--update-only"])
    assert code2 == 1, raw2


def test_update_fails_when_target_locked(sb, fp):
    """替换瞬间目标文件被占用：失败但不破坏旧文件。"""
    pkg = sb["root"] / "pkg.zip"
    _, sha = make_zip(pkg, {"payload.txt": "v2"})
    target = sb["root"] / "payload.txt"
    target.write_text("v1", encoding="utf-8")

    k32 = ctypes.windll.kernel32
    FILE_SHARE_NONE = 0
    OPEN_EXISTING = 3
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(str(target), 0x80000000, FILE_SHARE_NONE, None,
                        OPEN_EXISTING, 0x80, None)
    assert h and h != wintypes.HANDLE(-1).value, "没能锁住目标文件"
    try:
        write_version(sb, pkg, sha)
        code, events, raw = run_sandbox(sb, ["--update-only"])
        r = [e for e in events if e.get("status")][0]
        assert code == 1 and r["status"] == "failed", raw
        assert r["reason"], raw
    finally:
        k32.CloseHandle(h)

    assert target.read_text(encoding="utf-8") == "v1"
    # 解锁后重试仍能成功（说明失败没留下脏状态）
    code, events, raw = run_sandbox(sb, ["--update-only"])
    r = [e for e in events if e.get("status")][0]
    assert code == 0 and r["status"] == "updated", raw
    assert target.read_text(encoding="utf-8") == "v2"


def test_rollback_cli_restores_previous_version(sb, fp):
    """成功更新后保留上一版；--rollback 能整份退回。"""
    (sb["root"] / "payload.txt").write_text("v1", encoding="utf-8")
    pkg = sb["root"] / "pkg.zip"
    _, sha = make_zip(pkg, {"payload.txt": "v2", "newdir/marker.txt": "v2"})
    write_version(sb, pkg, sha)

    code, events, raw = run_sandbox(sb, ["--update-only"])
    assert code == 0 and [e for e in events if e.get("status")][0]["status"] == "updated", raw
    assert (sb["root"] / "payload.txt").read_text(encoding="utf-8") == "v2"

    code, events, raw = run_sandbox(sb, ["--rollback"])
    r = [e for e in events if e.get("status")][0]
    assert code == 0 and r["status"] == "rolledback", raw
    assert (sb["root"] / "payload.txt").read_text(encoding="utf-8") == "v1"
    assert (sb["root"] / "launcher" / "SjzLauncherTest.exe").exists()

    # 没有备份时回滚要明确失败，而不是乱删
    code, events, raw = run_sandbox(sb, ["--rollback"])
    assert code == 1, raw


def test_plain_version_json_and_notes(sb, fp):
    """明文 version.json（update_require_sig=false）可用，并回报 notes。"""
    pkg = sb["root"] / "pkg.zip"
    _, sha = make_zip(pkg, {"payload.txt": "v2"})
    plain = {"version": "9.9.9", "url": str(pkg), "sha256": sha,
             "notes": "修复了回滚路径的问题"}
    vj = sb["root"] / "version.json"
    vj.write_text(json.dumps(plain, ensure_ascii=False), encoding="utf-8")
    set_config(sb, update_url=str(vj), update_require_sig=False)

    code, events, raw = run_sandbox(sb, ["--update-only"])
    r = [e for e in events if e.get("status")][0]
    assert code == 0 and r["status"] == "updated", raw
    assert r["notes"] == "修复了回滚路径的问题", raw
    assert (sb["root"] / "payload.txt").read_text(encoding="utf-8") == "v2"

    # 默认配置（强制签名）下同样的明文必须被拒
    set_config(sb, update_url=str(vj), update_require_sig=True)
    code, events, raw = run_sandbox(sb, ["--update-only"])
    r = [e for e in events if e.get("status")][0]
    assert code == 1 and r["status"] == "rejected" and "payload/sig" in r["reason"], raw
