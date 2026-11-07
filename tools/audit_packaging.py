"""打包一致性静态审计：spec 收集 vs 代码实际需要。

不跑 PyInstaller（太慢），只做 AST + 文件系统静态比对：
  1. 数据文件差集 —— 运行时按路径读取的文件，spec.datas 是否覆盖
  2. 模块差集     —— 代码里的 import / 动态引用，spec.hiddenimports 是否覆盖
  3. 反向检查     —— spec 里声明但磁盘上不存在的 src / hiddenimport

用法：
    python tools/audit_packaging.py [--spec sjz.spec] [--json out.json]

差集必须为空，否则 exit 1。
"""
import argparse
import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "sjz.spec")

IGNORE_DIRS = {"__pycache__", ".pytest_cache", "perf", "_ref", "_pristine"}
# evo 的运行产物不进交付包，审计时也不当作必需文件
# collect/ 是采集与 bench 产物（events*.jsonl），同样不算必需
EVO_SKIP = {"runs", "fixtures", "state", ".state", "collect", "__pycache__"}


# ---------------------------------------------------------------- spec 解析
def load_spec(path):
    """执行 spec 到 Analysis 之前的部分，拿到真实的 datas / HIDDEN。

    与 tests/test_packaging.py 同样的做法：给 PyInstaller 的 hook 函数打桩。
    """
    import types

    src = open(path, encoding="utf-8").read()
    cut = src.find("a = Analysis(")
    if cut < 0:
        raise SystemExit(f"{path} 里找不到 a = Analysis(")
    head = src[:cut]

    stub = types.ModuleType("PyInstaller.utils.hooks")
    stub.collect_data_files = lambda *a, **k: []
    stub.collect_submodules = lambda *a, **k: []
    stub.collect_dynamic_libs = lambda *a, **k: []

    saved = sys.modules.get("PyInstaller.utils.hooks")
    sys.modules["PyInstaller.utils.hooks"] = stub
    try:
        g = {"__name__": "spec_under_audit", "__file__": path}
        old = os.getcwd()
        os.chdir(ROOT)  # spec 用 os.path.abspath(os.getcwd())
        try:
            exec(compile(head, path, "exec"), g)
        finally:
            os.chdir(old)
    finally:
        if saved is not None:
            sys.modules["PyInstaller.utils.hooks"] = saved
        else:
            sys.modules.pop("PyInstaller.utils.hooks", None)

    datas = [tuple(d) for d in g.get("datas", [])]
    hidden = set(g.get("HIDDEN", []))
    return datas, hidden


# ------------------------------------------------------------ 代码需要的文件
RUNTIME_TOOLS = ["fire_worker.py", "calibrate.py", "OCR_game.yaml", "OCR_tiny.yaml"]


def iter_files(base, suffix_filter=None):
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for f in files:
            p = os.path.join(root, f)
            if suffix_filter and not f.endswith(suffix_filter):
                continue
            yield p


def required_data_files():
    """运行时真正会按路径打开的非 .py 文件。"""
    req = set()
    # core/ 下除 .py 以外的都在运行时被 __file__ 定位（bricks.json、
    # skins_raw.csv、assets 整目录、ocr 模型/字模/模板/DLL）
    for p in iter_files(os.path.join(ROOT, "core")):
        if not p.endswith(".py"):
            req.add(p)
    # 前端
    for p in iter_files(os.path.join(ROOT, "web")):
        req.add(p)
    # 默认模板集（_seed_missing_assets 释放源）
    for p in iter_files(os.path.join(ROOT, "templates")):
        req.add(p)
    # 进化框架：源码 + 自带配置（runs/fixtures/state 运行产物不算必需）
    for base, dirs, files in os.walk(os.path.join(ROOT, "evo")):
        dirs[:] = [d for d in dirs if d not in EVO_SKIP]
        for f in files:
            req.add(os.path.join(base, f))
    # 随包运行时脚本
    for name in RUNTIME_TOOLS:
        req.add(os.path.join(ROOT, "tools", name))
    return sorted(req)


def covered_by_datas(path, datas):
    for src, dest in datas:
        src = os.path.abspath(src)
        if path == src:
            return True
        if os.path.isdir(src) and path.startswith(src + os.sep):
            return True
    return False


def dest_of(path, datas):
    for src, dest in datas:
        src = os.path.abspath(src)
        if path == src:
            return dest
        if os.path.isdir(src) and path.startswith(src + os.sep):
            rel = os.path.relpath(path, src)
            return os.path.join(dest, rel)
    return None


# --------------------------------------------------------- 代码 import 的模块
def runtime_py_files():
    """真正随 exe 一起运行/被调起的源码（tools 下只算运行时脚本，排除开发脚本）。"""
    files = [os.path.join(ROOT, "desktop.py"), os.path.join(ROOT, "run.py")]
    for p in iter_files(os.path.join(ROOT, "core"), ".py"):
        files.append(p)
    for name in ("fire_worker.py", "calibrate.py"):
        p = os.path.join(ROOT, "tools", name)
        if os.path.isfile(p):
            files.append(p)
    return [f for f in files if os.path.isfile(f)]


def imported_core_modules():
    """收集对 core.* 的显式/相对 import。"""
    mods = set()
    for p in runtime_py_files():
        tree = ast.parse(open(p, encoding="utf-8-sig").read(), p, type_comments=False)
        pkg = os.path.relpath(os.path.dirname(p), ROOT).replace(os.sep, ".")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = pkg if node.level == 1 else pkg.rsplit(".", node.level - 1)[0]
                    if node.module:
                        base = base + "." + node.module
                if base.startswith("core."):
                    mods.add(base)
                    mods.update(base + "." + a.name for a in node.names if a.name != "*")
                elif base == "core":
                    mods.update("core." + a.name for a in node.names if a.name != "*")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("core."):
                        mods.add(a.name)
    return mods


def dynamic_refs():
    """静态 import 抓不到、必须靠 datas/hiddenimports 的引用。"""
    refs = []
    patterns = [
        (re.compile(r'(?:importlib[^\n]*?|spec_from_file_location\([^\n]*?)[\'"](tools[\\/][\w.\-]+)[\'"]'), "data"),
        (re.compile(r'[\'"]tools[\\/]([\w.\-]+\.(?:py|yaml|yml|json))[\'"]'), "data"),
        (re.compile(r'[\'"]tools[\'"]\s*,\s*[\'"]([\w.\-]+\.(?:py|yaml|yml|json|dll))[\'"]'), "data"),
        (re.compile(r'[-]m\s+evo|[\\/]evo[\\/]cli|"evo\.cli"'), "evo"),
    ]
    for p in runtime_py_files():
        text = open(p, encoding="utf-8", errors="ignore").read()
        for i, line in enumerate(text.splitlines(), 1):
            for rx, kind in patterns:
                for m in rx.finditer(line):
                    if kind == "data":
                        name = m.group(1).replace("\\", "/").split("/")[-1]
                        refs.append({"file": os.path.relpath(p, ROOT), "line": i,
                                     "kind": "data", "ref": name})
                    else:
                        refs.append({"file": os.path.relpath(p, ROOT), "line": i,
                                     "kind": "evo", "ref": "evo"})
    # 去重
    seen, out = set(), []
    for r in refs:
        k = (r["file"], r["line"], r["ref"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def module_exists(name):
    rel = name.replace(".", os.sep) + ".py"
    if os.path.isfile(os.path.join(ROOT, rel)):
        return True
    pkg = os.path.join(ROOT, name.replace(".", os.sep), "__init__.py")
    return os.path.isfile(pkg)


# ----------------------------------------------------------------------- main
def audit(spec_path):
    datas, hidden = load_spec(spec_path)
    req = required_data_files()

    # 1) 数据差集
    missing = [p for p in req if not covered_by_datas(p, datas)]
    data_rows = [{
        "file": os.path.relpath(p, ROOT).replace(os.sep, "/"),
        "size": os.path.getsize(p),
        "dest": dest_of(p, datas) or "<未收集>",
    } for p in req]
    unreachable = [row for row in data_rows if row["dest"] == "<未收集>"]

    # spec src 悬空检查
    dangling = []
    for src, dest in datas:
        if not os.path.exists(src):
            dangling.append({"src": os.path.relpath(src, ROOT).replace(os.sep, "/"),
                             "dest": dest})

    # 2) 模块
    imported = sorted(imported_core_modules())
    dangling_modules = [m for m in sorted(hidden)
                        if m.startswith("core.") and not module_exists(m)]
    dyn = dynamic_refs()
    dyn_uncovered = []
    for r in dyn:
        if r["kind"] == "evo":
            ok = covered_by_datas(os.path.join(ROOT, "evo"), datas)
        else:
            ok = covered_by_datas(os.path.join(ROOT, "tools", r["ref"]), datas)
        if not ok:
            dyn_uncovered.append(r)

    # 3) 模块差集 = 代码 import 的 core 模块 −（hidden ∪ 静态可见）
    # 静态可见的模块 PyInstaller 自己会收；这里只把「被延迟/间接引用且没钉住」
    # 的算作差集，也就是 dyn_uncovered 与不存在的 hiddenimport。
    module_diff = dyn_uncovered + [{"kind": "hiddenimport-missing", "ref": m}
                                   for m in dangling_modules]

    return {
        "root": ROOT,
        "spec": os.path.relpath(spec_path, ROOT),
        "datas_count": len(datas),
        "hidden_count": len(hidden),
        "required_files": len(req),
        "imported_core_modules": imported,
        "data_diff": [r for r in data_rows if r["dest"] == "<未收集>"],
        "module_diff": module_diff,
        "dangling_datas": dangling,
        "ok": not missing and not module_diff and not dangling,
    }


def print_md(res):
    print(f"spec: {res['spec']}  datas={res['datas_count']}  hidden={res['hidden_count']}")
    print(f"运行时需要的文件: {res['required_files']}")
    print(f"代码 import 的 core 模块: {len(res['imported_core_modules'])}")
    print("\n## 数据项差集\n")
    if not res["data_diff"]:
        print("| 文件 | 缺口 |\n|---|---|\n| — | 空（全部已收集） |")
    else:
        print("| 文件 | 缺口 |\n|---|---|")
        for r in res["data_diff"]:
            print(f"| {r['file']} | 未在 spec.datas 声明 |")
    print("\n## 模块项差集\n")
    if not res["module_diff"]:
        print("| 模块/引用 | 缺口 |\n|---|---|\n| — | 空（全部已钉住） |")
    else:
        print("| 模块/引用 | 缺口 |\n|---|---|")
        for r in res["module_diff"]:
            print(f"| {r.get('ref')} | {r.get('kind')} |")
    if res["dangling_datas"]:
        print("\n## spec 声明但磁盘缺失\n")
        for d in res["dangling_datas"]:
            print(f"- {d['src']} -> {d['dest']}")


def groups(res, datas):
    import collections
    g = collections.OrderedDict()
    for p in required_data_files():
        rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
        if rel.startswith("core/assets"):
            k = "core/assets（OCR模型/字模/模板/DLL）"
        elif rel.startswith("core/"):
            k = "core 其他数据文件"
        elif rel.startswith("web/"):
            k = "web/"
        elif rel.startswith("templates/"):
            k = "templates/"
        elif rel.startswith("evo/"):
            k = "evo/"
        elif rel.startswith("tools/"):
            k = "tools/ 运行时脚本+驱动安装器"
        else:
            k = "其他"
        d = g.setdefault(k, {"n": 0, "miss": 0, "dest": set()})
        d["n"] += 1
        dest = dest_of(p, datas)
        if dest:
            d["dest"].add(os.path.dirname(dest).replace(os.sep, "/") or ".")
        else:
            d["miss"] += 1
    return g


def print_summary(spec_path):
    datas, hidden = load_spec(spec_path)
    g = groups(None, datas)
    print("\n## 分组覆盖表\n")
    print("| 类别 | 文件数 | 未收集 | 落地目录 |")
    print("|---|---|---|---|")
    for k, v in g.items():
        print(f"| {k} | {v['n']} | {v['miss']} | {', '.join(sorted(v['dest']))} |")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default=SPEC)
    ap.add_argument("--json")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args(argv)
    res = audit(args.spec)
    print_md(res)
    if args.summary:
        print_summary(args.spec)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
