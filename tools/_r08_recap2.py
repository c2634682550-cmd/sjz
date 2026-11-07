# -*- coding: utf-8 -*-
"""Capture current-build state for the R08-19 item (only item with a visible delta)."""
import io, os, sys, json, time, urllib.request
ROOT = r"C:\Users\26346\Desktop\project\dafors"
sys.path.insert(0, os.path.join(ROOT, "tools"))
import _r08_harness as H
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont

SHOTS = H.SHOTS
FONTP = r"C:\Windows\Fonts\msyh.ttc"


def F(sz):
    return ImageFont.truetype(FONTP, sz)


def grab(page, clip=None):
    png = page.screenshot(clip=clip, animations="disabled")
    return Image.open(io.BytesIO(png)).convert("RGB")


def board(img, label, caps, boxes):
    W = img.width
    for bb, col in boxes:
        d = ImageDraw.Draw(img)
        d.rectangle([bb[0] - 3, bb[1] - 3, bb[0] + bb[2] + 3, bb[1] + bb[3] + 3], outline=col, width=3)
    bar = Image.new("RGB", (W, 36), (22, 27, 34))
    d = ImageDraw.Draw(bar)
    d.rectangle([0, 0, 4, 36], fill=(250, 204, 21))
    d.text((14, 6), label, font=F(19), fill=(232, 237, 245))
    out = Image.new("RGB", (W, img.height + 36 + 26 * len(caps) + 16), (10, 13, 18))
    out.paste(bar, (0, 0)); out.paste(img, (0, 36))
    d = ImageDraw.Draw(out)
    yy = 36 + img.height + 8
    for ln in caps:
        d.text((14, yy), ln, font=F(19), fill=(228, 234, 242))
        yy += 27
    return out


srv, H.PORT = H.serve(); port = H.PORT
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True, args=["--force-device-scale-factor=1"])
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    page = ctx.new_page(); page.set_default_timeout(20000)
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    H.reset_state(page, running=True, engine_state="fire", clicks=10)
    page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=20000)
    H.wait(page, 1200)
    ok_before = H.js(page, "()=>({conn:$('#sbConnTxt').textContent, g:$('#gState').textContent, d:$('#gDetail').textContent})")
    H.ctl(page, sched_500=True)
    H.wait(page, 4200)
    img = grab(page, None)
    st = dict(conn=H.js(page, "$('#sbConnTxt').textContent"),
              gstate=H.js(page, "$('#gState').textContent"),
              detail=H.js(page, "$('#gDetail').textContent"),
              toasts=H.js(page, "$$('#toastHost .toast').length"),
              toast_txt=H.js(page, "$('#toastHost').textContent.trim()"),
              act=H.js(page, "$('#btnStartTop').dataset.act"),
              txt=H.js(page, "$('#startTopTxt').textContent"))
    # 恢复：解除 500，1 次轮询内应自愈
    H.ctl(page, sched_500=False)
    H.wait(page, 1300)
    rec = dict(conn=H.js(page, "$('#sbConnTxt').textContent"),
               gstate=H.js(page, "$('#gState').textContent"),
               detail=H.js(page, "$('#gDetail').textContent"))
    img2 = grab(page, None)
    out = board(img, "当前版本（2026-11-07 06:49 build）：/api/scheduler 持续 500 已 4.2 秒",
                [f"R08-19 已修复：连接徽标变为「{st['conn']}」，状态带「{st['gstate']}」，详情「{st['detail']}」，右上角 {st['toasts']} 条 toast：「{st['toast_txt']}」。",
                 f"解除 500 后 1.3 秒：状态带「{rec['gstate']}」、详情「{rec['detail']}」已自愈；徽标因 6 秒冷却仍短暂显示「{rec['conn']}」。"],
                [((0, 0, 1430, 116), (220, 38, 38)), ((0, 862, 1430, 34), (250, 204, 21))])
    p = os.path.join(SHOTS, "R08-19_current_500_warning.png")
    out.save(p)
    # 恢复后一帧，单独存
    img2.save(os.path.join(SHOTS, "R08-19_current_recovered.png"))
    print(json.dumps(dict(errors=errs[:2], before=ok_before, during=st, recovered=rec, file=p), ensure_ascii=False, indent=1))
    b.close()
srv.shutdown()
