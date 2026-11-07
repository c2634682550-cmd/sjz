"""自我诊断的数据结构。

一条诊断 = 一个时间窗 + 若干按分排序的根因假设 + 支撑证据（文件/行号）。

设计原则：
- 证据必须能指回原始行，不能只有一句结论；
- 置信度分三档来源：hard（必现硬信号）、stat（统计信号）、corr（相关性），
  低档来源不允许给出高置信度；
- 「判不出来」是一等公民：verdict 允许 insufficient / ambiguous / conflict。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Evidence:
    source: str                  # 仓库根相对路径或 RESULT 文件名
    line: int | None             # 行号（1 起）
    ts: float | None             # epoch 秒
    text: str                    # 原文片段（截断）
    rule: str = ""               # 命中的规则 id
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CauseHit:
    cause: str                   # 根因 id，如 c.input.uipi
    label: str
    score: float                 # 原始加权分
    confidence: float            # 映射后的置信度 0..1
    scene: str                   # 对应 core/scene_guard.py 的场景 id
    actions: list[str]           # 引用 core/recovery.py 的原子动作 id
    risk: str                    # 误判风险说明
    basis: str                   # hard|stat|corr
    evidence: list[Evidence] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = [e.as_dict() for e in self.evidence]
        return d


@dataclass
class Diagnosis:
    window_start: float | None
    window_end: float | None
    window_label: str
    verdict: str = "insufficient"   # conclusive|ambiguous|insufficient|conflict|normal
    causes: list[CauseHit] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # 缺什么数据/缺什么区分信号
    notes: list[str] = field(default_factory=list)
    sources: dict[str, int] = field(default_factory=dict)  # 各源读到的行数

    @property
    def top(self) -> CauseHit | None:
        return self.causes[0] if self.causes else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": {"start": self.window_start, "end": self.window_end,
                       "label": self.window_label},
            "verdict": self.verdict,
            "causes": [c.as_dict() for c in self.causes],
            "missing": self.missing,
            "notes": self.notes,
            "sources": self.sources,
        }
