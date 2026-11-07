"""卡密下载站 portal 包。"""
__all__ = ["create_app"]


def create_app(overrides=None):
    from .app import create_app as _create
    return _create(overrides)
