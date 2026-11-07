"""显卡 TDR 识别 + 进程消失后的自动恢复预算。

真机 04:28:34 取证：System 日志 `nvlddmkm` 153 连报 4 次 → UE Aftermath 出
GPU 崩溃报告 → CrashSight 落盘 → 进程消失。这不是注入失败、也不是导航失败，
所以必须单独判成 `GPU_TDR` 并写明确日志，别混进「点击没生效」里。

对外开放三件事：
  · classify_gone()   —— 窗口/进程消失时判原因（GPU_TDR / UNKNOWN）
  · RecoveryLedger    —— 自动恢复预算，最多 2 次，第 3 次给固定文案
  · handle_window_gone() —— 判因 → 记录进度 → 拉起游戏 → 等窗口 → 交回续跑

本模块不发任何鼠标/键盘事件；只读日志、拉进程、等窗口。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime

TDR_PROVIDER = "nvlddmkm"
TDR_EVENT_ID = 153
LIVEKERNEL_CODE = "141"
MAX_AUTO_RECOVER = 2
GIVE_UP_TEXT = "显卡不稳定，请降低画质或更新驱动"

GAME_EXE_CANDIDATES = (
    r"C:\WeGameApps\rail_apps\DeltaForce(2001918)\DeltaForce\Binaries\Win64"
    r"\DeltaForceClient-Win64-Shipping.exe",
    r"D:\WeGameApps\rail_apps\DeltaForce(2001918)\DeltaForce\Binaries\Win64"
    r"\DeltaForceClient-Win64-Shipping.exe",
)

_SYS_Q = (
    "$t=(Get-Date).AddMinutes(-{m}); "
    "Get-WinEvent -FilterHashtable @{{LogName='System';ProviderName='nvlddmkm';"
    "Id=153;StartTime=$t}} -ErrorAction SilentlyContinue | "
    "Select-Object TimeCreated,Id,ProviderName,Message | ConvertTo-Json -Compress"
)
_APP_Q = (
    "$t=(Get-Date).AddMinutes(-{m}); "
    "Get-WinEvent -FilterHashtable @{{LogName='Application';StartTime=$t}} "
    "-ErrorAction SilentlyContinue | "
    "Where-Object {{ $_.Message -match 'LiveKernelEvent' -and "
    "$_.Message -match '141' }} | "
    "Select-Object TimeCreated,Id,ProviderName,Message | ConvertTo-Json -Compress"
)


def _data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "dafors")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _decode(raw):
    """PowerShell 5 在中文系统上是 GBK 输出，先按 utf-8 试，失败退 gbk。"""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _power_shell(script, timeout=15):
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:                       # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "items": []}
    out = _decode(r.stdout or b"").strip()
    err = _decode(r.stderr or b"").strip()
    if not out:
        return {"error": err[:200], "items": []}
    try:
        data = json.loads(out)
    except ValueError:
        return {"error": "输出不是 JSON", "items": [], "raw": out[:300]}
    items = data if isinstance(data, list) else [data]
    return {"error": "", "items": [i for i in items if isinstance(i, dict)]}


def tdr_events(minutes=10, runner=None):
    """近 N 分钟内 System 的 nvlddmkm 153 与 LiveKernelEvent 141。"""
    run = runner or _power_shell
    sys_res = run(_SYS_Q.format(m=minutes))
    app_res = run(_APP_Q.format(m=minutes))
    out = []
    for i in (sys_res.get("items") or []):
        out.append({"log": "System", "time": i.get("TimeCreated"),
                    "id": i.get("Id"), "provider": i.get("ProviderName"),
                    "message": (i.get("Message") or "")[:200]})
    for i in (app_res.get("items") or []):
        out.append({"log": "Application", "time": i.get("TimeCreated"),
                    "id": i.get("Id"), "provider": i.get("ProviderName"),
                    "message": (i.get("Message") or "")[:200]})
    return {"items": out,
            "system_error": sys_res.get("error", ""),
            "app_error": app_res.get("error", "")}


def classify_gone(minutes=10, runner=None):
    """窗口消失的原因：有 TDR 事件就是 GPU_TDR，否则 UNKNOWN。"""
    ev = tdr_events(minutes=minutes, runner=runner)
    items = ev["items"]
    tdr = [i for i in items
           if (str(i.get("provider") or "").lower() == TDR_PROVIDER
               and str(i.get("id")) == str(TDR_EVENT_ID))]
    livek = [i for i in items
             if LIVEKERNEL_CODE in (i.get("message") or "")
             and "LiveKernelEvent" in (i.get("message") or "")]
    cause = "GPU_TDR" if (tdr or livek) else "UNKNOWN"
    return {"cause": cause, "tdr_153": len(tdr), "livekernel_141": len(livek),
            "events": items[:8], "query": {k: ev[k] for k in
                                           ("system_error", "app_error")}}


def log_line(payload, path=None):
    """写一行 JSON 到 gpu_tdr.log，返回日志路径。"""
    path = path or os.path.join(_data_dir(), "logs", "gpu_tdr.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(payload, ts=datetime.now().isoformat(
                timespec="seconds")), ensure_ascii=False) + "\n")
    except OSError:
        pass
    return path


def record_progress(progress, path=None):
    """把当前进度落盘，恢复后用这份续跑。"""
    path = path or os.path.join(_data_dir(), "crash_progress.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(progress, ts=datetime.now().isoformat(
                timespec="seconds")), f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return path


def load_progress(path=None):
    path = path or os.path.join(_data_dir(), "crash_progress.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class RecoveryLedger:
    """自动恢复预算：最多 2 次，第 3 次给固定文案。持久化，重启控制台不清零。

    只有「真的拉起了游戏」才花掉一次；拉不起来不扣，避免重启失败几次后
    直接把预算耗到放弃。
    """

    def __init__(self, path=None, max_recover=MAX_AUTO_RECOVER):
        self.path = path or os.path.join(_data_dir(), "crash_recovery.json")
        self.max_recover = int(max_recover)
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                d = {}
        except (OSError, ValueError):
            d = {}
        d.setdefault("recoveries", 0)
        d.setdefault("history", [])
        return d

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    @property
    def used(self):
        return int(self.data.get("recoveries") or 0)

    def allow(self):
        return self.used < self.max_recover

    def spend(self, cause, ok=True):
        if not ok:
            return self.used
        self.data["recoveries"] = self.used + 1
        self.data["history"].append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "cause": cause, "n": self.data["recoveries"]})
        self.data["history"] = self.data["history"][-10:]
        self._save()
        return self.data["recoveries"]

    def reset(self):
        self.data = {"recoveries": 0, "history": []}
        self._save()

    def give_up_message(self):
        return "%s（已自动恢复 %d 次）" % (GIVE_UP_TEXT, self.used)


def find_exe():
    for p in GAME_EXE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def launch_game(exe=None, spawn=None):
    """拉起游戏进程。返回 {ok, pid, exe, why}。"""
    exe = exe or find_exe()
    if not exe:
        return {"ok": False, "pid": None, "exe": None,
                "why": "找不到游戏 exe"}
    if spawn is not None:
        return spawn(exe)
    try:
        p = subprocess.Popen([exe], cwd=os.path.dirname(exe))
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "pid": None, "exe": exe,
                "why": f"{type(e).__name__}: {e}"}
    return {"ok": True, "pid": p.pid, "exe": exe, "why": ""}


def wait_window(timeout=180.0, finder=None, sleep=time.sleep):
    """等游戏窗口出现，返回 hwnd 或 None。"""
    finder = finder or _default_window
    t0 = time.time()
    while time.time() - t0 < timeout:
        hwnd = finder()
        if hwnd:
            return hwnd
        sleep(1.0)
    return None


def _default_window():
    try:
        from .input import find_game_window
        return find_game_window()
    except Exception:                            # noqa: BLE001
        return None


def handle_window_gone(progress=None, say=print, minutes=10, runner=None,
                       ledger=None, launcher=launch_game, finder=None,
                       wait_s=180.0):
    """窗口/进程消失的统一处理入口。

    返回 {cause, action, message, hwnd, recoveries}：
      action = "resume"  已自动拉起，交回调度续跑
      action = "gave_up" 预算用完，固定文案，等人工
      action = "no_exe"  判成 GPU_TDR 但找不到 exe，需要人工拉起
    """
    led = ledger or RecoveryLedger()
    cls = classify_gone(minutes=minutes, runner=runner)
    cause = cls["cause"]
    if progress:
        record_progress(progress)
    log_line({"event": "window_gone", "cause": cause,
              "tdr_153": cls["tdr_153"],
              "livekernel_141": cls["livekernel_141"],
              "recoveries": led.used})
    if cause == "GPU_TDR":
        say("窗口消失原因：GPU_TDR（nvlddmkm 153 ×%d，LiveKernelEvent 141 ×%d）"
            % (cls["tdr_153"], cls["livekernel_141"]), "error")
    else:
        say("窗口消失原因：日志里没有 TDR 证据，判 UNKNOWN（按普通掉线处理）",
            "warn")

    if not led.allow():
        msg = led.give_up_message()
        say(msg, "error")
        log_line({"event": "give_up", "cause": cause,
                  "recoveries": led.used})
        return {"cause": cause, "action": "gave_up", "message": msg,
                "hwnd": None, "recoveries": led.used}

    say("自动恢复 %d/%d：拉起游戏并等窗口" % (led.used + 1, led.max_recover))
    res = launcher()
    if not res.get("ok"):
        say("拉起失败：%s" % res.get("why"), "error")
        log_line({"event": "launch_fail", "cause": cause,
                  "why": res.get("why")})
        return {"cause": cause, "action": "no_exe",
                "message": "游戏没能拉起：%s" % res.get("why"),
                "hwnd": None, "recoveries": led.used}

    hwnd = wait_window(timeout=wait_s, finder=finder)
    if not hwnd:
        say("游戏进程起来了但 %d 秒内没等到窗口" % int(wait_s), "error")
        log_line({"event": "window_timeout", "cause": cause,
                  "pid": res.get("pid")})
        return {"cause": cause, "action": "no_exe",
                "message": "游戏已启动但窗口没出现", "hwnd": None,
                "recoveries": led.used}

    n = led.spend(cause, ok=True)
    say("已自动恢复 %d/%d，窗口 hwnd=%s；回到大厅后从上一次进度续跑"
        % (n, led.max_recover, hwnd))
    log_line({"event": "recovered", "cause": cause, "hwnd": hwnd,
              "pid": res.get("pid"), "recoveries": n})
    return {"cause": cause, "action": "resume",
            "message": "%s 已自动恢复 %d/%d" % (cause, n, led.max_recover),
            "hwnd": hwnd, "recoveries": n}
