"""离线自我诊断（evo/diagnose）。

只做「判断」：读日志 + 遥测 + 台账，产出根因排序与证据行号；
动作仍由 core/recovery.py 执行，本模块不新造动作。
"""

from .engine import diagnose  # noqa: F401

__all__ = ["diagnose"]
