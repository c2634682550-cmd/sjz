from core.ocr_service import Supervisor


def test_plain_keeps_only_filename_of_windows_path():
    out = Supervisor._plain(r"没有 D:\work\venv\Scripts\paddleex.exe")
    assert out == "没有 paddleex.exe"
    assert "venv" not in out


def test_plain_handles_forward_slash_and_drops_exception_names():
    assert Supervisor._plain("FileNotFoundError: 缺 C:/a/b/c.dll；其他") == "缺 c.dll；其他"
