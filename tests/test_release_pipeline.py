"""发布链路静态门槛：spec 收集差集必须为空、版本号单点一致、安装器规则不破。

这些用例不打包、不装系统目录，几秒钟跑完，作为 CI / build.bat 的前置闸门。
"""
import compileall
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))


def _load(path):
    spec = importlib.util.spec_from_file_location("mod_" + os.path.basename(path)[:-3], path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_spec_collects_everything_code_needs():
    """打包差集拼接：sjz.spec 与 sjz-onefile.spec 都必须是空差集。"""
    mod = _load(os.path.join(ROOT, "tools", "audit_packaging.py"))
    for spec in ("sjz.spec", "sjz-onefile.spec"):
        res = mod.audit(os.path.join(ROOT, spec))
        assert res["ok"], (
            f"{spec} 差集非空：data={res['data_diff']} module={res['module_diff']} "
            f"dangling={res['dangling_datas']}")
        assert res["required_files"] > 0


def test_runtime_modules_are_pinned():
    """关键运行时模块都在 hiddenimports 里（防止动态 import 被漏掉）。"""
    mod = _load(os.path.join(ROOT, "tools", "audit_packaging.py"))
    for spec in ("sjz.spec", "sjz-onefile.spec"):
        _, hidden = mod.load_spec(os.path.join(ROOT, spec))
        for name in ("core.humanmouse", "core.idle_roam", "core.repeat",
                     "core.skip_late", "core.stop_grade", "core.click_burst",
                     "core.money", "core.watchdog", "core.retention",
                     "core.recovery", "core.scene_guard", "core.timesync",
                     "core.ocr_rapid", "core.ocr_chain", "core.fire_proc",
                     "core.version"):
            assert name in hidden, f"{spec} 缺 hiddenimport {name}"


WRITE_CMD = "uv run python tools\\version_sync.py --write"


def _assert_version_artifacts():
    """版本生成前置闸门：缺失/过期时给出可读、可直接照做的失败信息。"""
    mod = _load(os.path.join(ROOT, "tools", "version_sync.py"))
    ok, rows = mod.check_all(mod.read_version())
    if not ok:
        bad = [r for r in rows if not r["ok"]]
        detail = "\n".join(f"  - {r['name']}：{r['detail']}" for r in bad)
        raise AssertionError(
            "版本派生文件缺失或与真源 core/version.py 不一致：\n"
            f"{detail}\n"
            f"先跑生成前置：{WRITE_CMD}\n"
            "（build.bat 的 [2/7] 已内置这一步；build/ 被 .gitignore 忽略，"
            "清掉 build/ 后必须先重新生成再跑测试）"
        )
    return mod, rows


def test_version_single_source():
    """exe 版本 / 关于页 / 更新比对 / 安装器 全部派生自 core/version.py。"""
    _assert_version_artifacts()


def test_version_generator_runs_without_build_dir(tmp_path):
    """生成器本身不依赖 build/ 存在：临时目录可复现三份派生文件且幂等。"""
    mod = _load(os.path.join(ROOT, "tools", "version_sync.py"))
    v = mod.read_version()

    first = None
    for _ in range(2):
        rendered = mod.render_all(v)
        if first is None:
            first = rendered
        else:
            assert rendered == first, "生成器输出不幂等（同一版本渲染两次结果不同）"
        for rel, text in rendered.items():
            dst = tmp_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(dst, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)

    vsi = (tmp_path / "build" / "version_info.txt").read_text(encoding="utf-8")
    assert f"'{v}'" in vsi
    assert f"filevers={mod.version_tuple(v)}" in vsi

    # 仓库里已有生成物时，必须与生成器输出逐字节一致（防手改漂移）
    for rel, text in first.items():
        repo = os.path.join(ROOT, *rel.split("/"))
        if os.path.isfile(repo):
            with open(repo, encoding="utf-8") as f:
                assert f.read() == text, f"{rel} 与生成器输出不一致（被手改过？）"


def test_exe_version_resource_declared():
    import re
    for spec in ("sjz.spec", "sjz-onefile.spec"):
        txt = open(os.path.join(ROOT, spec), encoding="utf-8").read()
        m = re.search(r"version=([^\n,]+)", txt)
        arg = m.group(1) if m else ""
        assert m and ("_VER_INFO" in arg or "_require_ver_info" in arg), (
            f"{spec} 没把版本资源接进 EXE（version= 参数里既没有 _VER_INFO "
            f"也没有 _require_ver_info：{arg!r}）")


def test_installer_rules():
    """安装器不得写死盘符、不得在卸载里删用户数据、必须有卸载提示。"""
    mod = _load(os.path.join(ROOT, "installer", "verify_installer.py"))
    checks = mod.static_checks()
    failed = [c for c in checks if not c["ok"]]
    assert not failed, failed


def test_no_hardcoded_install_drive():
    txt = open(os.path.join(ROOT, "installer", "sjz.iss"), encoding="utf-8-sig").read()
    import re
    bad = re.findall(r'(?i)(destdir|defaultdirname)\s*[:=]\s*"[a-z]:\\', txt)
    assert not bad, bad


def test_runtime_python_compiles():
    """随包全部 Python 源码可编译（含 evo/tools 运行时脚本）。"""
    targets = [os.path.join(ROOT, "core"), os.path.join(ROOT, "evo")]
    ok = True
    for t in targets:
        ok = compileall.compile_dir(t, quiet=1, force=True) and ok
    for f in ("desktop.py", "run.py",
              os.path.join("tools", "fire_worker.py"),
              os.path.join("tools", "calibrate.py")):
        ok = compileall.compile_file(os.path.join(ROOT, f), quiet=1, force=True) and ok
    assert ok


def test_web_about_reads_version_from_api():
    html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    assert "S.config.version" in html
