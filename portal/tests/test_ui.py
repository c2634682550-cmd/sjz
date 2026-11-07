"""后台与前台页面在 1366 / 手机宽度下不横向溢出（无头 Edge，不弹窗）。"""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("playwright.sync_api")

from werkzeug.serving import make_server                      # noqa: E402

from .conftest import create_admin, issue, login, upload_version  # noqa: E402

VIEWPORTS = {
    "desktop-1366": {"width": 1366, "height": 800},
    "mobile-390": {"width": 390, "height": 844},
}

PAGES = ["/", "/admin/login", "/admin/", "/admin/cards", "/admin/cards/new",
         "/admin/versions", "/admin/logs", "/admin/security"]


@pytest.fixture()
def live_server(app):
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _seed(app):
    create_admin(app)
    client = app.test_client()
    login(client)
    upload_version(client)
    issue(app, count=2)


def _overflow(page) -> tuple[int, int]:
    return page.evaluate(
        "() => [document.documentElement.scrollWidth, window.innerWidth]")


def test_no_horizontal_overflow(app, live_server, capsys):
    from playwright.sync_api import sync_playwright

    _seed(app)
    findings = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        for name, viewport in VIEWPORTS.items():
            context = browser.new_context(viewport=viewport, locale="zh-CN")
            page = context.new_page()
            page.goto(f"{live_server}/admin/login", wait_until="load")
            page.fill("#username", "admin")
            page.fill("#password", "Passw0rd!2345")
            page.click("button[type=submit]")
            page.wait_for_url("**/admin/**", timeout=10_000)

            for path in PAGES:
                page.goto(f"{live_server}{path}", wait_until="load")
                scroll_width, inner_width = _overflow(page)
                findings.append((name, path, scroll_width, inner_width,
                                 scroll_width > inner_width + 1))
                assert scroll_width <= inner_width + 1, (
                    f"{name} {path} 横向溢出 {scroll_width}>{inner_width}")
                # 手机宽度下顶部导航应可见（未被裁掉）
                assert page.is_visible("header.top .brand")
            context.close()
        browser.close()

    print("\n[ui] 视口 / 页面 / scrollWidth / innerWidth")
    for name, path, sw, iw, bad in findings:
        print(f"[ui] {name:12s} {path:18s} {sw:5d} / {iw:5d} "
              f"{'溢出' if bad else 'OK'}")
    assert len(findings) == len(VIEWPORTS) * len(PAGES)
    assert not any(row[4] for row in findings)


def test_pages_render_without_console_errors(app, live_server):
    from playwright.sync_api import sync_playwright

    _seed(app)
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport=VIEWPORTS["desktop-1366"])
        page = context.new_page()
        page.on("console", lambda msg: errors.append(msg.text)
                if msg.type == "error" else None)
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{live_server}/admin/login", wait_until="load")
        page.fill("#username", "admin")
        page.fill("#password", "Passw0rd!2345")
        page.click("button[type=submit]")
        page.wait_for_url("**/admin/**", timeout=10_000)
        for path in PAGES:
            page.goto(f"{live_server}{path}", wait_until="load")
        context.close()
        browser.close()
    assert not errors, errors
