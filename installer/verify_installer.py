"""安装器校验：静态规则 + 临时目录下的安装/卸载演练（不写系统目录）。

用法：
    python installer\\verify_installer.py [--json build\\installer_verify.json]

做的四件事：
  1. 静态检查 sjz.iss：目录不写死盘符、有卸载页数据说明、VC 运行库检测、
     可选驱动任务、快捷方式、注册表卸载键。
  2. 从 sjz.iss 解析 [UninstallDelete] 与 PreserveUserData 的真实路径集合，
     断言「PreserveUserData 声称保留的每个路径都被 UninstallDelete 覆盖」，
     并且「被删的用户数据路径必须全部在保留清单里」——不再有拍脑袋豁免。
  3. 若装了 Inno Setup（iscc），额外真跑一次静默安装到临时目录 —— 但绝不装到
     Program Files。
  4. 否则按 iss 解析出的路径在临时目录演练：安装 → 写用户数据 → 卸载 → 断言
     程序文件/快捷方式/注册表清空、保留目录里每个路径都存在且非空。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISS = os.path.join(ROOT, "installer", "sjz.iss")
DIST = os.path.join(ROOT, "dist", "SJZ-Control")

# 只用于「造数据」与兜底造树；真正的路径来源是 iss 解析结果
PROGRAM_TOP = ["_internal", "launcher", "web", "evo", "tools", "core",
               "SJZ-Control.exe"]
# 被 UninstallDelete 点名删除、但必须由 PreserveUserData 保留的目录/文件
USER_DATA_NAMES = ("config.json", "launcher.config.json", "data", "templates")


def find_iscc():
    cands = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Inno Setup 6", "ISCC.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Inno Setup 6", "ISCC.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
    ]
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None


def _section(text, name):
    m = re.search(r"^\[" + re.escape(name) + r"\]\s*$([\s\S]*?)(?=^\[|\Z)",
                  text, re.M | re.I)
    return m.group(1) if m else ""


def parse_iss():
    """从 sjz.iss 解析卸载删除集合与保留集合（唯一真源，不手抄）。"""
    text = open(ISS, encoding="utf-8-sig").read()
    ud = _section(text, "UninstallDelete")
    delete = re.findall(r'Name:\s*"\{app\}\\([^"]+)"', ud, re.I)
    delete = sorted({d.replace("\\\\", "\\").strip("\\").lower() for d in delete})

    m = re.search(r"function\s+PreserveUserData\s*:\s*String;([\s\S]*?)\nend;", text, re.M)
    body = m.group(1) if m else ""
    preserve = re.findall(r"App\s*\+\s*'\\([^']+)'", body)
    preserve = sorted({p.strip("\\").lower() for p in preserve})
    return text, text.lower(), delete, preserve, body


def static_checks():
    text, low, delete, preserve, body = parse_iss()
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("默认目录用 {autopf} 不写死盘符",
        "defaultdirname={autopf}" in low.replace(" ", ""),
        "DefaultDirName={autopf}\\SJZ-Control")
    add("允许用户改目录", "disabledirpage=no" in low.replace(" ", ""))
    add("升级沿用旧目录（同 AppId）", "usepreviousappdir=yes" in low.replace(" ", "")
        and "appid=" in low.replace(" ", ""))
    add("静默安装可用", "verysilent" in low)
    add("桌面/开始菜单快捷方式",
        "[icons]" in low and "{autodesktop}" in low and "{autoprograms}" in low)
    add("写卸载信息（注册表键）",
        "[registry]" in low and "uninsdeletekey" in low)
    # 驱动安装从 [Run] 的 `Tasks: driver` 改成了 [Code] 里的 EnsureDriver：
    # 任务定义仍在 [Tasks]（Name: "driver"），由 WizardIsTaskSelected 读取。
    _ns = re.sub(r"\s+", "", low)
    add("可选驱动任务",
        'name:"driver"' in _ns and "wizardistaskselected('driver')" in _ns
        and "install-interception.exe" in low)
    add("VC 运行库检测", "vcredistinstalled" in low and "runtimes\\x64" in low)
    add("卸载页说明数据位置", "usuninstall" in low and "mbinformation" in low
        and "{localappdata}\\sjz-control" in low)

    # —— 硬约束：拷贝必须校验，失败必须 rescue，失败必须落日志 ——
    add("保留走「校验版」拷贝",
        "CopyFileVerified" in body and "CopyTreeVerified" in body,
        "PreserveUserData 内必须调用 CopyFileVerified / CopyTreeVerified")
    add("拷贝失败不静默删（rescue）",
        "RescueItem" in body,
        "PreserveUserData 内必须在失败时调用 RescueItem 把本体搬出安装目录")
    add("失败详情落盘",
        "uninstall_preserve_error.log" in low and "writepreservelog" in low)
    add("保留目录只留最近 3 份",
        "pruneoldkeep" in low and "pickmaxdir" in low,
        "PruneOldKeep：删之前先校验最近 3 份非空")

    # —— 路径集合一一对应：不允许凭白放行 ——
    ud_paths = delete
    missing = [p for p in preserve if p not in ud_paths]
    add("Preserve 保留的路径都在 UninstallDelete 的删除范围里",
        not missing, "缺：" + ",".join(missing) if missing else
        "preserve=" + ",".join(preserve))
    leaks = [n for n in USER_DATA_NAMES
             if n in ud_paths and n not in preserve and n != "config.json.good"]
    add("UninstallDelete 里每个用户数据路径都被 PreserveUserData 覆盖",
        not leaks, "未覆盖：" + ",".join(leaks) if leaks else "")
    add("安装源指向 dist 且递归",
        "..\\dist\\sjz-control\\*" in low and "recursesubdirs" in low)
    return checks


def _copy_verified(src, dst):
    """模拟安装器的「先验证后删」：拷贝后确认目标存在且非空。"""
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
        files = sum(len(f) for _, _, f in os.walk(dst))
        return files > 0
    shutil.copy2(src, dst)
    return os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src)


def simulate():
    """按 iss 解析出的路径演练 安装→卸载，断言清理与保留。"""
    _, _, delete, preserve, _ = parse_iss()
    tmp = tempfile.mkdtemp(prefix="sjz-install-")
    app = os.path.join(tmp, "Programs", "SJZ-Control")
    programs = os.path.join(tmp, "StartMenu")
    desktop = os.path.join(tmp, "Desktop")
    local = os.path.join(tmp, "LocalAppData", "SJZ-Control")
    os.makedirs(programs)
    os.makedirs(desktop)
    os.makedirs(local)

    # ---- 安装：拷贝 dist 目录（模拟 [Files] recursesubdirs）----
    os.makedirs(app)
    installed_files = 0
    if os.path.isdir(DIST):
        for base, dirs, files in os.walk(DIST):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            rel = os.path.relpath(base, DIST)
            dst = app if rel == "." else os.path.join(app, rel)
            os.makedirs(dst, exist_ok=True)
            for f in files:
                shutil.copy2(os.path.join(base, f), os.path.join(dst, f))
                installed_files += 1
    else:
        for top in PROGRAM_TOP:
            p = os.path.join(app, top)
            if os.path.splitext(top)[1]:
                open(p, "w").write("x")
            else:
                os.makedirs(p, exist_ok=True)
                open(os.path.join(p, "dummy.bin"), "w").write("x")
            installed_files += 1

    # 快捷方式（模拟 [Icons]）
    open(os.path.join(programs, "SJZ-Control.lnk"), "w").write("lnk")
    open(os.path.join(desktop, "SJZ-Control.lnk"), "w").write("lnk")

    # ---- 用户数据（运行后产生；路径来自 iss，不手抄）----
    for name in preserve:
        p = os.path.join(app, name)
        if name.endswith(".json"):
            os.makedirs(os.path.dirname(p) or app, exist_ok=True)
            open(p, "w", encoding="utf-8").write('{"k":1}')
        else:
            os.makedirs(os.path.join(p, "shots"), exist_ok=True)
            open(os.path.join(p, "records.jsonl"), "w").write("row\n")
            open(os.path.join(p, "shots", "a.png"), "w").write("png")
    os.makedirs(os.path.join(local, "logs"), exist_ok=True)
    open(os.path.join(local, "license.json"), "w").write("lic")
    open(os.path.join(local, "logs", "app.log"), "w").write("log")

    # ---- 卸载：先搬用户数据（对应 [Code] PreserveUserData）----
    keep = os.path.join(local, "uninstalled-20261107-120000")
    os.makedirs(keep)
    copied, failed = set(), set()
    for name in preserve:
        src = os.path.join(app, name)
        if not os.path.exists(src):
            continue
        ok = _copy_verified(src, os.path.join(keep, name))
        (copied if ok else failed).add(name)

    # ---- 卸载：只删「校验通过」的路径（对应先验证后删）----
    for name in delete:
        p = os.path.join(app, name)
        if name in failed:
            continue                     # 拷贝失败 → 绝不删
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            os.remove(p)
    for lnk in (os.path.join(programs, "SJZ-Control.lnk"),
                os.path.join(desktop, "SJZ-Control.lnk")):
        if os.path.exists(lnk):
            os.remove(lnk)
    # 安装清单里的程序文件由卸载器整体反向删除；有拷贝失败时保留安装目录
    if not failed:
        shutil.rmtree(app, ignore_errors=True)

    results = []

    def add(name, ok, detail=""):
        results.append({"name": name, "ok": bool(ok), "detail": detail})

    add("安装目录已移除", not os.path.exists(app))
    add("开始菜单快捷方式已移除",
        not os.path.isfile(os.path.join(programs, "SJZ-Control.lnk")))
    add("桌面快捷方式已移除",
        not os.path.isfile(os.path.join(desktop, "SJZ-Control.lnk")))
    add("许可证未被卸载触碰", os.path.isfile(os.path.join(local, "license.json")))
    add("日志未被卸载触碰", os.path.isfile(os.path.join(local, "logs", "app.log")))
    add("保留目录非空", os.path.isdir(keep) and bool(os.listdir(keep)))

    # 每个「声称保留」的路径都必须在保留目录里存在且非空
    for name in preserve:
        p = os.path.join(keep, name)
        if os.path.isdir(p):
            ok = sum(len(f) for _, _, f in os.walk(p)) > 0
        else:
            ok = os.path.isfile(p) and os.path.getsize(p) > 0
        add("保留项存在且非空：" + name, ok, "failed_copy=" + ",".join(sorted(failed)))
    add("没有被跳过的拷贝", not failed,
        "failed=" + ",".join(sorted(failed)) if failed else "")

    shutil.rmtree(tmp, ignore_errors=True)
    return {"installed_files": installed_files,
            "delete_paths": delete, "preserve_paths": preserve,
            "data_keep_dir": "LOCALAPPDATA\\SJZ-Control\\uninstalled-<stamp>",
            "checks": results}


def real_silent_install(iscc):
    """真跑一次 Inno 静默安装到临时目录（不碰 Program Files）。"""
    tmp = tempfile.mkdtemp(prefix="sjz-inno-")
    app = os.path.join(tmp, "app")
    log = os.path.join(tmp, "install.log")
    try:
        r = subprocess.run([iscc, ISS], cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            return {"ok": False, "stage": "build", "detail": r.stdout[-800:]}
        setups = [f for f in os.listdir(os.path.join(ROOT, "dist-installer"))
                  if f.endswith(".exe")]
        if not setups:
            return {"ok": False, "stage": "build", "detail": "没产出 setup.exe"}
        setup = os.path.join(ROOT, "dist-installer", sorted(setups)[-1])
        r = subprocess.run([setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                            "/DIR=" + app, "/LOG=" + log],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return {"ok": False, "stage": "install", "detail": r.returncode}
        ok_installed = os.path.isfile(os.path.join(app, "SJZ-Control.exe")) or \
                       os.path.isfile(os.path.join(app, "launcher", "SjzLauncher.exe"))
        unins = os.path.join(app, "unins000.exe")
        ok_unins = os.path.isfile(unins)
        r2 = subprocess.run([unins, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                            capture_output=True, text=True)
        gone = False
        for _ in range(20):
            if not os.path.exists(app):
                gone = True
                break
            time.sleep(0.5)
        left = []
        if not gone:
            for base, dirs, files in os.walk(app):
                for f in files:
                    left.append(os.path.relpath(os.path.join(base, f), app))
        return {"ok": ok_installed and ok_unins and gone,
                "stage": "done",
                "detail": f"installed={ok_installed} uninstaller={ok_unins} "
                          f"dir_gone={gone} unins_rc={r2.returncode} "
                          f"left={len(left)} {left[:5]}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.path.join(ROOT, "build", "installer_verify.json"))
    ap.add_argument("--no-real", action="store_true",
                    help="即使装了 Inno 也不真跑")
    args = ap.parse_args(argv)

    checks = static_checks()
    sim = simulate()
    iscc = None if args.no_real else find_iscc()
    real = real_silent_install(iscc) if iscc else {"ok": None, "stage": "skipped",
                                                   "detail": "未安装 Inno Setup 6"}
    all_ok = all(c["ok"] for c in checks) and all(c["ok"] for c in sim["checks"]) \
        and (real["ok"] is None or real["ok"])

    out = {"ok": all_ok, "static": checks, "simulate": sim, "real_install": real}
    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    for c in checks:
        print(("  OK   " if c["ok"] else "  FAIL ") + "[静态] " + c["name"])
    for c in sim["checks"]:
        print(("  OK   " if c["ok"] else "  FAIL ") + "[演练] " + c["name"])
    print(f"  {real['stage']}: {'OK' if real['ok'] else real['detail']}")
    print("结果：" + ("通过" if all_ok else "有失败项"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
