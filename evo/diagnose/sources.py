"""离线证据源读取：文本日志 / soak cycles / metrics.csv / 台账 records.jsonl。

统一产出带「文件 + 行号 + 时间」的观测，供规则匹配与证据引用。

两条硬约束（防答案泄漏）：
1. 不读 soak cycles 的 `fault_active` 字段；
2. 不匹配日志里的 `[fault]` 注入行。
这两者是仿真台架自己写的「标准答案」，让诊断器读它们等于作弊。

任何读不动的行都保留成坏行证据，不静默跳过 —— 「台账读不动」本身就是症状。
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Iterable

# 台架答案标记，出现在日志里一律不进特征
LEAK_MARKERS = ("[fault]", "fault_start", "fault_end", "fault_active")

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")

_TS_FULL = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_TS_CLOCK = re.compile(r"(?<![\d:])(\d{2}:\d{2}:\d{2})(?![\d:])")


def read_text(path: str) -> tuple[list[str], str]:
    """按常见编码逐个试；全失败用 errors=replace 兜底（保留坏字节痕迹）。"""
    raw = open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace").splitlines(), "utf-16"
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc).splitlines(), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace").splitlines(), "utf-8/replace"


def parse_ts(line: str, anchor_date: str | None = None) -> float | None:
    m = _TS_FULL.search(line)
    if m:
        try:
            return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return None
    m = _TS_CLOCK.search(line)
    if m and anchor_date:
        try:
            return time.mktime(time.strptime(f"{anchor_date} {m.group(1)}", "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return None
    return None


def is_leak(line: str) -> bool:
    return any(m in line for m in LEAK_MARKERS)


@dataclass
class LogLine:
    source: str
    line: int
    ts: float | None
    text: str


@dataclass
class Row:
    source: str
    line: int
    ts: float | None
    data: dict


@dataclass
class SourceBundle:
    logs: list[LogLine] = field(default_factory=list)
    cycles: list[Row] = field(default_factory=list)
    metrics: list[Row] = field(default_factory=list)
    ledger: list[Row] = field(default_factory=list)
    bad_ledger: list[EvidenceRow] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class EvidenceRow:
    source: str
    line: int
    ts: float | None
    text: str


def rel(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:
        return path


def load_logs(paths: Iterable[str], root: str) -> tuple[list[LogLine], list[str]]:
    out: list[LogLine] = []
    notes: list[str] = []
    for p in paths:
        lines, enc = read_text(p)
        # 无日期的日志（RESULT 台架输出）用文件 mtime 兜底日期，只用于跨行对齐
        anchor = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(p)))
        n_ts = 0
        for i, raw in enumerate(lines, 1):
            if is_leak(raw):
                continue
            ts = parse_ts(raw, anchor)
            if ts is not None:
                n_ts += 1
            out.append(LogLine(rel(p, root), i, ts, raw.rstrip("\n")))
        notes.append(f"{rel(p, root)}: {len(lines)} 行, 编码 {enc}, 带时间戳 {n_ts} 行")
    return out, notes


def load_cycles(path: str, root: str) -> tuple[list[Row], list[str]]:
    rows: list[Row] = []
    notes: list[str] = []
    for i, raw in enumerate(read_text(path)[0], 1):
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            notes.append(f"cycles 第 {i} 行不是 JSON，跳过")
            continue
        # 防泄漏：台架答案字段直接丢弃
        d.pop("fault_active", None)
        ts = d.get("ts")
        rows.append(Row(rel(path, root), i, float(ts) if isinstance(ts, (int, float)) else None, d))
    return rows, notes


def load_metrics(path: str, root: str) -> tuple[list[Row], list[str]]:
    lines, _enc = read_text(path)
    if not lines:
        return [], ["metrics 为空"]
    body = "\n".join(lines)
    hdr = next(csv.reader([lines[0]]))
    rows: list[Row] = []
    for i, row in enumerate(csv.reader(body.splitlines()[1:]), 2):
        if not row or len(row) < 2:
            continue
        d = {k: (v if v != "" else None) for k, v in zip(hdr, row)}
        # 防泄漏：faults_* 是台架的注入计数，不是症状
        for k in ("faults_active", "faults_total", "faults_recovered"):
            d.pop(k, None)
        try:
            ts = float(d.get("ts"))
        except (TypeError, ValueError):
            ts = None
        rows.append(Row(rel(path, root), i, ts, d))
    return rows, []


def load_ledger(path: str, root: str) -> tuple[list[Row], list[EvidenceRow], list[str]]:
    """台账：逐行解析；坏行与超长行都是证据，不是噪声。"""
    if not os.path.exists(path):
        return [], [], [f"台账不存在: {rel(path, root)}"]
    lines, enc = read_text(path)
    rows: list[Row] = []
    bad: list[EvidenceRow] = []
    notes: list[str] = [f"{rel(path, root)}: {len(lines)} 行, 编码 {enc}"]
    for i, raw in enumerate(lines, 1):
        if not raw.strip():
            bad.append(EvidenceRow(rel(path, root), i, None, "<空行>"))
            continue
        try:
            d = json.loads(raw)
        except ValueError as exc:
            bad.append(EvidenceRow(rel(path, root), i, None, f"{type(exc).__name__}: {raw[:80]}"))
            continue
        ts = d.get("ts")
        rows.append(Row(rel(path, root), i,
                        float(ts) if isinstance(ts, (int, float)) else None, d))
    # 尾部没有换行 = 下次追加会和残行粘在一起（P6 现象）
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) not in (b"\n", b"\r"):
            notes.append("台账最后一行没有换行符：下一条追加会粘行")
    return rows, bad, notes


def load_bundle(*, logs: Iterable[str] = (), cycles: str | None = None,
                metrics: str | None = None, ledger: str | None = None,
                root: str = ".") -> SourceBundle:
    b = SourceBundle()
    logs = [p for p in logs if p]
    # 缺文件不是崩溃理由：记一条备注，等于该信号源不可用
    keep = []
    for p in logs:
        if os.path.exists(p):
            keep.append(p)
        else:
            b.notes.append(f"日志不存在: {rel(p, root)}")
    if keep:
        b.logs, n = load_logs(keep, root)
        b.notes += n
    if cycles and os.path.exists(cycles):
        b.cycles, n = load_cycles(cycles, root)
        b.notes += n
    elif cycles:
        b.notes.append(f"cycles 不存在: {rel(cycles, root)}")
    if metrics and os.path.exists(metrics):
        b.metrics, n = load_metrics(metrics, root)
        b.notes += n
    elif metrics:
        b.notes.append(f"metrics 不存在: {rel(metrics, root)}（该时段无遥测）")
    if ledger:
        b.ledger, b.bad_ledger, n = load_ledger(ledger, root)
        b.notes += n
    b.counts = {
        "log": len(b.logs), "cycles": len(b.cycles),
        "metrics": len(b.metrics), "ledger": len(b.ledger),
        "ledger_bad": len(b.bad_ledger),
    }
    return b


def in_window(ts: float | None, start: float | None, end: float | None) -> bool:
    """无时间戳的行一律纳入（RESULT 台架文件没有时间，只能整段看）。"""
    if start is None or end is None or ts is None:
        return True
    return start <= ts <= end
