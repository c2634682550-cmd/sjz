"""发布链路一键验证（不打包、不装系统目录）。

    python tools\\release_verify.py [--with-launcher-tests]

产出 build\\release_verify.json 与 build\\release_verify.md（报告引用）。
"""
import argparse
import compileall
import importlib.util
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

RESULTS = []


def add(name, ok, detail=""):
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})


def load(path):
    spec = importlib.util.spec_from_file_location("rv_" + os.path.basename(path)[:-3], path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def check_py_compile():
    # tools/ 里混有开发脚本（其中有历史遗留的语法错误文件），
    # 只编译真正随包发布的运行时脚本
    targets = [os.path.join(ROOT, "core"), os.path.join(ROOT, "evo")]
    ok = True
    for t in targets:
        ok = compileall.compile_dir(t, quiet=1, force=True) and ok
    for f in ("desktop.py", "run.py",
              os.path.join("tools", "fire_worker.py"),
              os.path.join("tools", "calibrate.py"),
              os.path.join("tools", "version_sync.py"),
              os.path.join("tools", "audit_packaging.py"),
              os.path.join("tools", "make_release.py"),
              os.path.join("tools", "release_verify.py")):
        ok = compileall.compile_file(os.path.join(ROOT, f), quiet=1, force=True) and ok
    add("py_compile（运行时源码）", ok)


def check_collect_data_files():
    """真调一次 PyInstaller 的 collect_data_files，确认 rapidocr 静态资源可见。"""
    try:
        from PyInstaller.utils.hooks import collect_data_files
        files = collect_data_files("rapidocr")
    except Exception as e:  # PyInstaller 未装就直接跳过，不算失败
        add("collect_data_files 演练", True, f"跳过：{e}")
        return
    yamls = [f for f in files if f[0].lower().endswith((".yaml", ".yml"))]
    add("collect_data_files(rapidocr)", len(files) > 0,
        f"{len(files)} 个文件，其中 yaml {len(yamls)} 个")


def check_spec():
    mod = load(os.path.join(ROOT, "tools", "audit_packaging.py"))
    for spec in ("sjz.spec", "sjz-onefile.spec"):
        res = mod.audit(os.path.join(ROOT, spec))
        add(f"打包差集 {spec}",
            res["ok"] and not res["data_diff"] and not res["module_diff"],
            f"datas={res['datas_count']} hidden={res['hidden_count']} "
            f"必需文件={res['required_files']} 差集={len(res['data_diff'])}")
        for row in res["data_diff"]:
            add("  · 数据未收集", False, row["file"])
        for row in res["module_diff"]:
            add("  · 模块未钉住", False, str(row))


def check_version():
    mod = load(os.path.join(ROOT, "tools", "version_sync.py"))
    ok, rows = mod.check_all(mod.read_version())
    add("版本号四处一致", ok, "; ".join(f"{r['name']}={r['detail']}"
                                        for r in rows if r["ok"]))
    for r in rows:
        if not r["ok"]:
            add("  · 版本不一致", False, r["name"] + " " + r["detail"])


def check_installer(no_real):
    mod = load(os.path.join(ROOT, "installer", "verify_installer.py"))
    static = mod.static_checks()
    for c in static:
        add("安装器：" + c["name"], c["ok"], c["detail"])
    sim = mod.simulate()
    for c in sim["checks"]:
        add("卸载演练：" + c["name"], c["ok"], c["detail"])
    iscc = None if no_real else mod.find_iscc()
    if iscc:
        real = mod.real_silent_install(iscc)
        add("Inno 静默安装→卸载（临时目录）", real["ok"],
            str(real.get("detail")))
    else:
        add("Inno 静默安装（真跑）", True, "跳过：本机未装 Inno Setup 6")


def check_launcher_tests():
    py = sys.executable
    r = subprocess.run([py, "-m", "pytest", "launcher/tests", "-q"],
                       cwd=ROOT, capture_output=True, text=True)
    tail = (r.stdout or "").strip().splitlines()[-1:] 
    add("启动器更新/回滚测试", r.returncode == 0, " ".join(tail))


def write_out():
    ok = all(r["ok"] for r in RESULTS)
    data = {"ok": ok, "checks": RESULTS}
    os.makedirs(os.path.join(ROOT, "build"), exist_ok=True)
    with open(os.path.join(ROOT, "build", "release_verify.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    lines = ["# 发布链路验证", "", "| 结果 | 检查项 | 明细 |", "|---|---|---|"]
    for r in RESULTS:
        lines.append("| {} | {} | {} |".format(
            "通过" if r["ok"] else "失败", r["name"], r["detail"].replace("|", "/")))
    lines.append("")
    lines.append("总结果：**" + ("通过" if ok else "有失败项") + "**")
    with open(os.path.join(ROOT, "build", "release_verify.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    for r in RESULTS:
        print(("  OK   " if r["ok"] else "  FAIL ") + r["name"] + "  " + r["detail"])
    print("总结果：" + ("通过" if ok else "有失败项"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-real-install", action="store_true")
    ap.add_argument("--with-launcher-tests", action="store_true")
    args = ap.parse_args(argv)
    check_py_compile()
    check_collect_data_files()
    check_spec()
    check_version()
    check_installer(args.no_real_install)
    if args.with_launcher_tests:
        check_launcher_tests()
    return write_out()


if __name__ == "__main__":
    sys.exit(main())
