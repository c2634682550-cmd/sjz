"""启动器自测公共件：跑 SjzLauncherTest.exe、签卡密、造沙箱。

约定：所有测试只碰 launcher\\ 内的文件；临时数据写 pytest tmp_path，
LOCALAPPDATA 用环境变量指到 tmp_path，绝不读写真机的
%LOCALAPPDATA%\\SJZ-Control（用户的卡密不会被动到）。
"""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]          # ...\dafors
LAUNCHER = ROOT / "launcher"
EXE = LAUNCHER / "SjzLauncherTest.exe"
GUI_EXE = LAUNCHER / "SjzLauncher.exe"

sys.path.insert(0, str(ROOT))

import core.license as lic                        # noqa: E402

# 测试密钥每轮现生成，只在内存里：不落盘、不进夹具，跑完随进程销毁。
# SjzLauncherTest.exe 是 SJZ_TEST_BUILD 构建，允许用环境变量注入对应公钥；
# 发布版 SjzLauncher.exe 没有这条通路。
TEST_SEED_HEX = lic.new_private_key_hex()
TEST_PUB_B64 = lic.public_key_b64_from_priv(TEST_SEED_HEX)


def seed_hex() -> str:
    return TEST_SEED_HEX


@pytest.fixture(autouse=True)
def _pin_test_public_key(monkeypatch):
    """进程内的 Python 侧（sign_blob/verify_blob）也用同一把临时公钥。"""
    monkeypatch.setattr(lic, "PUBLIC_KEY_B64", TEST_PUB_B64)


def run_cli(args, env=None, timeout=60):
    """跑命令行模式的启动器，返回 (退出码, 解析后的 JSON, 原始输出)。"""
    e = dict(os.environ)
    e.pop("PYTHONPATH", None)
    e["SJZ_LAUNCHER_PUBKEY_B64"] = TEST_PUB_B64
    if env:
        e.update({k: str(v) for k, v in env.items()})
    p = subprocess.run([str(EXE)] + list(args), capture_output=True, text=True,
                       timeout=timeout, env=e, cwd=str(ROOT))
    data = None
    for line in p.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
    return p.returncode, data, (p.stdout + p.stderr).strip()


@pytest.fixture(scope="session")
def fp():
    """启动器看到的真实指纹（本机）。"""
    code, data, raw = run_cli(["--fp"])
    assert code == 0 and data and "vec" in data, raw
    return data


def payload_for(vec, **over):
    now = int(time.time())
    p = {
        "v": 1,
        "lic_id": "TEST-0001",
        "tier": "pro",
        "trial": False,
        "issued_at": now - 60,
        "exp": now + 86400,
        "grace_h": 72,
        "fp_vec": dict(vec),
        "fp_weights": dict(lic.WEIGHTS),
        "perpetual": False,
    }
    p.update(over)
    return p


def sign(payload):
    return lic.sign_blob(payload, seed_hex())


def write_blob(path, payload, sig=None):
    blob = sign(payload) if sig is None else {"payload": payload, "sig": sig}
    path = Path(path)
    path.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    return path


def write_raw(path, text):
    path = Path(path)
    path.write_text(text, encoding="utf-8")
    return path


def blob_text(payload, sig):
    return json.dumps({"payload": payload, "sig": sig}, ensure_ascii=False)


# ---------------------------------------------------------------- 沙箱

@pytest.fixture
def sandbox(tmp_path):
    """假安装根：<tmp>\\sand\\launcher\\SjzLauncherTest.exe，AppPaths.Root = <tmp>\\sand。

    启动器按「exe 目录名是 launcher 就取父目录」定根，所以拷进去跑，
    更新链路的原子替换只会落在 <tmp>\\sand，不会碰真实项目目录。
    """
    sand = tmp_path / "sand"
    (sand / "launcher").mkdir(parents=True)
    for name in ("SjzLauncherTest.exe", "launcher.config.json"):
        src = LAUNCHER / name
        (sand / "launcher" / name).write_bytes(src.read_bytes())
    data = tmp_path / "localappdata"
    data.mkdir()
    return {"root": sand, "exe": sand / "launcher" / "SjzLauncherTest.exe",
            "data": data}


def sand_env(sandbox, **extra):
    e = {"LOCALAPPDATA": sandbox["data"], "SJZ_DATA_DIR": sandbox["data"]}
    e.update(extra)
    return e


def run_sandbox(sandbox, args, timeout=80, **env):
    e = dict(os.environ)
    e.pop("PYTHONPATH", None)
    e["SJZ_LAUNCHER_PUBKEY_B64"] = TEST_PUB_B64
    e.update({k: str(v) for k, v in sand_env(sandbox, **env).items()})
    p = subprocess.run([str(sandbox["exe"])] + list(args), capture_output=True,
                       text=True, timeout=timeout, env=e,
                       cwd=str(sandbox["root"]))
    events = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return p.returncode, events, (p.stdout + p.stderr).strip()


def set_config(sandbox, **kv):
    cfg = json.loads((LAUNCHER / "launcher.config.json").read_text(encoding="utf-8"))
    cfg.update(kv)
    (sandbox["root"] / "launcher" / "launcher.config.json").write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def put_license(sandbox, payload):
    """把卡密放进沙箱数据目录（AppPaths.DataDir = %SJZ_DATA_DIR%）。"""
    d = sandbox["data"]
    d.mkdir(parents=True, exist_ok=True)
    path = d / "license.json"
    write_blob(path, payload)
    return path


def pid_alive(pid) -> bool:
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000, False, int(pid))
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == 0x00000102
    finally:
        k32.CloseHandle(h)
