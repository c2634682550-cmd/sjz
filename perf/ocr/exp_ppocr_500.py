# -*- coding: utf-8 -*-
"""抓二级服务的原始 500 响应体，确认崩溃点（PIR/oneDNN），不是配置写错。"""
import urllib.error
import urllib.request

import cv2
import numpy as np

BOUND = "----perfx"
img = np.full((40, 160, 3), 255, np.uint8)
_ok, buf = cv2.imencode(".png", img)
raw = buf.tobytes()
head = (f'--{BOUND}\r\nContent-Disposition: form-data; name="file"; '
        'filename="a.png"\r\nContent-Type: image/png\r\n\r\n').encode()
body = head + raw + f'\r\n--{BOUND}--\r\n'.encode()
req = urllib.request.Request(
    "http://127.0.0.1:8081/ocr", data=body,
    headers={"Content-Type": f"multipart/form-data; boundary={BOUND}"})
try:
    r = urllib.request.urlopen(req, timeout=30)
    print("HTTP", r.status, r.read()[:200])
except urllib.error.HTTPError as e:
    print("HTTP", e.code)
    print(e.read().decode("utf-8", "replace")[:1200])
except Exception as e:                       # noqa: BLE001
    print(type(e).__name__, e)
