# -*- coding: utf-8 -*-
"""Verify whether the 5 high-sev R08 items still reproduce on the current web/index.html."""
import io, os, sys, json, time, urllib.request
ROOT = r"C:\Users\26346\Desktop\project\dafors"
sys.path.insert(0, os.path.join(ROOT, "tools"))
import _r08_harness as H
from playwright.sync_api import sync_playwright

SHIM = ("window.loadLicense = async () => ({});"
        "window.maybeGuide = () => {};"
        "window.guideGo = () => {};")
OUT = {}

def newpage(ctx, shim=True):
    p = ctx.new_page(); p.set_default_timeout(20000)
    if shim:
        p.add_init_script(SHIM)
    return p

def boot(page, port):
    page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=20000)
    for _ in range(60):
        if H.js(page, "!!S.pollTimer"): return True
        H.wait(page, 100)
    return False

def main():
    srv, H.PORT = H.serve(); port = H.PORT
    print("mock on", port)
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--force-device-scale-factor=1"])
        ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)

        # A. 不注入 shim：确认启动错误
        page = newpage(ctx, shim=False)
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
        page.wait_for_selector("#boot", state="detached", timeout=20000)
        H.wait(page, 2500)
        OUT["boot"] = dict(pageerrors=errs[:3], pollTimer=H.js(page, "!!S.pollTimer"),
                           ready=H.js(page, "S.ready"), v=H.js(page, "S.status && S.status._v"))
        page.close()

        # A2. 注入 shim 后能否正常启动
        page = newpage(ctx)
        OUT["boot_shim"] = dict(ok=boot(page, port), pollTimer=H.js(page, "!!S.pollTimer"))
        page.close()

        # 1. 轮询竞态
        page = newpage(ctx)
        H.reset_state(page, running=True, engine_state="fire", clicks=100)
        boot(page, port); H.wait(page, 1200)
        H.ctl(page, sched_delay=[0.05, 0.05, 3.0, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05])
        H.wait(page, 2200)
        H.ctl(page, clicks=5000)
        samples = []; drops = []
        t0 = time.time(); last = None
        while time.time() - t0 < 9:
            v = H.js(page, "S.status && S.status._v")
            det = H.js(page, "$('#gDetail').textContent")
            samples.append((round(time.time() - t0, 1), v, det))
            if last and v is not None and last < v:
                pass
            if last is not None and v is not None and v < last:
                drops.append((last, v))
            last = v
            H.wait(page, 50)
        mx = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/__ctl", timeout=5).read())["max_inflight"]
        jumps = [(samples[i - 1][2], samples[i][2]) for i in range(1, len(samples))
                 if samples[i - 1][2] and samples[i][2] and "5000" in samples[i - 1][2] and "100" in samples[i][2] and "5000" not in samples[i][2]]
        OUT["R08-01"] = dict(max_inflight=mx, v_drops=drops[:5], visible_jumps=jumps[:3], n=len(samples))
        page.close()

        # 3. 添加连点
        page = newpage(ctx)
        H.reset_state(page, tasks=[H.mk_task(1)], add_delay=0.5)
        boot(page, port)
        adds = {"n": 0}
        page.on("request", lambda r: adds.__setitem__("n", adds["n"] + 1) if "/api/tasks/add" in r.url else None)
        page.evaluate("()=>openAdd()"); H.wait(page, 250)
        page.fill("#addName", "连点测试-同名")
        page.evaluate("()=>{const b=document.querySelector('#addOk');b.click();b.click();b.click();}")
        H.wait(page, 120)
        dis = H.js(page, "$('#addOk').disabled")
        H.wait(page, 2200)
        OUT["R08-03"] = dict(posts=adds["n"], disabled_inflight=dis, tasks=H.js(page, "S.tasks.length"))
        page.close()

        # 6. 勾选清空
        page = newpage(ctx)
        H.reset_state(page, tasks=[H.mk_task(i) for i in range(1, 7)])
        boot(page, port); H.wait(page, 400)
        for i in range(3):
            page.locator(".rowchk").nth(i).click()
        H.wait(page, 250)
        nb = H.js(page, "$$('.rowchk:checked').length")
        H.wait(page, 2600)
        OUT["R08-06"] = dict(before=nb, after=H.js(page, "$$('.rowchk:checked').length"),
                             chkall=H.js(page, "$('#chkAll').checked"))
        page.close()

        # 7. 错误页固化
        page = newpage(ctx)
        H.reset_state(page, tasks=[H.mk_task(1), H.mk_task(2)], tasks_500_once=True)
        boot(page, port)
        modes = []
        t0 = time.time()
        while time.time() - t0 < 5:
            modes.append((round(time.time() - t0, 1), H.js(page, "taskMode"), H.js(page, "S.tasks.length")))
            H.wait(page, 400)
        OUT["R08-07"] = dict(modes=modes, text=H.js(page, "$('#taskRows').textContent.replace(/\\s+/g,' ').trim().slice(0,50)"))
        page.close()

        # 19. 500 静默
        page = newpage(ctx)
        H.reset_state(page, running=True, engine_state="fire", clicks=10)
        boot(page, port); H.wait(page, 1200)
        H.ctl(page, sched_500=True)
        H.wait(page, 4000)
        before_toast = H.js(page, "$('#toastHost').textContent.trim()")
        before_n = H.js(page, "$$('#toastHost .toast').length")
        before_det = H.js(page, "$('#gDetail').textContent")
        page.evaluate("()=>document.querySelector('#btnStartTop').click()")
        H.wait(page, 1500)
        OUT["R08-19"] = dict(conn=H.js(page, "$('#sbConnTxt').textContent"),
                             overlay_hidden=H.js(page, "$('#connOverlay').hidden"),
                             toast=before_toast, toast_n=before_n,
                             toast_after=H.js(page, "$('#toastHost').textContent.trim()"),
                             gstate=H.js(page, "$('#gState').textContent"),
                             detail_before=before_det, detail_after=H.js(page, "$('#gDetail').textContent"),
                             act=H.js(page, "$('#btnStartTop').dataset.act"),
                             txt=H.js(page, "$('#startTopTxt').textContent"))
        page.close()
        b.close()
    srv.shutdown()
    print(json.dumps(OUT, ensure_ascii=False, indent=1))
    with open(os.path.join(H.SHOTS, "recap_verify.json"), "w", encoding="utf-8") as f:
        json.dump(OUT, f, ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()
