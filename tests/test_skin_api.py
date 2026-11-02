"""典藏外观：目录查询、标记目标、定位。

- 目录要给出「每个赛季 × 每个品阶」下的皮肤，并且每件都带**枪种**
  （枪种是筛选面板自己给的判据，比从型号推断可靠）
- 标记目标要能存进 config.json（换句话说「我就是要这一件」得记住）
- 定位失败要给出原因，不能默默成功
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import bricks  # noqa: E402
from core.nav_col import NavColPanel  # noqa: E402
from core.nav_coords import COL_GUN_X, COL_GUN_X2, COL_GUN_Y  # noqa: E402


def test_catalog_covers_every_season_and_tier():
    rows = bricks.catalog()
    assert rows, "目录是空的"
    seasons = {r["season"] for r in rows}
    assert len(seasons) >= 11, f"只有 {len(seasons)} 个赛季"
    tiers = {r["tier"] for r in rows}
    assert tiers == {"传说", "史诗", "稀有", "普通"}


def test_every_skin_knows_its_gun_type():
    """枪种要标出来 —— 它是筛选面板里的一个维度。

    型号认不准没关系（AS Val → ASVa），枪种是准的。
    """
    unknown = [r["name"] for r in bricks.catalog() if not r.get("gun")]
    assert len(unknown) <= 2, f"有 {len(unknown)} 件没标枪种：{unknown[:5]}"


def test_skins_of_filters_by_gun_type():
    rows = bricks.catalog()
    if not rows:
        return
    gun = next((r["gun"] for r in rows if r.get("gun")), None)
    if not gun:
        return
    got = bricks.skins_of(tier=rows[0]["tier"], season=rows[0]["season"])
    assert all(r["gun"] == gun for r in got)


def test_gun_filter_rows_cover_the_seven_types():
    """筛选面板的枪种是**两行**：步枪/冲锋枪/霰弹枪/轻机枪 + 精确射手/狙击/手枪。"""
    assert len(NavColPanel.GUN_ROWS) == 7
    assert COL_GUN_Y == (558, 614), "两行枪种的 y 变了"
    assert len(COL_GUN_X) == 4 and len(COL_GUN_X2) == 3
    rows = {name: (row, idx) for name, (row, idx) in NavColPanel.GUN_ROWS.items()}
    assert rows["步枪"] == ("gun", 0)
    assert rows["轻机枪"] == ("gun", 3)
    assert rows["精准射手步枪"] == ("gun2", 0)
    assert rows["手枪"] == ("gun2", 2)


# ---------------------------------------------------------------- HTTP

@pytest.fixture
def server(tmp_path, monkeypatch):
    """起一个真服务（用临时 config.json，不碰用户自己的配置）。"""
    import threading
    import time
    import urllib.request

    import core.config as cfgmod
    from core.server import Handler, ThreadingHTTPServer

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"queue": []}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))

    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def _get(base, path):
    import urllib.request
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(base, path, payload):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_skins_endpoint_lists_seasons_and_tiers(server):
    d = _get(server, "/api/skins")
    assert d["ok"] is True
    assert len(d["seasons"]) >= 11
    s = d["seasons"][0]
    assert s["season"] and s["season_name"]
    assert "传说" in s["tiers"]


def test_target_can_be_set_and_cleared(server):
    rows = bricks.catalog()
    if not rows:
        return
    name = rows[0]["name"]
    code, data = _post(server, "/api/skins/target", {"name": name})
    assert code == 200 and data["ok"]
    assert data["target"]["name"] == name
    assert data["target"]["tier"] == rows[0]["tier"]

    code, data = _post(server, "/api/skins/target", {"name": ""})
    assert code == 200 and data["target"] == {}


def test_locate_with_an_unknown_name_is_refused(server):
    code, data = _post(server, "/api/skins/locate", {"name": "根本没有这个皮肤xyz"})
    assert code == 404
    assert not data["ok"]


# ---------------------------------------------------------------- 筛选档位一致性

def test_every_skin_in_the_catalog_maps_to_a_filter_slot():
    """每件皮肤的 (品阶, 枪种) 都必须是筛选面板里真有的档位。

    这是「在筛选里准确找到对应枪皮」的第一环：`set_tier` / `set_gun`
    只认 4 个品阶、7 个枪种（NavColPanel.GUN_ROWS），知识库要是给出
    「通用机枪」这种面板里没有的名字，`set_gun` 直接抛 NavError，
    定位在第一步就断了，而且报错信息看不出是知识库的问题。
    """
    tiers = ("传说", "史诗", "稀有", "普通")
    bad = []
    for row in bricks.catalog():
        if row["tier"] not in tiers or row["gun"] not in NavColPanel.GUN_ROWS:
            bad.append((row["name"], row["tier"], row["gun"]))
    assert not bad, f"这些皮肤映射不到筛选档位：{bad[:5]}"


def test_visible_names_in_the_real_screenshot_all_resolve():
    """真机截图里那七件，名字丢给知识库都要能解析出筛选参数。

    名字是 OCR 读出来的（带破折号、可能有错字），解析必须容错 ——
    `find_skin` 是相似度匹配，不是字符串相等。
    """
    names = ["AS Val突击步枪-夜穹", "M16A4突击步枪-夜穹", "腾龙突击步枪-夜穹",
             "QBZ95-1突击步枪-叠锋", "PTR-32突击步枪-幻夜",
             "SCAR-H战斗步枪-幻夜", "G3战斗步枪-叠锋"]
    for n in names:
        hit = bricks.find_skin(n)
        assert hit, f"解析不出：{n}"
        assert hit["score"] >= 0.8, f"{n} 只匹配到 {hit['name']}（{hit['score']:.2f}）"
        assert hit["gun"] in NavColPanel.GUN_ROWS
