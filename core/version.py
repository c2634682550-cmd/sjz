"""版本号单点（唯一真源）。

所有地方都从这里派生，禁止再写字面量：
  · exe 文件版本 —— tools/version_sync.py 生成 build/version_info.txt
  · 启动器更新比对 —— tools/version_sync.py 生成 launcher/Version.g.cs
  · 界面「关于」页 —— core/config.py 的 public() 注入 version 字段
  · pyproject.toml —— version_sync --write 同步

改版本只改这一行，然后跑：
    python tools/version_sync.py --write
"""

APP_VERSION = "1.1.0"

# 兼容两种导入习惯（`from core.version import APP_VERSION` / `__version__`）
__version__ = APP_VERSION
VERSION = APP_VERSION
