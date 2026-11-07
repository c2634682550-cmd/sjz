"""`core.retention`：台账轮转、截图修剪、日志轮转、目录统计。

这模块以前零覆盖。它错的方式很安静：轮转顺序搞反会把最新台账删掉、
修剪按名字而不按 mtime 会删错文件、`dir_usage` 把目录算成文件会让
界面上的磁盘占用虚高 —— 每一条都不会报错，只会慢慢吃盘/丢数据。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import retention as r  # noqa: E402


def _write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)
    return path


# ---------------------------------------------------------------- rotate

def test_rotate_does_nothing_below_the_limit(tmp_path):
    p = _write(tmp_path / "records.jsonl", "x" * 100)
    assert r.rotate(str(p), max_bytes=200) is False
    assert os.path.isfile(p)
    assert not os.path.isfile(str(p) + ".1")


def test_rotate_cascades_and_keeps_only_the_newest(tmp_path):
    p = str(tmp_path / "records.jsonl")
    _write(p, "current")
    _write(p + ".1", "older")
    _write(p + ".2", "oldest")
    assert r.rotate(p, max_bytes=1, keep=3) is True

    assert not os.path.isfile(p), "当前文件应该被挪走"
    assert open(p + ".1", encoding="utf-8").read() == "current"
    assert open(p + ".2", encoding="utf-8").read() == "older"
    assert not os.path.isfile(p + ".3"), "keep=3 只能留 .1/.2 两个历史"


def test_rotate_with_keep_two_drops_the_previous_archive(tmp_path):
    p = str(tmp_path / "records.jsonl")
    _write(p, "current")
    _write(p + ".1", "discard-me")
    assert r.rotate(p, max_bytes=1, keep=2) is True
    assert open(p + ".1", encoding="utf-8").read() == "current"


def test_rotate_disabled_when_max_bytes_is_zero(tmp_path):
    p = _write(tmp_path / "records.jsonl", "x" * 100)
    assert r.rotate(str(p), max_bytes=0) is False
    assert os.path.isfile(p)


def test_rotate_exactly_at_the_limit_still_rotates(tmp_path):
    p = _write(tmp_path / "records.jsonl", "12345")
    assert r.rotate(str(p), max_bytes=5) is True, "到上限应该轮转，而不是越界才转"


def test_archives_lists_from_newest_to_oldest(tmp_path):
    p = str(tmp_path / "records.jsonl")
    _write(p + ".1", "a")
    _write(p + ".2", "b")
    assert r.archives(p, keep=3) == [p + ".1", p + ".2"]
    assert r.archives(p, keep=2) == [p + ".1"]


# ---------------------------------------------------------------- prune_dir

def _age(path, seconds):
    ts = time.time() - seconds
    os.utime(path, (ts, ts))


def test_prune_dir_removes_the_oldest_files_first(tmp_path):
    paths = []
    for i in range(5):
        p = _write(tmp_path / f"shot_{i}.jpg", "x" * 10)
        _age(p, 50 - i * 10)          # shot_0 最老
        paths.append(p)

    removed = r.prune_dir(str(tmp_path), max_files=2, max_bytes=10 ** 9)
    assert removed == 3
    left = sorted(os.listdir(tmp_path))
    assert left == ["shot_3.jpg", "shot_4.jpg"], f"删错了文件：{left}"


def test_prune_dir_honours_the_byte_budget(tmp_path):
    for i in range(5):
        p = _write(tmp_path / f"s{i}.bin", "x" * 10)
        _age(p, 50 - i * 10)

    removed = r.prune_dir(str(tmp_path), max_files=100, max_bytes=25)
    assert removed == 3
    assert r.dir_usage(str(tmp_path)) == (2, 20)


def test_prune_dir_missing_directory_is_a_noop(tmp_path):
    assert r.prune_dir(str(tmp_path / "nope")) == 0


def test_prune_dir_leaves_exactly_max_files(tmp_path):
    for i in range(4):
        p = _write(tmp_path / f"f{i}.jpg", "x")
        _age(p, 40 - i * 10)
    assert r.prune_dir(str(tmp_path), max_files=4, max_bytes=10 ** 9) == 0


# ---------------------------------------------------------------- rotate_log

def test_rotate_log_only_when_over_the_limit(tmp_path):
    p = _write(tmp_path / "ocr_service_9000.log", "abc")
    assert r.rotate_log(str(p), max_bytes=10) is False
    assert os.path.isfile(p)


def test_rotate_log_replaces_the_previous_backup(tmp_path):
    p = str(tmp_path / "ocr_service_9000.log")
    _write(p, "first-run")
    _write(p + ".1", "stale-backup")

    assert r.rotate_log(p, max_bytes=1) is True
    assert not os.path.isfile(p)
    assert open(p + ".1", encoding="utf-8").read() == "first-run", (
        "旧 .1 没被覆盖，日志会无限膨胀"
    )

    _write(p, "second-run")
    assert r.rotate_log(p, max_bytes=1) is True
    assert open(p + ".1", encoding="utf-8").read() == "second-run"


# ---------------------------------------------------------------- dir_usage

def test_dir_usage_counts_files_only(tmp_path):
    _write(tmp_path / "a.txt", "123")
    _write(tmp_path / "b.txt", "12345")
    (tmp_path / "sub").mkdir()
    assert r.dir_usage(str(tmp_path)) == (2, 8)


def test_dir_usage_missing_directory(tmp_path):
    assert r.dir_usage(str(tmp_path / "nope")) == (0, 0)
