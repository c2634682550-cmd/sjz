"""WSGI 入口：gunicorn -w 2 -b 127.0.0.1:8080 portal.wsgi:application"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portal.app import create_app          # noqa: E402

application = create_app()
app = application
