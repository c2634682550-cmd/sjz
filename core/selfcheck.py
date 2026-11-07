"""打包 / 运行环境自检（`--doctor` 的扩展段）。

覆盖：模型文件、输入驱动（DLL + 内核服务）、WebView2、字体、权限级别、许可状态。
原则：**只读**。不装驱动、不发输入、不改注册表、不开窗口、不动真实光标。
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import time

APP_NAME = "SJZ-Control"
# WebView2 Runtime 的 EdgeUpdate 客户端 GUID（微软固定值）
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
# UI 必需的字体：微软雅黑（中文正文）。`segmdl2.ttf`（Segoe MDL2 图标）
# 以前被算成必需 —— 但 web/index.html 的字族是
# "Segoe UI","Microsoft YaHei","PingFang SC"，**没有任何一个字符引用
# MDL2**；缺它只是系统级图标字体少了，界面完全正常。把可选字体标成必需
# 会让干净的 Win10 LTSC / 精简系统误报一条红字（审查 R09-13）。
FONT_REQUIRED = ("msyh.ttc",)
FONT_ICON_OPTIONAL = ("segmdl2.ttf",)
FONT_FILES = ("msyh.ttc", "msyhbd.ttc", "msyhsl.ttc", "segmdl2.ttf")


def _win():
    return sys.platform.startswith("win")


def _sha16(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return "?"


def _reg(root, path, name):
    if not _win():
        return None
    try:
        import winreg
        with winreg.OpenKey(root, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:                          # noqa: BLE001
        return None


def models():
    """OCR 模型文件清单：(文件名, 大小, sha256 前 16 位)。"""
    try:
        from core import ocr
        d = ocr.rapid_model_dir()
    except Exception:                          # noqa: BLE001
        return None, []
    if not d or not os.path.isdir(d):
        return d, []
    rows = []
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            rows.append((name, os.path.getsize(p), _sha16(p)))
    return d, rows


def driver():
    """interception.dll 与内核驱动状态；返回 dict，不产生任何输入事件。"""
    out = {"dll": None, "dll_error": None, "context": None,
           "sys_installed": False, "service_start": None}
    try:
        from core import interception
        cand = interception._dll_candidates()   # noqa: SLF001
        out["dll_paths"] = cand
        out["dll"] = next((p for p in cand if os.path.isfile(p)), None)
        if out["dll"]:
            out["dll_size"] = os.path.getsize(out["dll"])
            out["dll_sha16"] = _sha16(out["dll"])
        out["dll_error"] = interception._LIB.error()  # noqa: SLF001
        # probe=False：只建 context，不发任何事件（不碰光标）
        ok, why = interception.driver_ready(probe=False)
        out["context"] = ok
        out["context_error"] = why
    except Exception as e:                     # noqa: BLE001
        out["context_error"] = f"{type(e).__name__}: {e}"

    # Interception 的驱动**不是** interception.sys：官方 v1.0.1 包里只有一个
    # install-interception.exe（驱动以 DRIVER/KBDNT、DRIVER/MOUNT 资源内嵌），
    # 安装后落盘的是 keyboard.sys + mouse.sys 两个上滤驱动，服务名也是
    # keyboard / mouse。按旧判据（interception.sys + 服务 interception）查，
    # 装好的机器也会被判成「未安装」。
    drv_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "drivers")
    out["driver_dir"] = drv_dir
    sys_files = {n: os.path.isfile(os.path.join(drv_dir, n))
                 for n in ("keyboard.sys", "mouse.sys")}
    out["sys_files"] = sys_files
    out["sys_path"] = os.path.join(drv_dir, "keyboard.sys")
    out["sys_installed"] = all(sys_files.values())
    if _win():
        svcs = {}
        for name in ("keyboard", "mouse"):
            start = _reg(__import__("winreg").HKEY_LOCAL_MACHINE,
                         rf"SYSTEM\CurrentControlSet\Services\{name}", "Start")
            if start is not None:
                svcs[name] = int(start)
        out["services"] = svcs
        if svcs:
            out["service_name"] = ",".join(svcs)
            out["service_start"] = min(svcs.values())
        # 设备对象是驱动自己创建的：能打开才证明内核侧真的在跑
        try:
            from core.interception import _device_openable
            ok_dev, err_dev = _device_openable()
            out["device"] = {"path": r"\\.\interception00",
                             "openable": ok_dev, "err": err_dev}
        except Exception:                    # noqa: BLE001
            out["device"] = None
    return out


def webview2():
    out = {"pv": None, "dirs": [], "pywebview": None}
    if _win():
        for root, path in ((__import__("winreg").HKEY_LOCAL_MACHINE,
                            rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
                           (__import__("winreg").HKEY_LOCAL_MACHINE,
                            rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}")):
            pv = _reg(root, path, "pv")
            if pv:
                out["pv"] = pv
                break
        for base in (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.environ.get("ProgramFiles", r"C:\Program Files")):
            d = os.path.join(base, "Microsoft", "EdgeWebView", "Application")
            if os.path.isdir(d):
                out["dirs"].append((d, sorted(os.listdir(d))[:3]))
    try:
        import webview                          # noqa: F401
        out["pywebview"] = True
    except Exception:                          # noqa: BLE001
        out["pywebview"] = False
    return out


def fonts():
    root = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")
    rows = []
    for name in FONT_FILES:
        p = os.path.join(root, name)
        rows.append((name, os.path.isfile(p)))
    return rows


def privilege():
    out = {"admin": None, "integrity": None, "level": None}
    try:
        out["admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                          # noqa: BLE001
        pass
    try:
        from core.input import process_integrity
        rid = process_integrity()
        out["integrity"] = rid
        out["level"] = {0x2000: "中（普通用户）", 0x3000: "高（管理员）",
                        0x1000: "低", 0x4000: "系统"}.get(rid, hex(rid) if rid else "?")
    except Exception as e:                     # noqa: BLE001
        out["level"] = f"读取失败：{type(e).__name__}"
    return out


def license_status():
    try:
        from core import license as lic
        return lic.summary()
    except Exception as e:                     # noqa: BLE001
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}


def doctor_lines(*, deep=True) -> list:
    """给 desktop._doctor() 打印的行。deep=False 时跳过耗时的 OCR 初始化。"""
    lines = []

    lines.append("[模型]")
    mdir, rows = models()
    lines.append(f"  目录          = {mdir}")
    if rows:
        total = sum(r[1] for r in rows)
        for name, size, sha in rows:
            lines.append(f"  {name:<34} {size / 1e6:6.1f} MB  sha256:{sha}")
        lines.append(f"  合计          = {total / 1e6:.1f} MB")
    else:
        lines.append("  模型文件      = 缺失")
    if deep:
        try:
            from core import ocr
            t0 = time.perf_counter()
            ready = ocr.available()
            lines.append(f"  RapidOCR 初始化= {ready}，"
                         f"{1000 * (time.perf_counter() - t0):.0f} ms（已预热则接近 0）")
            lines.append(f"  引擎错误      = {ocr.rapid_error() or '（无）'}")
        except Exception as e:                 # noqa: BLE001
            lines.append(f"  RapidOCR 初始化= 失败 {type(e).__name__} {e}")

    lines.append("[输入驱动]")
    d = driver()
    lines.append(f"  DLL           = {d.get('dll') or '缺失'}")
    if d.get("dll"):
        lines.append(f"  DLL 大小/sha  = {d.get('dll_size', 0) / 1e6:.2f} MB / "
                     f"{d.get('dll_sha16')}")
    if d.get("dll_error") and not d.get("dll"):
        lines.append(f"  DLL 错误      = {d['dll_error']}")
    _svcs = d.get("services") or {}
    _files = d.get("sys_files") or {}
    _svc_txt = ("已安装 Start=%s（%s）" % (d["service_start"], d.get("service_name"))
                if d.get("service_start") is not None else "未安装")
    _file_txt = " ".join(f"{n}={'存在' if ok else '不存在'}"
                         for n, ok in (_files.items() or [("keyboard.sys", False)]))
    lines.append(f"  内核服务      = {_svc_txt}；驱动文件 {_file_txt}")
    _dev = d.get("device")
    if _dev:
        lines.append("  设备          = " + _dev["path"]
                     + (" 可打开（内核侧在跑）" if _dev["openable"]
                        else f" 打不开（错误码 {_dev['err']}）"))
    lines.append(f"  context       = {d.get('context')}"
                 + (f"（{d.get('context_error')}）" if d.get("context_error") else ""))
    lines.append("  说明          = context 能建**不代表**驱动已装（create_context 会假阳性）；"
                 "--doctor 不发任何输入事件，所以不做真发探活")
    if not _svcs and not d.get("sys_installed"):
        lines.append("  结论          = 驱动未安装 → 重新运行安装程序并勾选『安装高速输入驱动』，"
                     "装完重启电脑；手册见 docs/driver-install.md。当前按 input_fallback 处理"
                     "（默认 fail，界面显式报错），功能降级但可用")

    lines.append("[WebView2]")
    w = webview2()
    lines.append(f"  Runtime 版本  = {w['pv'] or '未注册'}")
    for dp, vers in w["dirs"]:
        lines.append(f"  安装目录      = {dp}（{', '.join(vers)}）")
    lines.append(f"  pywebview     = {w['pywebview']}")
    if not w["pv"] and not w["dirs"]:
        lines.append("  结论          = 缺 WebView2 Runtime，界面起不来；"
                     "安装包需内置 Evergreen Bootstrapper 或提示用户安装")

    lines.append("[字体]")
    for name, ok in fonts():
        if name in FONT_REQUIRED:
            tag = "必需"
        elif name in FONT_ICON_OPTIONAL:
            tag = "可选（UI 未引用，仅系统图标字体）"
        else:
            tag = "可选"
        lines.append(f"  {name:<14} = {'有' if ok else '缺失'}（{tag}）")
    if any(not os.path.isfile(os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "Fonts", n))
            for n in FONT_REQUIRED):
        lines.append("  结论          = 缺必需字体会出现方框；安装包需带上或检测安装")
    else:
        lines.append("  结论          = 必需字体齐全（图标字体缺失不影响渲染）")

    lines.append("[权限]")
    p = privilege()
    lines.append(f"  管理员        = {p['admin']}；完整性 = {p['level']}")
    lines.append(f"  Windows 版本  = {sys.getwindowsversion()}")

    lines.append("[许可]")
    ls = license_status()
    lines.append(f"  状态          = {ls.get('status')}  ok={ls.get('ok')}"
                 + (f"（{ls.get('reason')}）" if ls.get("reason") else ""))
    if ls.get("tier"):
        lines.append(f"  档位/试用     = {ls.get('tier')} / {ls.get('trial')}；"
                     f"剩余 {ls.get('remaining_s')} s")
    lines.append(f"  许可文件      = {ls.get('path') or 'n/a'}（存在={ls.get('present')}）")
    return lines


if __name__ == "__main__":
    for line in doctor_lines():
        print(line)
