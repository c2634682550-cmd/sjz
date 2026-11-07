"""把诊断结果渲染成人能读的文本（JSON 由 model.as_dict 直接输出）。"""
from __future__ import annotations

from ..common import fmt_ts
from .model import Diagnosis

_VERDICT_TEXT = {
    "conclusive": "有结论",
    "ambiguous": "分不开（需二次观测）",
    "conflict": "硬信号冲突（需人工）",
    "insufficient": "证据不足（放弃自动诊断）",
    "normal": "未见已知异常",
}


def render(d: Diagnosis, width: int = 78) -> str:
    out = []
    span = ""
    if d.window_start and d.window_end:
        span = f"{fmt_ts(d.window_start)} ~ {fmt_ts(d.window_end)}（{d.window_end - d.window_start:.0f}s）"
    head = d.window_label or span
    if span and span != d.window_label:
        head = f"{d.window_label}  {span}" if d.window_label else span
    out.append(f"窗口：{head}")
    out.append(f"结论：{d.verdict} —— {_VERDICT_TEXT.get(d.verdict, d.verdict)}")
    if d.sources:
        out.append("数据：" + "，".join(f"{k}={v}" for k, v in d.sources.items()))
    out.append("")

    if d.causes:
        out.append("根因排序：")
        for i, c in enumerate(d.causes, 1):
            out.append(f"  #{i} {c.label}")
            out.append(f"     置信度 {c.confidence:.2f}（{c.basis}），分数 {c.score:.2f}，"
                       f"场景 {c.scene or '-'}")
            out.append(f"     建议动作：{', '.join(c.actions) or '（现有动作库无对应动作）'}")
            if c.risk:
                out.append(f"     误判风险：{c.risk}")
            if c.evidence:
                out.append("     证据：")
                for e in c.evidence:
                    loc = f"{e.source}:{e.line}" if e.line else e.source
                    out.append(f"       {loc}  {e.text}")
    else:
        out.append("根因排序：（无）")

    if d.missing:
        out.append("")
        out.append("盲区 / 缺什么：")
        for m in d.missing:
            out.append(f"  - {m}")
    if d.notes:
        out.append("")
        out.append("读取备注：")
        for n in d.notes:
            out.append(f"  - {n}")
    return "\n".join(out)
