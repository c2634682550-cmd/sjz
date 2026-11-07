"""站点配置：默认值 + portal/config.json + PORTAL_* 环境变量。

所有路径都收敛到 PROJECT_ROOT 下；密钥类材料只存在 var/ 目录里、
权限 600，不进数据库、不进日志。
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORTAL_ROOT = os.path.join(PROJECT_ROOT, "portal")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import license as core_license        # noqa: E402

DEFAULTS = {
    # 监听（只给 run.py / 文档用，WSGI 部署时由 gunicorn 决定）
    "host": "127.0.0.1",
    "port": 8080,
    # 站点名（页面标题）
    "site_name": "SJZ 卡密下载站",
    # 下载令牌
    "token_ttl_seconds": 60,
    # 卡密默认每天可下载次数
    "default_max_per_day": 5,
    # 可选设备绑定总开关（单卡还可以再开）
    "device_binding_enabled": False,
    # 单 IP 提交频次：window 秒内最多 limit 次
    "ip_limit": 10,
    "ip_window_seconds": 60,
    # 同一 IP 失败多少次后要求验证码
    "captcha_after_failures": 5,
    "fail_window_seconds": 600,
    # 同一卡密失败多少次后临时锁定（秒）
    "card_fail_limit": 5,
    "card_fail_window_seconds": 600,
    "card_fail_lock_seconds": 600,
    # 后台登录
    "admin_max_failures": 5,
    "admin_lock_seconds": 900,
    "session_lifetime_seconds": 8 * 3600,
    "session_cookie_secure": False,      # 上 HTTPS 后置 true
    # 上传
    "max_upload_bytes": 200 * 1024 * 1024,
    "max_zip_entries": 5000,
    "max_zip_uncompressed_bytes": 2 * 1024 * 1024 * 1024,
    "max_zip_ratio": 100,
    # 时区（统计按这个时区切天）
    "timezone": "Asia/Shanghai",
    # 签名私钥：只在仓库外（SJZ_SIGNING_KEY 或受控目录），仓库里只有公钥。
    # 与 tools/license_server.py 用同一个解析函数，保证指向同一套。
    "signing_key_file": core_license.default_signing_key_path(),
    # 是否信任反向代理的 X-Forwarded-For（nginx 前面必须开）
    "trust_proxy": False,
    # 预留：抢购/数据看板对接 token；空 = 接口关闭
    "integration_token": "",
    # 是否只接受 HTTPS（会加 HSTS 并把 cookie 标记 Secure）
    "https_only": False,
}

_ENV_MAP = {
    "PORTAL_HOST": ("host", str),
    "PORTAL_PORT": ("port", int),
    "PORTAL_TOKEN_TTL": ("token_ttl_seconds", int),
    "PORTAL_DB": ("db_path", str),
    "PORTAL_VAR_DIR": ("var_dir", str),
    "PORTAL_SIGNING_KEY": ("signing_key_file", str),
    "PORTAL_TRUST_PROXY": ("trust_proxy", lambda v: v == "1"),
    "PORTAL_HTTPS_ONLY": ("https_only", lambda v: v == "1"),
    "PORTAL_INTEGRATION_TOKEN": ("integration_token", str),
    "PORTAL_MAX_UPLOAD": ("max_upload_bytes", int),
}

_SECRET_FILES = ("secret_key", "card_pepper")


def _read_or_create_secret(path: str, nbytes: int = 32) -> str:
    """读 32 字节十六进制密钥；不存在就生成并 chmod 600。"""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
        if len(value) >= 32:
            return value
    value = secrets.token_hex(nbytes)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return value


class Config:
    """一个实例 = 一套运行目录。测试用不同 var_dir 即可完全隔离。"""

    def __init__(self, overrides: dict | None = None):
        data = dict(DEFAULTS)
        cfg_file = os.environ.get("PORTAL_CONFIG") or os.path.join(
            PORTAL_ROOT, "config.json")
        if os.path.isfile(cfg_file):
            with open(cfg_file, encoding="utf-8") as f:
                data.update(json.load(f) or {})
        for env_key, (field, cast) in _ENV_MAP.items():
            raw = os.environ.get(env_key)
            if raw not in (None, ""):
                try:
                    data[field] = cast(raw)
                except ValueError:
                    pass
        if overrides:
            data.update(overrides)

        self._data = data
        self.var_dir = os.path.abspath(
            data.get("var_dir") or os.path.join(PORTAL_ROOT, "var"))
        self.db_path = os.path.abspath(
            data.get("db_path") or os.path.join(self.var_dir, "portal.db"))
        self.storage_dir = os.path.join(self.var_dir, "storage")
        self.log_dir = os.path.join(self.var_dir, "logs")
        self.export_dir = os.path.join(self.var_dir, "exports")
        self.tmp_dir = os.path.join(self.var_dir, "tmp")
        for path in (self.var_dir, self.storage_dir, self.log_dir,
                     self.export_dir, self.tmp_dir):
            os.makedirs(path, exist_ok=True)

        self.secret_key = _read_or_create_secret(
            os.path.join(self.var_dir, "secret_key"))
        self.card_pepper = _read_or_create_secret(
            os.path.join(self.var_dir, "card_pepper"))

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    @property
    def signing_key_file(self):
        return self._data["signing_key_file"]
