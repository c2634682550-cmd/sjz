"""模板自愈：模板尺寸 / 采集环境跟当前屏幕对不上时，自动处理掉。

真机现场（2026-11-08）：启动前检查的「倒计时区域」常年亮红，原文
「模板 315×67 比筛选区域 298×58 还大 —— 模板是在更大分辨率/缩放下截的」，
然后把用户赶去命令行跑 `--calibrate` 手输像素框。实测那台机器

    进程 DPI 感知 per-monitor-aware(V2)，显示器 96 DPI / 100%，
    客户区 1920×1080，meta.json 里 capture_screen=1920×1080 / dpi=96

—— 环境完全一致，比例正好 1.0；而且 315/298=1.057、67/58=1.155 两个
比值不相等，真要是 125% 缩放，宽高会同时是 1.25。所以模板根本没有
「在更大分辨率下载」，红的是自检自己的口径（`core/preflight.py`
`_template_scale_issue` 用**未外扩**的区域比原始模板尺寸），外加倒计时
路线本来就不需要 buy 模板。

因此这里按代价从小到大做三件事：

  1) 几何对齐（只读）：用 `vision.scale_candidates` —— 也就是引擎真正
     会试的那套比例 —— 算模板缩放后能否塞进引擎的搜索窗
     （buy 外扩 0.15、confirm 外扩 0.25，与 `engine` 里 `expand()` 一致）。
     塞得下就判可用，一个字节都不写。
  2) 等比补偿（写盘，可回滚）：连候选比例都塞不进时，把模板重采样到刚好
     塞得进的比例，写回 PNG + meta.json（写前快照到用户数据目录）。
  3) 兜底重采（写盘，可回滚）：当前画面里模板「像但不够像」（分数在
     0.55 ~ 阈值之间，典型是界面改版/换皮），按框选区域从**当前画面**
     裁一张新模板写回。分数连 0.55 都不到时不采 —— 那说明按钮压根不在
     画面里，硬裁只会把背景存成模板，是更坏的静默失败。

约束（与项目既有约定一致）：
  · 只抓屏，不构造 `Mouse`、不移动光标、不抢焦点、不开任何窗口；
  · 不忙等；所有循环都是「几个候选比例」的有限循环；
  · 测试环境用 `DAFORS_TEMPLATE_WRITE=0` 关掉写盘（tests/conftest.py 已设），
    免得自愈把仓库里的模板改写掉。
"""

import json
import os
import time

import cv2

from . import template_backup
from . import vision

# 与 engine 的 `vision.expand(region, pad)` 参数一一对应 —— 改这里必须同步
# 改 engine，否则自检的「搜索窗」和引擎真正搜的范围就不是同一个东西。
SEARCH_PAD = {"buy": 0.15, "confirm": 0.25}
DEFAULT_PAD = 0.15

# 降采样下限：再小纹理就糊了（vision 深度搜的下限也是 0.5）。
MIN_FIT = 0.5
# 「像但不够像」的下限。低于它认为目标不在画面里，不重采。
RESCUE_MIN = 0.55
# 重采后自校验分数（裁的就是这块，理论上≈1.0）。
SELF_MIN = 0.97
# 裁出来的模板至少要有这么多灰度起伏，否则是纯色块/背景。
MIN_STD = 4.0
# 边缘像素占比下限，挡掉「一整片天空/墙」这种能过 std 但没有结构的块。
MIN_EDGE = 0.006

REQUIRED = {"list": ("buy",), "countdown": ("confirm",),
            "detail": ("buy", "confirm")}

_SKIP_WRITE = ("0", "false", "no", "off")


# ---------------------------------------------------------------- 环境 / 计划


def _meta_path():
    from . import config as _cfgmod
    return os.path.join(_cfgmod.TEMPLATE_DIR, "meta.json")


def _read_meta():
    try:
        with open(_meta_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _workflow_of(cfg):
    run = cfg.get("run") if isinstance(cfg.get("run"), dict) else {}
    return run.get("workflow") or cfg.get("workflow")


def required(workflow):
    """这条路线的自检真正要看的模板。与 engine `_validate` 同口径。"""
    return REQUIRED.get(workflow or "", REQUIRED["list"])


def writes_allowed():
    flag = os.environ.get("DAFORS_TEMPLATE_WRITE")
    if flag is None:
        return True
    return str(flag).strip().lower() not in _SKIP_WRITE


def env_facts(cfg, screen):
    """当前采集环境 vs 模板元数据。只读，给自检/界面看「到底差在哪」。"""
    from .screen import awareness_name

    meta = _read_meta()
    cap = meta.get("capture_screen")
    meta_dpi = meta.get("dpi")
    client = list(getattr(screen, "size", (0, 0)) or (0, 0))
    dpi = int(getattr(screen, "dpi", 96) or 96)
    ratio = dpi_ratio = 1.0
    try:
        if cap and len(cap) == 2 and cap[0] and cap[1] and client[0] and client[1]:
            ratio = round(((client[0] / float(cap[0]))
                            * (client[1] / float(cap[1]))) ** 0.5, 4)
    except (TypeError, ValueError, ZeroDivisionError):
        ratio = 1.0
    try:
        if meta_dpi:
            dpi_ratio = round(dpi / float(meta_dpi), 4)
    except (TypeError, ValueError, ZeroDivisionError):
        dpi_ratio = 1.0
    return {
        "awareness": int(getattr(screen, "awareness", 0) or 0),
        "awareness_name": awareness_name(int(getattr(screen, "awareness", 0) or 0)),
        "dpi": dpi,
        "scale": float(getattr(screen, "scale", 1.0) or 1.0),
        "scale_percent": int(round(float(getattr(screen, "scale", 1.0) or 1.0) * 100)),
        "client": client,
        "client_is_window": bool(getattr(screen, "client_rect", None)),
        "meta_capture_screen": list(cap) if isinstance(cap, (list, tuple)) else None,
        "meta_dpi": int(meta_dpi) if meta_dpi else None,
        "screen_ratio": ratio,
        "dpi_ratio": dpi_ratio,
    }


def plan(cfg, screen, workflow=None):
    """每张必需模板的「尺寸 / 区域 / 搜索窗 / 能用的缩放比例」。只读。"""
    out = {}
    meta = _read_meta()
    tpl_meta = meta.get("templates") or {}
    for name in required(workflow or _workflow_of(cfg)):
        path = cfg.template_path(name)
        box = cfg.region(name)
        if not box or not os.path.isfile(path):
            continue
        tpl = vision.imread(path, cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            continue
        tw, th = int(tpl.shape[1]), int(tpl.shape[0])
        pad = SEARCH_PAD.get(name, DEFAULT_PAD)
        region_px = tuple(int(v) for v in screen.norm_to_px(box))
        search_px = tuple(int(v) for v in
                          screen.norm_to_px(vision.expand(box, pad)))
        scales = vision.scale_candidates(tpl)
        fit = None
        for s in scales:
            if tw * s <= search_px[2] and th * s <= search_px[3]:
                fit = float(s)
                break
        need = min(search_px[2] / float(tw), search_px[3] / float(th))
        out[name] = {"path": path, "size": (tw, th),
                     "capture_screen": vision.capture_screen_of(tpl),
                     "scales": [float(s) for s in scales],
                     "pad": pad, "region_px": region_px,
                     "search_px": search_px, "fit": fit,
                     "need": round(need, 4),
                     "meta_size": (tpl_meta.get(name + ".png") or {}).get("size_px")}
    return out


# ------------------------------------------------------------------ 写盘 / 回滚


def _atomic_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        vision.forget_meta(path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _record_meta(name, size_px, env, action, extra=None):
    """把这一张模板新的采集环境写进 meta.json（只写单图条目 + 历史）。

    顶层 `capture_screen` / `dpi` 保持原样：那是「随包模板集」的环境。
    逐图条目由 `vision._meta_for()` 合并，重采过的那张才按今天的屏算比例。
    """
    meta = _read_meta()
    meta.setdefault("schema", 1)
    entry = (meta.setdefault("templates", {})).setdefault(name + ".png", {})
    entry.update({
        "size_px": [int(size_px[0]), int(size_px[1])],
        "capture_screen": list(env.get("client") or []),
        "dpi": int(env.get("dpi") or 96),
        "auto_healed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
    })
    if extra:
        entry.update(extra)
    hist = meta.get("auto_heal")
    if not isinstance(hist, list):
        hist = []
    hist.insert(0, {"at": entry["auto_healed_at"], "template": name + ".png",
                    "action": action, "size_px": entry["size_px"],
                    "capture_screen": entry["capture_screen"],
                    "reason": (extra or {}).get("reason") or ""})
    meta["auto_heal"] = hist[:5]
    return _atomic_json(_meta_path(), meta)


def _persist(name, img, env, action, reason, extra=None):
    """写模板 PNG（先快照，失败回滚）。返回 (是否成功, 备份目录, 说明)。"""
    dst = _meta_path()
    template_dir = os.path.dirname(dst)
    png = os.path.join(template_dir, name + ".png")
    snap = template_backup.save([png, dst], reason=f"{action}:{name}")
    if not snap:
        return False, "", ("无法把现有模板备份到用户数据目录"
                           "（%s），本次不覆盖" % template_backup.backup_dir())
    if not vision.imwrite(png, img):
        template_backup.restore(snap, template_dir)
        return False, snap, "模板写盘失败（%s），已回滚" % png
    if not _record_meta(name, (img.shape[1], img.shape[0]), env, action,
                        dict(extra or {}, reason=reason)):
        template_backup.restore(snap, template_dir)
        return False, snap, "meta.json 写不进去，模板已回滚"
    return True, snap, ""


# ------------------------------------------------------------------- 单张处理


def _crop(gray, box):
    x, y, w, h = [int(v) for v in box]
    if not gray.size:
        return None
    x, y = max(0, x), max(0, y)
    w = max(1, min(int(gray.shape[1]) - x, w))
    h = max(1, min(int(gray.shape[0]) - y, h))
    return gray[y:y + h, x:x + w]


def _near_center(loc, region_px, pad, tpl_px):
    """命中框的中心是否还压在框选区域中心附近（±15% 区域内）。

    比的是**中心到中心**，不是左上角到左上角：模板尺寸可能比区域小
    （比如 buy 的区域是按钮本体、模板带了一圈边），左上角天然就有偏差。
    """
    if not loc:
        return False
    rw, rh = float(region_px[2]), float(region_px[3])
    tw, th = float(tpl_px[0]), float(tpl_px[1])
    cx = rw * pad + rw / 2.0
    cy = rh * pad + rh / 2.0
    mx = float(loc[0]) + tw / 2.0
    my = float(loc[1]) + th / 2.0
    return abs(mx - cx) <= rw * 0.15 and abs(my - cy) <= rh * 0.15


def _texture_ok(patch):
    """裁出来的块是不是「有结构的东西」——纯色背景 / 渐变墙一律不要。"""
    if patch is None or patch.size < 16:
        return False, "裁出来的块太小"
    if float(patch.std()) < MIN_STD:
        return False, f"裁出来的块几乎是纯色（灰度标准差 {patch.std():.1f}）"
    edges = cv2.Canny(patch, 50, 150)
    ratio = float((edges > 0).mean())
    if ratio < MIN_EDGE:
        return False, f"裁出来的块没有结构（边缘占比 {ratio:.3%}）"
    return True, ""


def _one(name, info, gray, frame, page, threshold, allow_write):
    """单张模板：几何 → 复验 → 补偿 → 重采。返回状态字典。"""
    # 彩色原图留着写盘（PNG 里仍是彩色，和人手截的一致）；匹配一律走灰度。
    color = vision.imread(info["path"], cv2.IMREAD_COLOR)
    tpl = vision.to_gray(color)
    if tpl is None:
        return {"name": name, "state": "failed", "message": "模板文件读不出来"}
    tw, th = int(tpl.shape[1]), int(tpl.shape[0])
    sw, sh = info["search_px"][2], info["search_px"][3]
    actions = []
    backup = ""
    out = {"name": name, "size": (tw, th), "search_px": (sw, sh),
           "region_px": info["region_px"], "pad": info["pad"]}

    # ① 几何：候选比例里能塞进搜索窗吗
    if info["fit"] is None:
        need = info["need"]
        if not allow_write or need < MIN_FIT:
            out.update(state="failed", actions=actions,
                       message=(f"模板 {tw}×{th} 缩到 {need:.2f}× 才能塞进 "
                                f"{sw}×{sh} 的搜索窗"
                                + ("（低于安全下限 0.50×，重采样会糊）"
                                   if need < MIN_FIT else "；当前禁止写盘")))
            return out
        actions.append(f"等比补偿：{tw}×{th} → {need:.2f}×")
        new = cv2.resize(color, None, fx=need, fy=need,
                         interpolation=cv2.INTER_AREA)
        ok, backup, why = _persist(name, new, env=info["env"], action="rescaled",
                                   reason="模板比搜索窗大，等比补偿",
                                   extra={"fit": round(need, 4)})
        if not ok:
            out.update(state="failed", actions=actions, message=why)
            return out
        tpl = vision.to_gray(new)
        tw, th = int(tpl.shape[1]), int(tpl.shape[0])
        out.update(size=(tw, th), backup=backup)

    # ② 复验：当前画面里到底像不像
    score = None
    if gray is not None:
        pad = info["pad"]
        box = (info["region_px"][0] - int(round(info["region_px"][2] * pad)),
               info["region_px"][1] - int(round(info["region_px"][3] * pad)),
               sw, sh)
        crop = _crop(gray, box)
        if crop is not None and crop.size:
            score, loc, used = vision.match_scaled(crop, tpl, deep=True)
            out["score"] = round(float(score), 4)
            out["scale_used"] = float(used)
            # 纯色/无结构画面里 match_scaled 会返回 loc=None —— 别在这里炸，
            # loc 缺失就当作「没找到」，后续重采的位置闸会自然地拒掉。
            out["loc"] = ([int(loc[0]), int(loc[1])]
                          if isinstance(loc, (list, tuple)) and len(loc) >= 2
                          else None)
        if score is not None and score >= threshold:
            out.update(state="ok" if not backup else "rescaled", actions=actions,
                       backup=backup,
                       message=f"与当前画面匹配 {score:.2f}")
            return out

    # ③ 兜底重采：像但不够像（界面改版），且确实站在详情页
    if score is not None and score >= RESCUE_MIN:
        # 位置闸：真按钮得落在框选区域中心附近。分数够但位置飘到窗口边上，
        # 多半是「像但不是它」（旁边的渐变/图标），拿它重采会把背景写进模板，
        # 那是最坏的静默失败 —— 宁可不采。
        used = float(out.get("scale_used") or 1.0)
        if not _near_center(out.get("loc"), info["region_px"], info["pad"],
                            (tw * used, th * used)):
            out.update(state="fit",
                       actions=actions + ["重采被拒：最佳匹配位置偏离框选区域"],
                       backup=backup,
                       message=f"与当前画面 {score:.2f}，但匹配位置偏离区域中心，未重采")
            return out
        if page != "detail":
            out.update(state="fit", actions=actions + ["复验分数偏低，但当前页面不是详情页，未重采"],
                       backup=backup, message=f"与当前画面只有 {score:.2f}；停在详情页可自动重采")
            return out
        if not allow_write:
            out.update(state="fit", actions=actions + ["需要重采，但当前禁止写盘"],
                       backup=backup, message=f"与当前画面只有 {score:.2f}，需要重采")
            return out
        patch = _crop(frame if frame is not None else gray, info["region_px"])
        good, why = _texture_ok(vision.to_gray(patch))
        if not good:
            out.update(state="fit", actions=actions + [f"重采被拒：{why}"],
                       backup=backup, message=f"与当前画面只有 {score:.2f}，{why}")
            return out
        if patch.ndim == 2:
            patch = cv2.cvtColor(patch, cv2.COLOR_GRAY2BGR)
        ok, backup, why = _persist(name, patch, env=info["env"], action="recaptured",
                                   reason="界面与模板差异过大，按当前画面重采",
                                   extra={"from_score": round(float(score), 3)})
        if not ok:
            out.update(state="failed", actions=actions + ["重采写盘失败"], message=why)
            return out
        actions.append(f"按当前画面重采（旧分数 {score:.2f}）")
        # 自校验：新模板在同一个搜索窗里必须稳定命中
        crop = _crop(gray, (info["region_px"][0] - int(round(info["region_px"][2] * info["pad"])),
                            info["region_px"][1] - int(round(info["region_px"][3] * info["pad"])),
                            sw, sh))
        new_gray = vision.to_gray(patch)
        s2 = vision.match_scaled(crop, new_gray, deep=False)[0] if crop is not None else 0.0
        out.update(size=(int(patch.shape[1]), int(patch.shape[0])), backup=backup,
                   state="recaptured" if s2 >= SELF_MIN else "failed",
                   actions=actions, self_score=round(float(s2), 4),
                   message=(f"已按当前画面重采（自校验 {s2:.2f}）" if s2 >= SELF_MIN
                            else f"重采后自校验只有 {s2:.2f}，已保留但不可信"))
        return out

    if backup:
        out.update(state="rescaled", actions=actions, backup=backup,
                   message="已等比补偿模板尺寸并写回（旧文件已备份）")
        return out
    out.update(state="fit", actions=actions, backup=backup,
               message="当前画面里找不到这个按钮（几何已对齐，未复验）"
                       if score is not None else "未抓到画面，只做了几何对齐")
    return out


# ---------------------------------------------------------------------- 入口


def heal(cfg, frame=None, page=None, workflow=None, screen=None,
         allow_write=None, reason="preflight"):
    """跑一遍自愈。返回可直接塞进自检 facts 的结果字典。

    幂等、可随时调用（启动前检查 / 一键自动标定 / 命令行 `--auto` 都走这里）。
    全程只读屏幕；只有确实需要时才写 `templates/`，且写前必快照。
    """
    from . import regions

    own = screen is None
    result = {"status": "skipped", "ok": True, "message": "", "actions": [],
              "templates": {}, "passed": [], "writes": []}
    try:
        if screen is None:
            screen = regions.open_screen(cfg)
        env = env_facts(cfg, screen)
        result["env"] = env
        wf = workflow or _workflow_of(cfg)
        if allow_write is None:
            allow_write = writes_allowed()
        info = plan(cfg, screen, wf)
        names = [n for n in required(wf) if n in info]
        if not names:
            result.update(status="skipped",
                          message="没有需要自愈的模板（文件或区域缺失）")
            return result
        if frame is None:
            try:
                frame = screen.grab()
            except Exception:               # noqa: BLE001
                frame = None
        gray = vision.to_gray(frame) if frame is not None else None
        try:
            threshold = float(cfg.get("threshold") or 0.85)
        except (TypeError, ValueError):
            threshold = 0.85

        for name in names:
            info[name]["env"] = env
            one = _one(name, info[name], gray, frame, page,
                       threshold, allow_write)
            result["templates"][name] = one
            if one.get("backup"):
                result["writes"].append(name)
                result["backup"] = one["backup"]
            if one["state"] in ("ok", "fit", "rescaled", "recaptured"):
                result["passed"].append(name)
            result["actions"].extend(one.get("actions") or [])

        states = [t["state"] for t in result["templates"].values()]
        bad = [t for t in result["templates"].values() if t["state"] == "failed"]
        if bad:
            result["status"] = "failed"
            result["ok"] = False
            result["message"] = "；".join(
                f"{t['name']}：{t.get('message') or '不可用'}" for t in bad)
        elif "recaptured" in states:
            result["status"] = "recaptured"
            result["message"] = (f"已按当前 {env['scale_percent']}% 缩放"
                                 "自动重采模板")
        elif "rescaled" in states:
            result["status"] = "rescaled"
            result["message"] = (f"已按当前 {env['scale_percent']}% 缩放"
                                 "自动补偿模板尺寸")
        elif all(s == "ok" for s in states):
            result["status"] = "ok"
            result["message"] = "模板与当前画面一致"
        else:
            result["status"] = "fit"
            result["message"] = (f"已按当前 {env['scale_percent']}% 缩放"
                                 "自动适配（几何一致，未在当前画面复验）")
        return result
    except Exception as e:                  # noqa: BLE001
        result.update(status="failed", ok=False,
                      message=f"模板自愈没跑起来：{type(e).__name__}: {e}")
        return result
    finally:
        if own and screen is not None:
            try:
                screen.close()
            except Exception:               # noqa: BLE001
                pass


def diagnose(cfg, screen=None, workflow=None):
    """只读诊断：当前环境 vs 模板，一份可直接展示的证据表。"""
    from . import regions

    own = screen is None
    try:
        if screen is None:
            screen = regions.open_screen(cfg)
        env = env_facts(cfg, screen)
        info = plan(cfg, screen, workflow)
        return {"env": env,
                "templates": {k: {kk: vv for kk, vv in v.items()
                                  if kk != "env"} for k, v in info.items()}}
    finally:
        if own and screen is not None:
            try:
                screen.close()
            except Exception:               # noqa: BLE001
                pass


def restore(name=None):
    """把最近一次快照回滚回 `templates/`。返回实际恢复的文件名列表。"""
    from . import config as _cfgmod

    snap = template_backup.latest()
    if not snap:
        return []
    names = [name + ".png", "meta.json"] if name else None
    return template_backup.restore(snap, _cfgmod.TEMPLATE_DIR, names)
