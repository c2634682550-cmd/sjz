"""本地开发启动：python -m portal.run（生产用 gunicorn portal.wsgi:application）。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portal.app import create_app          # noqa: E402

app = create_app()

if __name__ == "__main__":
    cfg = app.extensions["portal_config"]
    app.run(host=cfg.get("host"), port=int(cfg.get("port")), debug=False,
            use_reloader=False)
