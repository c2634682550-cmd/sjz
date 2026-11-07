import json, time, threading
from http.server import ThreadingHTTPServer
import sys
sys.path.insert(0, r"C:\Users\26346\Desktop\project\dafors\tools")
import _r08_harness as H
srv, port = H.serve()
H.PORT = port
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    pg = b.new_page()
    errs=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.type+":"+m.text) if m.type=="error" else None)
    H.reset_state(pg, tasks=[H.mk_task(1), H.mk_task(2)])
    pg.goto(f"http://127.0.0.1:{port}/", wait_until="load")
    pg.wait_for_timeout(1500)
    print("errors:", errs[:6])
    for e in ["typeof S", "S && S.tasks && S.tasks.length", "JSON.stringify([S.sel, typeof S.sel])", "taskMode"]:
        try:
            print(e, "=>", pg.evaluate("()=>(" + e + ")"))
        except Exception as ex:
            print(e, "ERR", ex)
    pg.screenshot(path=r"C:\Users\26346\Desktop\project\dafors\_r08_shots\debug.png")
    b.close()
srv.shutdown()
