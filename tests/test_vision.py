"""中文路径下存图/读图的回归测试。

本地回归的是一条**会让整个抢购功能失效、但完全不报错**的缺陷：
OpenCV 的 `imread` / `imwrite` 在 Windows 上按 ANSI 码页打开文件，路径里
只要有一个中文字符就直接失败 —— `imwrite` 返回 False，`imread` 返回 None，
两者都不抛异常。

项目自己的目录名就可能带中文（实测解压在「新建文件夹 (2)」下）。后果是
`/api/template` 把模板存到 `templates/` 下失败，界面提示保存成功，但
`engine._load` 随后报「模板读不出来」，抢购根本起不来；而且报错点在读取，
不在保存，排查方向会被带偏。

所以这一组测试直接断言「写进去的能原样读回来」，而不是断言「调用了某个函数」。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.vision import imread, imwrite  # noqa: E402


@pytest.fixture()
def cn_dir(tmp_path):
    """一个名字含中文的目录，模拟用户把项目放在中文路径下。"""
    d = tmp_path / "新建文件夹 (2)" / "templates"
    d.mkdir(parents=True)
    return d


def test_roundtrip_through_a_chinese_path(cn_dir):
    """写进中文路径再读回来，像素必须一致。

    这是缺陷的直接复现：`cv2.imwrite` 对同一路径返回 False、`cv2.imread`
    返回 None，所以「写成功且读回来一致」这一条在修复前必然失败。
    """
    img = np.zeros((24, 40, 3), np.uint8)
    img[:, :20] = (10, 200, 30)
    img[:, 20:] = (200, 30, 10)

    path = os.path.join(cn_dir, "buy.png")
    assert imwrite(path, img) is True
    assert os.path.isfile(path), "文件没真的落盘"

    back = imread(path)
    assert back is not None, "读回来是 None —— 中文路径仍然失败"
    assert back.shape == img.shape
    assert np.array_equal(back, img), "像素被改动了"


def test_ascii_path_still_works(tmp_path):
    """别为了修中文路径把普通路径搞坏。"""
    img = np.full((8, 8, 3), 77, np.uint8)
    path = os.path.join(tmp_path, "confirm.png")
    assert imwrite(path, img) is True
    back = imread(path)
    assert back is not None and np.array_equal(back, img)


def test_imread_missing_file_returns_none(tmp_path):
    """文件不存在返回 None，不能抛异常 —— 调用方全都按 None 判断。"""
    assert imread(os.path.join(tmp_path, "没有这个文件.png")) is None


def test_imwrite_reports_failure_instead_of_lying(tmp_path):
    """写不进去必须返回 False。

    以前 `cv2.imwrite` 的返回值被直接丢掉，`/api/template` 无论如何都回
    `ok: True`。现在返回值要能反映真实结果，否则界面会假装保存成功。
    """
    # 指向一个不存在的目录，写入必然失败
    assert imwrite(os.path.join(tmp_path, "no_such_dir", "x.png"), np.zeros((4, 4, 3), np.uint8)) is False
