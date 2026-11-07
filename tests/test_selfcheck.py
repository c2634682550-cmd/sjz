"""`core.selfcheck`：只读环境自检（--doctor）。

以前零覆盖。原则是**只读**：不装驱动、不发输入、不改注册表、不开窗口。
所以这里的用例全部把系统访问替换成假对象，只验证「读到什么就报什么」，
不碰真机、不读真注册表。
"""
import hashlib
import importlib
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import selfcheck as sc  # noqa: E402

core = importlib.import_module("core")


def _fake_ocr(**kw):
    ns = types.SimpleNamespace(**kw)
    return ns


# ---------------------------------------------------------------- _sha16

def test_sha16_is_the_first_16_hex_chars(tmp_path):
    p = tmp_path / "m.onnx"
    p.write_bytes(b"hello")
    want = hashlib.sha256(b"hello").hexdigest()[:16]
    assert sc._sha16(str(p)) == want
    assert len(sc._sha16(str(p))) == 16


def test_sha16_missing_file_returns_question_mark(tmp_path):
    assert sc._sha16(str(tmp_path / "nope.onnx")) == "?"


# ---------------------------------------------------------------- models

def test_models_lists_files_with_size_and_hash(tmp_path, monkeypatch):
    (tmp_path / "b.onnx").write_bytes(b"12345")
    (tmp_path / "a.onnx").write_bytes(b"123")
    fake = _fake_ocr(rapid_model_dir=lambda: str(tmp_path))
    monkeypatch.setattr(core, "ocr", fake)
    monkeypatch.setitem(sys.modules, "core.ocr", fake)

    d, rows = sc.models()
    assert d == str(tmp_path)
    assert rows == [
        ("a.onnx", 3, hashlib.sha256(b"123").hexdigest()[:16]),
        ("b.onnx", 5, hashlib.sha256(b"12345").hexdigest()[:16]),
    ]


def test_models_reports_a_missing_directory(tmp_path, monkeypatch):
    missing = tmp_path / "models"
    fake = _fake_ocr(rapid_model_dir=lambda: str(missing))
    monkeypatch.setattr(core, "ocr", fake)
    monkeypatch.setitem(sys.modules, "core.ocr", fake)

    assert sc.models() == (str(missing), [])


def test_models_survives_an_import_failure(monkeypatch):
    def boom():
        raise RuntimeError("模型组件没装")

    fake = _fake_ocr(rapid_model_dir=boom)
    monkeypatch.setattr(core, "ocr", fake)
    monkeypatch.setitem(sys.modules, "core.ocr", fake)

    d, rows = sc.models()
    assert d is None and rows == []


# ---------------------------------------------------------------- fonts

def test_fonts_reads_from_the_system_root_and_marks_required(tmp_path, monkeypatch):
    fonts_dir = tmp_path / "Fonts"
    fonts_dir.mkdir()
    (fonts_dir / "msyh.ttc").write_bytes(b"x")
    monkeypatch.setenv("SystemRoot", str(tmp_path))

    rows = sc.fonts()
    assert rows == [("msyh.ttc", True), ("msyhbd.ttc", False),
                    ("msyhsl.ttc", False), ("segmdl2.ttf", False)]
    assert dict(rows)["msyh.ttc"] is True
    assert "msyh.ttc" in sc.FONT_REQUIRED


# ---------------------------------------------------------------- privilege

def test_privilege_maps_integrity_levels(monkeypatch):
    from core import input as input_mod
    for rid, want in ((0x2000, "中（普通用户）"), (0x3000, "高（管理员）"),
                      (0x1000, "低"), (0x4000, "系统")):
        monkeypatch.setattr(input_mod, "process_integrity", lambda rid=rid: rid)
        out = sc.privilege()
        assert out["integrity"] == rid
        assert out["level"] == want

    monkeypatch.setattr(input_mod, "process_integrity", lambda: 0x1234)
    assert sc.privilege()["level"] == "0x1234"


def test_privilege_surfaces_a_read_failure(monkeypatch):
    from core import input as input_mod

    def boom():
        raise RuntimeError("拿不到令牌")

    monkeypatch.setattr(input_mod, "process_integrity", boom)
    out = sc.privilege()
    assert out["level"] == "读取失败：RuntimeError"


# ---------------------------------------------------------------- webview2

def test_webview2_on_non_windows_skips_the_registry(monkeypatch):
    monkeypatch.setattr(sc, "_win", lambda: False)
    out = sc.webview2()
    assert out["pv"] is None
    assert out["dirs"] == []


# ---------------------------------------------------------------- doctor_lines

def _stub_sections(monkeypatch, with_models=True):
    if with_models:
        monkeypatch.setattr(sc, "models", lambda: (None, []))
    monkeypatch.setattr(sc, "driver", lambda: {
        "dll": None, "dll_error": None, "context": None,
        "sys_installed": False, "service_start": None,
    })
    monkeypatch.setattr(sc, "webview2", lambda: {
        "pv": None, "dirs": [], "pywebview": False,
    })
    monkeypatch.setattr(sc, "fonts", lambda: [
        ("msyh.ttc", False), ("msyhbd.ttc", False),
        ("msyhsl.ttc", False), ("segmdl2.ttf", False),
    ])
    monkeypatch.setattr(sc, "privilege", lambda: {
        "admin": False, "integrity": 0x2000, "level": "中（普通用户）",
    })
    monkeypatch.setattr(sc, "license_status", lambda: {
        "status": "ok", "ok": True, "path": None, "present": False,
    })


def test_doctor_lines_short_mode_covers_every_section(monkeypatch, tmp_path):
    _stub_sections(monkeypatch)
    # 结论行直接读 SystemRoot 下的 Fonts，所以连环境变量一起挪走
    monkeypatch.setenv("SystemRoot", str(tmp_path / "nowhere"))
    lines = sc.doctor_lines(deep=False)
    text = "\n".join(lines)

    for header in ("[模型]", "[输入驱动]", "[WebView2]", "[字体]", "[权限]", "[许可]"):
        assert header in text, f"缺少段落 {header}"
    assert "模型文件      = 缺失" in text
    assert "驱动未安装" in text
    assert "缺 WebView2 Runtime" in text
    assert "缺必需字体会出现方框" in text
    assert "管理员        = False；完整性 = 中（普通用户）" in text
    assert "状态          = ok  ok=True" in text
    assert "RapidOCR" not in text, "deep=False 不该去初始化 OCR"


def test_doctor_lines_deep_mode_reports_ocr_boot(monkeypatch, tmp_path):
    # 这一段就是要走真的 models()：用假 ocr 模块把目录指到 tmp
    _stub_sections(monkeypatch, with_models=False)
    (tmp_path / "det.onnx").write_bytes(b"m")
    fake = _fake_ocr(rapid_model_dir=lambda: str(tmp_path),
                     available=lambda: True, rapid_error=lambda: None)
    monkeypatch.setattr(core, "ocr", fake)
    monkeypatch.setitem(sys.modules, "core.ocr", fake)

    text = "\n".join(sc.doctor_lines(deep=True))
    assert "det.onnx" in text
    assert "RapidOCR 初始化= True" in text
    assert "引擎错误      = （无）" in text


def test_doctor_lines_lists_services_and_webview_dirs(monkeypatch):
    _stub_sections(monkeypatch)
    monkeypatch.setattr(sc, "driver", lambda: {
        "dll": r"C:\x\interception.dll", "dll_size": 2_000_000,
        "dll_sha16": "abc123", "dll_error": None,
        "context": True, "context_error": None,
        "sys_installed": True, "service_start": 0,
        "services": {"keyboard": 0, "mouse": 0}, "service_name": "keyboard,mouse",
        "sys_files": {"keyboard.sys": True, "mouse.sys": True},
    })
    monkeypatch.setattr(sc, "webview2", lambda: {
        "pv": "120.0.1", "dirs": [(r"C:\Edge", ["120.0.1"])], "pywebview": True,
    })
    text = "\n".join(sc.doctor_lines(deep=False))
    assert "内核服务      = 已安装 Start=0" in text
    assert "驱动文件 keyboard.sys=存在 mouse.sys=存在" in text
    assert "DLL 大小/sha  = 2.00 MB / abc123" in text
    assert "Runtime 版本  = 120.0.1" in text
    assert "安装目录      = C:\\Edge（120.0.1）" in text
    assert "驱动未安装" not in text
