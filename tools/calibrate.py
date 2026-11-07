"""离线标定工具：截模板 + 覆盖区域坐标（批量 / 无界面 / CI）。

9 个区域的坐标已经内置在 `core/region_table.py`（1080p 客户区基准），
运行时按实际游戏客户区自动换算，开抢前还会自动校验锚点。买家不需要
用这个工具，界面「高级设置 → 区域微调」只在自动标定反复失败时才用。

这个命令行工具留给批量部署 / 无界面 / CI 场景，用来：
  · 换掉 templates/ 里的 buy/confirm 图样；
  · 给特殊界面版本覆盖某几个区域坐标（写回 config.json 的 regions）。

用法（不需要打开任何窗口，开抢前跑一次即可）::

    # 交互式：按提示输入像素框
    SJZ-Control.exe --calibrate --workflow countdown
    python tools/calibrate.py --workflow countdown

    # 非交互（CI / 远程 / 无控制台 exe）：框全部用参数给
    python tools/calibrate.py --workflow countdown \
        --box clock=1200,80,300,60 --box buy=1400,700,240,90 \
        --box confirm=760,520,180,70

    # 复用已有截图（人不在游戏前，先截屏带走再标）
    python tools/calibrate.py --workflow countdown --screenshot full.png

    # 完全非交互：按当前画面自动对齐/重采模板（启动前检查用的就是这条）
    python tools/calibrate.py --auto

    # 回滚最近一次自动改写的模板
    python tools/calibrate.py --restore-templates
    python tools/calibrate.py --restore-templates buy

坐标一律是**选中显示器截图内的物理像素**：左上角为 (0,0)，格式
`x,y,w,h`；程序抓屏保存的 PNG 可以直接用画图/截图工具量。

每条流程必须标定的项（来自 core/engine.py 的 start_check）::

    countdown : 区域 clock / buy / confirm，模板 confirm
    list      : 区域 list / buy，        模板 buy
    detail    : 区域 buy / confirm，      模板 buy + confirm

模板框默认取「区域框向外各扩 8%」——区域要比模板大一圈，按钮位置有几
像素漂移也能搜到；区域和模板一样大时，只有分毫不差才匹配得上。
"""
import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import cv2                      # noqa: E402

from core import config as config_mod   # noqa: E402
from core import vision                 # noqa: E402

# 每条流程必须的项。与 core/engine.py:start_check 一一对应，改流程时一起改。
PROFILES = {
    "countdown": {"regions": ["clock", "buy", "confirm"],
                  "templates": ["confirm"],
                  "optional": ["name", "price", "bar"]},
    "list": {"regions": ["list", "buy"],
             "templates": ["buy"],
             "optional": ["list_refresh", "name", "price"]},
    "detail": {"regions": ["buy", "confirm"],
               "templates": ["buy", "confirm"],
               "optional": ["name", "price", "bar"]},
}

REGION_CN = {
    "clock": "游戏内倒计时数字（只要数字本身）",
    "buy": "购买按钮（整个按钮，含边框）",
    "confirm": "确认弹窗里的确认按钮",
    "list": "交易行列表（整块可滚动区域）",
    "list_refresh": "列表刷新按钮（可选）",
    "name": "商品名称文字（可选）",
    "price": "价格文字（可选）",
    "bar": "详情弹窗价格条（可选）",
}


def _ask(prompt, default=None):
    """读一行输入。无控制台（windowed exe）时给出可读指引而不是崩掉。"""
    tip = f"（回车用默认 {default}）" if default else ""
    try:
        raw = input(f"{prompt}{tip}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print("当前没有可交互的控制台。请在 cmd 里运行：")
        print("  SJZ-Control.exe --calibrate --workflow <countdown|list|detail>")
        print("或用参数模式把每个框都给出：")
        print("  python tools/calibrate.py --workflow countdown "
              "--box clock=1200,80,300,60 --box buy=1400,700,240,90 ...")
        raise SystemExit(3)
    if not raw and default:
        return default
    return raw


def _parse_box(text, size=None):
    """`x,y,w,h` → (x, y, w, h)；顺带做边界校验。"""
    parts = [p for p in str(text).replace("，", ",").replace(" ", ",").split(",") if p]
    if len(parts) != 4:
        raise ValueError(f"要 4 个数 x,y,w,h，收到 {text!r}")
    x, y, w, h = (int(round(float(p))) for p in parts)
    if w < 8 or h < 8:
        raise ValueError(f"框太小：{w}×{h}（至少 8×8）")
    if size:
        sw, sh = size
        if x < 0 or y < 0 or x + w > sw or y + h > sh:
            raise ValueError(f"框超出截图范围 {sw}×{sh}：x={x} y={y} w={w} h={h}")
    return x, y, w, h


def _expand(box, ratio=0.08, size=None):
    x, y, w, h = box
    dx, dy = int(round(w * ratio)), int(round(h * ratio))
    nx, ny = max(0, x - dx), max(0, y - dy)
    nw, nh = w + 2 * dx, h + 2 * dy
    if size:
        nw = min(nw, size[0] - nx)
        nh = min(nh, size[1] - ny)
    return nx, ny, nw, nh


def _norm(box, size):
    x, y, w, h = box
    sw, sh = size
    return {"x": round(x / sw, 4), "y": round(y / sh, 4),
            "w": round(w / sw, 4), "h": round(h / sh, 4)}


def _templates_dir(cfg):
    path = cfg.template_path("__probe__")
    return os.path.dirname(path)


def _update_meta(tdir, screen, saved):
    """写 templates/meta.json：采集屏尺寸/DPI + 每张模板的像素尺寸。

    core/vision.py 靠这份元数据把模板重采样到当前分辨率/缩放；没有它
    只能退化为多尺度搜索（每帧多几次 matchTemplate，还会多一分误配风险）。
    """
    meta_path = os.path.join(tdir, "meta.json")
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            meta = {}
    except (OSError, ValueError):
        meta = {}
    meta["schema"] = 1
    meta["capture_screen"] = [int(screen.size[0]), int(screen.size[1])]
    meta["dpi"] = int(getattr(screen, "dpi", 96))
    meta["captured_on"] = time.strftime("%Y-%m-%d %H:%M:%S")
    entries = meta.setdefault("templates", {})
    for name, shape in saved.items():
        entries[f"{name}.png"] = {
            "size_px": [int(shape[1]), int(shape[0])],
            "capture_screen": [int(screen.size[0]), int(screen.size[1])],
        }
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        return meta_path
    except OSError as e:
        print(f"!! 写 {meta_path} 失败：{e}")
        return None


def _grab(screen):
    img = screen.grab()
    return img


def _auto(args):
    """非交互自愈：对齐 / 重采模板，或回滚最近一次快照。

    和「启动前检查」调的是同一个 `core.template_heal.heal()`：只抓屏，
    不开窗口、不动光标、不弹输入框。给批量部署 / CI / 远程用。
    """
    from core import template_heal

    cfg = config_mod.Config.load()
    if args.restore_templates is not None:
        name = (args.restore_templates or "").strip() or None
        done = template_heal.restore(name)
        if not done:
            print("没有可回滚的模板快照（template_backups 为空）")
            return 1
        print("已回滚：" + "、".join(done))
        return 0

    diag = template_heal.diagnose(cfg, workflow="detail")
    envv = diag["env"]
    print("=" * 64)
    print("模板自愈（非交互）")
    print("=" * 64)
    print(f"DPI 感知 {envv['awareness_name']}｜显示器 {envv['dpi']} DPI / "
          f"{envv['scale_percent']}%")
    print(f"客户区 {envv['client'][0]}×{envv['client'][1]}"
          + ("（游戏窗口）" if envv["client_is_window"]
             else "（没找到游戏窗口，按显示器算）"))
    print(f"模板元数据：采集屏 {envv['meta_capture_screen']} / DPI "
          f"{envv['meta_dpi']} → 屏幕比例 {envv['screen_ratio']}×，"
          f"DPI 比例 {envv['dpi_ratio']}×")
    for name, info in diag["templates"].items():
        fit = f"{info['fit']:.2f}×" if info["fit"] else "否（需等比补偿）"
        print(f"  [{name}] 模板 {info['size'][0]}×{info['size'][1]}｜区域 "
              f"{info['region_px'][2]}×{info['region_px'][3]}｜搜索窗 "
              f"{info['search_px'][2]}×{info['search_px'][3]}"
              f"（外扩 {info['pad']:.0%}）｜可塞下 {fit}")
    print()

    heal = template_heal.heal(cfg, workflow="detail", reason="cli --auto")
    print(f"结果：{heal['status']} —— {heal.get('message') or '无说明'}")
    for act in heal.get("actions") or []:
        print(f"  · {act}")
    for name, t in (heal.get("templates") or {}).items():
        score = f"｜分数 {t['score']:.2f}" if t.get("score") is not None else ""
        print(f"  [{name}] {t['state']}｜{t.get('message') or ''}{score}")
    if heal.get("backup"):
        print(f"旧文件快照：{heal['backup']}")
        print("回滚：python tools/calibrate.py --restore-templates")
    return 0 if heal.get("ok") else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="SJZ 首次标定：模板 + 区域")
    ap.add_argument("--workflow", default="countdown",
                    choices=sorted(PROFILES), help="要标定的流程")
    ap.add_argument("--screenshot", default="",
                    help="用已有整屏截图代替实时抓屏（坐标相对这张图）")
    ap.add_argument("--box", action="append", default=[],
                    metavar="NAME=x,y,w,h",
                    help="直接给定区域/模板框，可重复；模板框名加 tpl: 前缀，"
                         "如 --box buy=... --box tpl:buy=...")
    ap.add_argument("--monitor", default=1,
                    help="显示器编号（默认 1=自动跟随游戏窗口）")
    ap.add_argument("--dry-run", action="store_true", help="只打印步骤，不抓屏不写盘")
    ap.add_argument("--auto", action="store_true",
                    help="非交互自愈：按当前画面自动对齐/重采模板，"
                         "不弹任何输入框（等价于启动前检查里那一步）")
    ap.add_argument("--restore-templates", nargs="?", const="", default=None,
                    metavar="NAME",
                    help="把最近一次模板快照回滚到 templates/（可只回滚一张，"
                         "如 --restore-templates buy）")
    args = ap.parse_args(argv)

    if args.auto or args.restore_templates is not None:
        return _auto(args)

    profile = PROFILES[args.workflow]
    need_regions = list(profile["regions"])
    need_templates = list(profile["templates"])
    optional = list(profile["optional"])

    print("=" * 64)
    print(f"SJZ 首次标定 — 流程 {args.workflow}")
    print("=" * 64)
    print("第 0 步：把游戏切到对应页面")
    if args.workflow == "countdown":
        print("         · 打开商品详情页，能看到购买按钮（公示期倒计时）")
    elif args.workflow == "list":
        print("         · 打开交易行在售列表")
    else:
        print("         · 打开商品详情页（有购买/确认按钮）")
    print("第 1 步：抓整屏（不开窗、不动鼠标）")
    print("第 2 步：按提示输入像素框 x,y,w,h（相对截图左上角）")
    print("第 3 步：写 templates/*.png + templates/meta.json + config.json 的 regions")
    print(f"必须区域：{', '.join(need_regions)}")
    print(f"必须模板：{', '.join(need_templates)}")
    print(f"可选区域：{', '.join(optional) or '无'}")
    print()

    if args.dry_run:
        for name in need_regions:
            print(f"  [{name}] {REGION_CN.get(name, name)}")
        for name in need_templates:
            print(f"  [模板 {name}] 默认 = {name} 区域向外扩 8%")
        return 0

    cfg = config_mod.Config.load()
    size = None
    img = None
    if args.screenshot:
        img = vision.imread(args.screenshot)
        if img is None:
            print(f"!! 读不了截图：{args.screenshot}")
            return 2
        size = (img.shape[1], img.shape[0])
    else:
        from core.screen import Screen
        screen = Screen(args.monitor)
        print(f"选中显示器 {screen.index}：{screen.pick_reason}，"
              f"{screen.size[0]}×{screen.size[1]}，DPI {screen.dpi}（{screen.scale:.2f}×）")
        img = _grab(screen)
        size = screen.size

    shot_dir = os.path.join(config_mod.DATA_DIR, "calibration")
    os.makedirs(shot_dir, exist_ok=True)
    shot_path = os.path.join(
        shot_dir, f"{args.workflow}_{time.strftime('%Y%m%d_%H%M%S')}.png")
    vision.imwrite(shot_path, img)
    print(f"整屏截图已存：{shot_path}")
    print(f"用画图打开它量出每个框的像素坐标（左上角为 0,0）。")
    print()

    # 参数里预先给的框
    preset = {}
    for item in args.box:
        if "=" not in item:
            print(f"!! --box 格式要 NAME=x,y,w,h，忽略：{item!r}")
            continue
        name, val = item.split("=", 1)
        preset[name.strip()] = val.strip()

    regions = {}
    templates = {}
    for name in need_regions:
        default = None
        tpl_hint = preset.get(f"tpl:{name}")
        if tpl_hint:
            try:
                default = ",".join(str(v) for v in _expand(_parse_box(tpl_hint, size), size=size))
            except ValueError:
                default = None
        default = preset.get(name, default)
        if name in preset:
            raw = preset[name]
        else:
            raw = _ask(f"[区域 {name}] {REGION_CN.get(name, name)}", default)
        while True:
            try:
                box = _parse_box(raw, size)
                regions[name] = box
                break
            except ValueError as e:
                print(f"  !! {e}")
                raw = _ask(f"[区域 {name}] 重新输入 x,y,w,h")

    for name in need_templates:
        tpl_key = f"tpl:{name}"
        default = preset.get(tpl_key)
        if not default and name in regions:
            default = ",".join(str(v) for v in _expand(regions[name], size=size))
        raw = default if tpl_key in preset else _ask(
            f"[模板 {name}] 按钮紧贴框（比区域小一圈更稳）", default)
        while True:
            try:
                tpl_box = _parse_box(raw, size)
                break
            except ValueError as e:
                print(f"  !! {e}")
                raw = _ask(f"[模板 {name}] 重新输入 x,y,w,h")

        x, y, w, h = tpl_box
        crop = img[y:y + h, x:x + w].copy()
        if crop.size == 0:
            print(f"  !! {name} 框裁出来是空的，跳过")
            continue
        templates[name] = crop

    # ---- 落盘 + 回验 ----
    tdir = _templates_dir(cfg)
    os.makedirs(tdir, exist_ok=True)
    saved_shapes = {}
    for name, crop in templates.items():
        path = cfg.template_path(name)
        vision.imwrite(path, crop)
        saved_shapes[name] = crop.shape
        print(f"模板已写：{path}（{crop.shape[1]}×{crop.shape[0]}）")

    if not args.screenshot:
        for name, crop in templates.items():
            x, y, w, h = _expand(regions.get(name, (0, 0, crop.shape[1], crop.shape[0])),
                                 size=size)
            roi = img[y:min(y + h, size[1]), x:min(x + w, size[0])]
            score, loc = vision.match(vision.to_gray(roi), vision.to_gray(crop))
            print(f"回验 {name}：匹配分数 {score:.4f}"
                  f"（阈值 {cfg.get('threshold')}）{'通过' if score >= float(cfg.get('threshold') or 0.85) else '偏低，检查框是否贴紧按钮'}")
        meta = _update_meta(tdir, screen, saved_shapes)
        if meta:
            print(f"元数据已写：{meta}")
    else:
        print("（用 --screenshot 模式，跳过实时回验；请下次启动前用 --doctor 看模板尺寸）")

    with config_mod.Config.transaction() as tx:
        for name, box in regions.items():
            tx.set_region(name, _norm(box, size))
        tx["workflow"] = args.workflow

    print()
    print("区域已写入 config.json：")
    with config_mod.Config.transaction() as tx:
        for name in need_regions:
            print(f"  {name:<6} = {tx.region(name)}")
    print()
    print("下一步：运行 `SJZ-Control.exe --doctor`（源码 `python desktop.py --doctor`）")
    print("确认「游戏窗口 / 模板 / 区域」三项全绿，再开抢。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
