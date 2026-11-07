"""N9/E7：`10**400` 这类数值溢出经接口返回必须是可读 4xx，不许 500。

覆盖三处：
  1. `_save_config` 的数值字段（threshold/click_hz/...）
  2. `check_conditions` 的 console 数值（min_price/quota/...）
  3. `_json` 序列化兜底（Infinity/NaN/无法序列化的值）
"""
import json
import types

from core import server as S
from core.validate import check_conditions


def _fake_handler():
    return types.SimpleNamespace(
        _json=lambda payload, code=200: (payload, code))


def test_config_numeric_overflow_is_readable_400():
    fake = _fake_handler()
    payload, code = S.Handler._save_config(fake, {"threshold": 10 ** 400})
    assert code == 400                                # 不是 500
    assert payload["ok"] is False
    assert "threshold" in payload["msg"]
    assert "超出可表示范围" in payload["msg"]


def test_config_infinite_string_is_readable_400():
    for key in ("click_hz", "fire_timeout_s", "monitor", "list_max_tries"):
        payload, code = S.Handler._save_config(_fake_handler(), {key: "1e400"})
        assert code == 400, key
        assert "不是有限值" in payload["msg"] or "超出可表示范围" in payload["msg"]


def test_console_condition_overflow_is_error_not_exception():
    clean, errs = check_conditions({"min_price": 10 ** 400, "quota": 1})
    assert errs                                     # 可读错误
    assert "最低价" in errs[0]
    assert clean["min_price"] == 0


def test_json_dump_falls_back_to_400_for_unserializable_values():
    sent = {}

    def _send(code, body="", ctype="application/json; charset=utf-8"):
        sent["code"] = code
        sent["body"] = body

    fake = types.SimpleNamespace(_send=_send)
    S.Handler._json(fake, {"x": float("inf")}, 200)
    assert sent["code"] == 400
    payload = json.loads(sent["body"])
    assert payload["ok"] is False
    assert "无法序列化" in payload["msg"]
