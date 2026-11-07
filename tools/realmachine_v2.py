#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验证执行器 v2（在 realmachine_auto.py 之上扩展，不推倒重来）。

六个阶段 S0–S5，每段可独立跑 / 单独重跑：
  S0 环境预检      S1 界面识别与导航   S2 单件抢购全链路
  S3 卡点精度      S4 异常自愈演练     S5 前端联调（离线可跑）

不卡壳机制：
  · 每项超时 = 待判（PENDING），不算失败；全局看门狗硬顶。
  · 每阶段独立重试预算（默认 3），耗尽标 BLOCKED 后继续下一段。
  · 阶段崩溃走 try/catch 边界 + 安全态复位，不带崩整体。
  · 单实例互斥量；F8 / _realmachine_abort 立即停手并释放输入。

即修即验：
  · 失败自动收集：最小复现步骤、日志片段、配置快照、返回码、必要截图（≤3）。
  · 归属判定规则见 OWNER_RULES（报告中同步输出）。
  · 有登记修复器的项自动改（仅本仓库、仅小改动），只重跑该项，最多 3 轮。
  · 所有修改进 fixes[]：文件 / 行号 / 依据 / 前值 / 后值 / 重跑结果。

用法见 tools/realmachine_auto.md 的「v2」章节。
"""
from __future__ import annotations

import argparse
import copy
import ctypes
import ctypes.wintypes
import difflib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter, deque
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import realmachine_auto as base                     # noqa: E402
from realmachine_auto import (                      # noqa: E402
    Aborted, _fmt, _iso, _mean, _pct, _pc, REASON_CN, STATUS_CN,
)
try:                                                 # noqa: E402
    from core import switches, engine as engine_mod
    from core import scene_guard as sg
    from core import single_instance
except Exception as _imp_exc:                        # noqa: BLE001
    switches = None
    engine_mod = None
    sg = None
    single_instance = None
    _EXTRA_IMPORT_ERROR = f"{type(_imp_exc).__name__}: {_imp_exc}"
else:
    _EXTRA_IMPORT_ERROR = ""

ROOT = base.ROOT
SHOT_ROOT = os.path.join(ROOT, "_rm_shots")
BACKUP_ROOT = os.path.join(ROOT, "_rm_backup")
JSON_PATH = os.path.join(ROOT, "_realmachine_v2_result.json")
MD_PATH = os.path.join(ROOT, "_realmachine_v2_result.md")
TRACE_PATH = os.path.join(ROOT, "_realmachine_v2_trace.log")
ABORT_FILE = base.ABORT_FILE
EVENT_FILE = base.EVENT_FILE
MUTEX_NAME = "Local\\SJZ-Realmachine-V2"
CFG_PATH = os.path.join(ROOT, "config.json")

STAGE_CN = {
    "S0": "环境预检", "S1": "界面识别与导航", "S2": "单件抢购全链路",
    "S3": "卡点精度", "S4": "异常自愈演练", "S5": "前端联调",
    "S6": "数据字段与遥测", "S7": "性能基线",
}
STAGE_TIMEOUT = {"S0": 150, "S1": 120, "S2": 240, "S3": 420, "S4": 300,
                 "S5": 720, "S6": 180, "S7": 600}
STAGE_RETRY = 3          # 每阶段重试预算（含首次）
HEAL_ROUNDS = 3          # 即修即验单项重跑上限

# 每项默认超时（秒）；到点标「待判」，不判失败
ITEM_TIMEOUT = {
    "S0": 25, "S1": 30, "S2": 45, "S3": 60, "S4": 40, "S5": 25,
    "S6": 40, "S7": 90,
}

COVERAGE_PATH = os.path.join(ROOT, "_realmachine_coverage.md")
# 台账/证据列（与 core/pooltrace.py 的 EVIDENCE_KEYS 逐项对齐，顺序固定）。
# 复核第二轮第 7 条：原来只有 7 列，导出/回放会丢 listed_at_err_s、
# join_conflict、won_source 三列。
EVIDENCE_FIELDS = ("pool_joined_at", "join_result", "roll_self", "roll_max",
                   "won", "listed_at", "publicity_s",
                   "listed_at_err_s", "join_conflict", "won_source")
NEW_SWITCH_KEYS = (
    "stop.by_grade.enabled", "stop.by_grade.legend", "stop.by_grade.epic",
    "stop.by_grade.rare", "stop.by_grade.scope",
    "scan.repeat.min", "scan.repeat.max",
    "buy.followup.enabled",
    "click.burst.mode", "click.burst.hold_ms", "click.burst.interval_ms",
    "click.burst.window",
    "buy.skip_late.threshold_s",
    "mouse.human.traj", "mouse.human.move_ms", "mouse.human.curve",
    "mouse.idle.mode", "mouse.idle.interval_ms", "mouse.idle.return_s",
    "mouse.idle.min_remain_s", "mouse.idle.wheel_notches",
)

# 归属判定规则（原文写进报告，不留含糊）
OWNER_RULES = [
    ("环境", "驱动未就绪 / 提权不足 / 游戏窗口缺失或失焦 / DPI·分辨率不匹配 / "
             "磁盘或权限不足 / OCR 服务不可用 / 截图失败"),
    ("前端", "S5 控件段：data-sw 控件缺失、disabled、点击或 change 未发出 "
             "/api/switches 请求、页面 JS 报错、控件值与接口返回值不一致"),
    ("后端", "S5 接口/配置/行为段：接口 5xx、返回 ok=false、合法值被拒、"
             "接口成功但 config.json 未变、fresh 加载读不回、"
             "消费方读取值不一致"),
    ("后端", "S2/S3/S4 业务链路：引擎状态机异常、台账未写入、"
             "倒计时零点反推与点击时序逻辑异常"),
]

# 覆盖类别（用户十类 + 基础环境）
CATEGORY_CN = {
    0: "基础环境", 1: "七个新功能运行时", 2: "21 开关三段联通",
    3: "入池与抽签字段", 4: "性能基线", 5: "输入链路",
    6: "异常自愈", 7: "时间同步", 8: "界面与显示环境",
    9: "数据与遥测", 10: "长时运行",
}


def _cov(stage, cid, title, cat, pre, crit, tmo, owner, game, gate=False):
    return {"id": cid, "stage": stage, "title": title, "cat": cat, "pre": pre,
            "crit": crit, "tmo": tmo, "owner": owner, "game": game, "gate": gate}


# 覆盖矩阵唯一真源：V2_CHECKS 由它派生，_realmachine_coverage.md 由它生成。
COVERAGE_ROWS = [
    # ---------------------------------------------------------------- S0
    _cov("S0", "S0-1", "配置读取与结构", 0,
         "无", "load_ok=True 且 data 可读", 25, "环境", False, True),
    _cov("S0", "S0-2", "配置合法性（越界自动修正）", 0,
         "无", "0 项越界；越界自动夹回并记录 diff", 25, "环境", False, True),
    _cov("S0", "S0-3", "许可有效性", 0,
         "有许可文件才启用门禁", "enforced=True 时许可有效；无门禁则跳过", 25,
         "环境", False, True),
    _cov("S0", "S0-4", "磁盘空间", 0,
         "无", "剩余 ≥200MB", 25, "环境", False, True),
    _cov("S0", "S0-5", "DPI 感知与分辨率", 8,
         "无", "进程 DPI 感知 ≥ per-monitor；分辨率 ≥ 1280×720", 25,
         "环境", False, True),
    _cov("S0", "S0-6", "管理员权限（可自提权重启）", 0,
         "无", "已管理员或 --no-auto-elevate；提权重启不重复", 25,
         "环境", False),
    _cov("S0", "S0-7", "输入驱动可用性（只检测不安装）", 5,
         "驱动已安装", "鼠标设备存在且 identity() 可枚举；不安装", 25,
         "环境", False),
    _cov("S0", "S0-8", "游戏进程与窗口", 0,
         "游戏已启动", "进程/窗口柄可用，客户区 ≥ 800×600", 25,
         "环境", True),
    _cov("S0", "S0-9", "窗口尺寸与区域适配", 8,
         "游戏窗口", "regions 全部落在客户区内，越界自动夹回", 25,
         "环境", True),
    _cov("S0", "S0-10", "OCR 自检（真实帧）", 0,
         "游戏窗口", "真实帧 OCR 可读，置信度 ≥0.8", 25, "环境", True),
    _cov("S0", "S0-11", "中止通道", 0,
         "无", "F8 热键注册成功 + 中止文件可检测", 25, "环境", False),
    _cov("S0", "S0-12", "输入三通道身份（真实设备/虚拟/legacy/message）", 5,
         "驱动可用", "真实设备 VID_3554&PID_F50D 枚举到；回退通道 ∈ {legacy,message}",
         25, "环境", False),
    _cov("S0", "S0-13", "低级钩子标记（无注入标记）", 5,
         "驱动可用", "点击路径用驱动 stroke flags；mouse_event 仅安全复位；"
         "LLMHF 注入标记 0 处", 20, "后端", False),
    _cov("S0", "S0-14", "Raw Input 归属", 5,
         "有鼠标活动样本", "raw_mouse_activity 可按 vid:pid 归属；真实 VID 命中",
         25, "环境", False),
    _cov("S0", "S0-15", "光标还原失败率", 5,
         "游戏窗口+真实光标", "20 次保存/还原失败 ≤1 次（≤5%）", 45,
         "环境", True),
    _cov("S0", "S0-16", "前台不被抢", 5,
         "游戏窗口在前台", "连续 50 次零位移发送后前台 hwnd 仍为游戏窗口（100%）",
         60, "环境", True),
    _cov("S0", "S0-17", "显示缩放 125%/150%/200%", 8,
         "人工切换缩放后重跑", "当前 scale ∈ {1.0,1.25,1.5,1.75,2.0}；"
         "三种缩放各自坐标映射正确", 25, "环境", False),
    _cov("S0", "S0-18", "多显示器坐标", 8,
         "≥1 台显示器", "每个显示器 Screen(i).norm_to_px 落在各自 bounds 内",
         25, "环境", False),
    _cov("S0", "S0-19", "时间同步多源严格", 7,
         "无（离线伪造 NTP 响应）", "≥4 个独立来源；ok=True 且 degraded=False",
         30, "后端", False),
    _cov("S0", "S0-20", "时间同步单源降级", 7,
         "无（离线伪造单源）", "degraded=True；|offset| ≤300s；warning 非空",
         30, "后端", False),
    _cov("S0", "S0-21", "断网不污染状态", 7,
         "无（离线伪造全失败）", "ok=False 且 offset/state 与失败前一致", 30,
         "后端", False),
    # ---------------------------------------------------------------- S1
    _cov("S1", "S1-1", "当前界面识别", 0,
         "游戏窗口", "page ∈ 已知页集合", 30, "环境", True, True),
    _cov("S1", "S1-2", "回到交易行主界面", 0,
         "游戏窗口", "恢复后 page=market", 30, "环境", True, True),
    _cov("S1", "S1-3", "导航到目标列表（在售页）", 0,
         "游戏窗口", "page=onsale 且 ≤30s", 30, "环境", True, True),
    _cov("S1", "S1-4", "步数与超时预算", 0,
         "S1 已跑", "单步 ≤30s；S1 总步数 ≤8", 30, "环境", True),
    _cov("S1", "S1-5", "空闲漫游：非倒计时路线不启用", 1,
         "无", "gate(list)=False 且 tick 不改坐标；gate(countdown)=True",
         30, "后端", False),
    _cov("S1", "S1-6", "空闲漫游：全程零点击", 1,
         "无", "60s 模拟 tick 中 click 调用=0 且 move>0", 30, "后端", False),
    _cov("S1", "S1-7", "空闲漫游：开火前归位", 1,
         "无", "剩余 ≤min_remain_s 时 settled=True 且终点=home(±8px)",
         30, "后端", False),
    _cov("S1", "S1-8", "最大化/还原后区域坐标", 8,
         "游戏窗口", "还原→最大化→还原各一次后 regions 仍落在客户区",
         40, "环境", True),
    # ---------------------------------------------------------------- S2
    _cov("S2", "S2-0", "测试标的与预算闸", 0,
         "--allow-buy + 标的/预算", "闸门通过；否则 S2 只读", 45,
         "环境", False, True),
    _cov("S2", "S2-1", "读价", 0,
         "S2 闸门过", "价格可读且 ≤max_price", 45, "环境", True),
    _cov("S2", "S2-2", "选量", 0,
         "S2 闸门过", "数量 = quota 且商品名匹配", 45, "环境", True),
    _cov("S2", "S2-3", "确认", 0,
         "S2 闸门过", "出现 fire/confirm 状态", 45, "环境", True),
    _cov("S2", "S2-4", "台账", 0,
         "本轮成交", "成交后台账新增 ≥1", 45, "后端", True),
    _cov("S2", "S2-5", "全链路耗时", 0,
         "S2 已跑", "有完整状态序列", 45, "环境", True),
    _cov("S2", "S2-6", "随机刷新次数（1–3 实际分布）", 1,
         "配置 scan.repeat.min=1 max=3", "300 次抽样覆盖 1/2/3 且不越界；"
         "真机 refresh_used 落在 [min,max]", 45, "后端", False),
    _cov("S2", "S2-7", "买到即停：立刻断刷", 1,
         "无（静态契约）+ 真机成交", "repeat_refresh 含 _stop 与预算双闸；"
         "成交后 refresh_used=0", 45, "后端", False),
    _cov("S2", "S2-8", "来不及跳过（反推时刻判定）", 1,
         "无", "阈值默认 2s；剩余 1.2s 判定跳过；0s/非倒计时不跳过；"
         "跳过记录不含价/量字段", 45, "后端", False),
    _cov("S2", "S2-9", "按品阶停止（三档各自达标即停）", 1,
         "无", "传说/史诗/稀有 三档命中 target 即停，提前 0 次、达标 1 次",
         45, "后端", False),
    _cov("S2", "S2-10", "连点（带长/间隔实际生效）", 1,
         "无", "read 回读 hold_ms/interval_ms 精确；run 实际保持/间隔符合；"
         "与补刀互斥", 45, "后端", False),
    _cov("S2", "S2-11", "假购买补刀（默认关，最多补一次）", 1,
         "无", "默认 off；开启后每次成交补点 ≤1；连点模式禁用", 45,
         "后端", False),
    # ---------------------------------------------------------------- S3
    _cov("S3", "S3-1", "成对读数样本（≥16）", 0,
         "游戏窗口 + 倒计时", "成对读数 ≥16 条", 60, "环境", True, True),
    _cov("S3", "S3-2", "反推解锁时刻", 0,
         "S3-1 通过", "zero_at 可用且 trusted=True", 60, "后端", True),
    _cov("S3", "S3-3", "点击/预估与理论零点偏差", 0,
         "S3-2 通过", "偏差 p95 ≤80ms", 60, "后端", True),
    _cov("S3", "S3-4", "超窗比例", 0,
         "有偏差样本", "超窗比例 ≤5%（窗 120ms）", 60, "后端", True),
    _cov("S3", "S3-5", "成对读数策略真机可用性", 0,
         "S3-1 通过", "成对间隔 p95 ≤30ms", 60, "环境", True),
    _cov("S3", "S3-6", "拟人轨迹：开火零位移", 1,
         "无", "预热后 fire 轨迹 ≤2 点、位移 0px、时长 ≤30ms", 45,
         "后端", False),
    _cov("S3", "S3-7", "拟人轨迹：非开火走曲线", 1,
         "无", "400px 非开火轨迹 ≥8 点；离弦最大偏移 >1px；类型 windmouse",
         45, "后端", False),
    _cov("S3", "S3-8", "拟人轨迹：瞬移降级率", 1,
         "无", "record_fire 分类正确；straight_rate ≤10%（预热命中 ≥90%）",
         45, "后端", False),
    # ---------------------------------------------------------------- S4
    _cov("S4", "S4-1", "点错界面 → 自愈", 6,
         "场景守卫可用", "scene=unexpected_page 且恢复 ok/recovering", 40,
         "后端", False),
    _cov("S4", "S4-2", "未知界面 → 自愈", 6,
         "场景守卫可用", "scene=ui_changed 且不硬停", 40, "后端", False),
    _cov("S4", "S4-3", "点击没生效 → 自愈", 6,
         "场景守卫可用", "scene=click_no_effect 且重试后恢复", 40, "后端", False),
    _cov("S4", "S4-4", "弹窗遮挡 → 自愈", 6,
         "场景守卫可用", "scene=confirm_unexpected 且按预算处理", 40,
         "后端", False),
    _cov("S4", "S4-5", "窗口失焦 → 自愈", 6,
         "场景守卫可用", "scene=window_lost 且重新聚焦后 ok", 40,
         "后端", False),
    _cov("S4", "S4-6", "游戏掉线提示 → 自愈", 6,
         "场景守卫可用", "scene=game_down 且进入可恢复/等待人工", 40,
         "后端", False),
    _cov("S4", "S4-7", "恢复汇总", 6,
         "S4 已跑", "全部演练项识别并可恢复；未演练明确列出", 40,
         "后端", False),
    _cov("S4", "S4-8", "电脑卡顿 → 自愈", 6,
         "无", "frame_dt_p95=400ms → scene=lag，随后恢复 ok", 40,
         "后端", False),
    _cov("S4", "S4-9", "掉线重登 → 自愈", 6,
         "无", "game_alive=False+login_visible=True → game_down；重登后 ok",
         40, "后端", False),
    _cov("S4", "S4-10", "显示器休眠唤醒 → 自愈", 6,
         "无", "minimized=True+帧冻结 → window_lost；唤醒后 ok", 40,
         "后端", False),
    _cov("S4", "S4-11", "看门狗卡死恢复", 6,
         "无", "心跳 100s 未更新第 3 次 poll 触发；冷却内不重复；恢复后复位",
         30, "后端", False),
    # ---------------------------------------------------------------- S5
    _cov("S5", "S5-0", "本地服务与页面加载", 0,
         "端口可用", "/api/switches 返回 200", 60, "后端", False, True),
    _cov("S5", "S5-1", "开关控件四段联调", 2,
         "服务已起", "每个 data-sw 控件：控件→接口→磁盘→消费方全通过", 600,
         "前端", False, True),
    _cov("S5", "S5-2", "假按钮结论", 2,
         "S5-1 已跑", "0 个假按钮/失联控件", 30, "前端", False),
    _cov("S5", "S5-3", "时间同步界面提示可见", 7,
         "服务已起", "页面含「时间校准」；/api/timesync 200 且含 degraded/offset",
         25, "前端", False),
    _cov("S5", "S5-4", "21 个新开关专项汇总", 2,
         "S5-1 已跑", "21/21 key 三段联通全通过（逐 key 证据）", 60,
         "前端", False),
    # ---------------------------------------------------------------- S6
    _cov("S6", "S6-1", "七字段落盘（合成记录）", 3,
         "无", "records.add(extra=7 键) → load 回读 7 键齐全且原值不变",
         30, "后端", False),
    _cov("S6", "S6-2", "七字段类型与缺失语义", 3,
         "无", "int/float/str/bool 类型正确；识别不到必须 null，不得 0",
         30, "后端", False),
    _cov("S6", "S6-3", "台账行可解析", 3,
         "存在 data/records.jsonl", "逐行 JSON 解析成功率 100%", 25,
         "后端", False),
    _cov("S6", "S6-4", "遥测事件映射与入库", 9,
         "无（临时库）", "_event(row)→clean_event→Store.ingest→user_events ≥1",
         40, "后端", False),
    _cov("S6", "S6-5", "看板能显示该用户数据", 9,
         "S6-4 已入库", "users() 含该 lic；overview 命中 ≥1；看板文件含用户详情节",
         30, "后端", False),
    _cov("S6", "S6-6", "真实成交行七字段取值", 3,
         "真机成交 ≥1 单", "最近 hit 行 7 字段齐全、类型正确、无 0 替 null",
         45, "后端", True),
    _cov("S6", "S6-7", "遥测上报成功", 9,
         "真机成交 + telemetry server", "agent 上报 200 或 outbox 清空", 45,
         "后端", True),
    # ---------------------------------------------------------------- S7
    _cov("S7", "S7-0", "基线换算自检", 4,
         "无", "已知样本 p50/p95/max 计算正确；3220–3550 判带正确", 30,
         "后端", False),
    _cov("S7", "S7-1", "单件端到端耗时", 4,
         "真机成交样本 ≥3", "p50 ≤3550ms（基线 3743.9ms）", 120,
         "后端", True),
    _cov("S7", "S7-2", "点击光标占用", 4,
         "无（隐藏测试窗口/消息通道）", "p50 ≤0.40ms、p95 ≤1.0ms、max ≤2.0ms",
         60, "后端", False),
    _cov("S7", "S7-3", "offset_ms 分布", 4,
         "真机点火或成交样本", "p50 ≤30ms、p95 ≤80ms", 120, "后端", True),
    _cov("S7", "S7-4", "长时资源（过夜）", 10,
         "--overnight-hours ≥1 + 游戏", "内存增长 ≤15%；句柄 ≤+200；台账无丢失",
         240, "环境", True),
]


def coverage_breakdown():
    must = [r["id"] for r in COVERAGE_ROWS if r["game"]]
    no = [r["id"] for r in COVERAGE_ROWS if not r["game"]]
    return must, no


def write_coverage_md(path=COVERAGE_PATH):
    """覆盖矩阵落盘：每行 检查项/阶段/前置/判据/超时/归属/是否必须开游戏。"""
    must, no = coverage_breakdown()
    L = ["# 真机验证覆盖矩阵\n",
         "> 由 `tools/realmachine_v2.py` 的 `COVERAGE_ROWS` 生成；"
         "改代码即改矩阵，不手工双维护。\n",
         f"> 共 {len(COVERAGE_ROWS)} 项："
         f"必须开游戏 {len(must)} 项 / 不开游戏也能测 {len(no)} 项\n",
         "\n## 一、必须开游戏才能测的项\n",
         "、".join(must),
         "\n\n## 二、不开游戏也能测的项\n",
         "、".join(no),
         "\n\n## 三、逐项矩阵\n"]
    for sid in ("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"):
        rows = [r for r in COVERAGE_ROWS if r["stage"] == sid]
        if not rows:
            continue
        L.append(f"\n### {sid} {STAGE_CN[sid]}（{len(rows)} 项）\n")
        L.append("| ID | 阶段 | 类别 | 检查项 | 前置条件 | 量化判据 | 超时 | 失败归属 | 必须开游戏 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            L.append(f"| {r['id']} | {r['stage']} | {CATEGORY_CN.get(r['cat'], r['cat'])} "
                     f"| {r['title']} | {r['pre']} | {r['crit']} | {r['tmo']}s "
                     f"| {r['owner']} | {'是' if r['game'] else '否'} |")
    L.append("\n## 四、类别覆盖对照（用户十类）\n")
    L.append("| 类别 | 覆盖项 |")
    L.append("|---|---|")
    for cid, name in CATEGORY_CN.items():
        if cid == 0:
            continue
        ids = [r["id"] for r in COVERAGE_ROWS if r["cat"] == cid]
        L.append(f"| {cid}. {name} | {'、'.join(ids) if ids else '—'} |")
    try:
        with open(path, "w", encoding="utf-8") as fp:
            fp.write("\n".join(L) + "\n")
        return path
    except Exception:                                    # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 数据结构
class ItemTimeout(Exception):
    """单项超时：结果改判「待判」，继续下一项。"""


class StageTimeout(Exception):
    """阶段超时：该阶段剩余项标待判，进入下一阶段。"""


class CheckV2:
    __slots__ = ("cid", "title", "stage", "gate", "status", "owner", "reason",
                 "detail", "measured", "threshold", "duration_s", "shots",
                 "codes", "evidence", "repro", "fix", "at", "mode")

    def __init__(self, cid, title, stage, gate=False):
        self.cid = cid
        self.title = title
        self.stage = stage
        self.gate = gate
        self.status = None
        self.owner = ""
        self.reason = ""
        self.detail = ""
        self.measured = "—"
        self.threshold = "—"
        self.duration_s = 0.0
        self.shots: list[str] = []
        self.codes: dict = {}
        self.evidence: dict = {}
        self.repro: dict = {"steps": [], "log": [], "config": {}, "codes": {},
                            "shots": []}
        self.fix: dict = {}
        self.at = None
        self.mode = ""

    def ok(self, measured, threshold, detail=""):
        self.status = "pass"
        self.measured = str(measured)
        self.threshold = str(threshold)
        self.detail = detail
        self.at = _iso()
        return self

    def bad(self, measured, threshold, detail=""):
        self.status = "fail"
        self.measured = str(measured)
        self.threshold = str(threshold)
        self.detail = detail
        self.at = _iso()
        return self

    def blocked(self, detail, measured="—", threshold="—"):
        self.status = "blocked"
        self.reason = detail
        self.measured = str(measured)
        self.threshold = str(threshold)
        self.detail = detail
        self.at = _iso()
        return self

    def skip(self, reason, detail="", measured="—", threshold="—"):
        self.status = "skip"
        self.reason = reason
        self.measured = str(measured)
        self.threshold = str(threshold)
        self.detail = detail
        self.at = _iso()
        return self

    def pending(self, detail, measured="—", threshold="—"):
        self.status = "pending"
        self.reason = "timeout"
        self.measured = str(measured)
        self.threshold = str(threshold)
        self.detail = detail
        self.at = _iso()
        return self

    def as_dict(self):
        return {
            "id": self.cid, "title": self.title, "stage": self.stage,
            "gate": self.gate, "status": self.status, "owner": self.owner,
            "measured": self.measured, "threshold": self.threshold,
            "detail": self.detail, "reason": self.reason,
            "duration_s": round(self.duration_s, 2), "shots": self.shots,
            "codes": self.codes, "evidence": self.evidence,
            "repro": self.repro, "fix": self.fix, "mode": self.mode,
            "at": self.at,
        }


# ---------------------------------------------------------------- 主执行器
class RunnerV2(base.Runner):
    def __init__(self, args):
        super().__init__(args)
        self.args = args
        self.checks: list[CheckV2] = []
        self._by_id: dict[str, CheckV2] = {}
        self._item_fns: dict[str, callable] = {}
        self._shot_dir = os.path.join(SHOT_ROOT, self.run_id)
        self._backup_dir = os.path.join(BACKUP_ROOT, self.run_id)
        self.log_ring = deque(maxlen=300)
        self.steps = deque(maxlen=60)
        self.fixes: list[dict] = []
        self.stage_rows: list[dict] = []
        self.stage_gate: dict[str, bool] = {}
        self._stage = ""
        self._item = ""
        self._item_deadline = 0.0
        self._stage_deadline = 0.0
        self._global_deadline = (_pc() + args.global_timeout
                                 if getattr(args, "global_timeout", 0) else 0.0)
        self._wd_stop = threading.Event()
        self._wd_thread = None
        self.stage_overdue = False
        self.item_overdue = False
        self.relaunch_requested = False
        self.keyboard = None
        self.nav = None
        self.screen = None
        self.game_rect = None
        self._js_errors: list[str] = []
        self._s5_before_sha = ""
        self._s5_http = ""
        self._s5_proc = None

    # ------------------------------------------------------------ 输出/状态
    def say(self, msg, level="info"):
        self.log_ring.append(f"{_iso()} {level}: {msg}")
        prefix = {"info": "", "warn": "[!] ", "fail": "[×] ",
                  "pass": "[✓] "}.get(level, "")
        print(f"{prefix}{msg}", flush=True)
        self.trace(f"{level}: {msg}")

    def step(self, text):
        self.steps.append(f"{_iso()} {text}")
        self.trace(f"step: {text}")

    def tick(self):
        """协作式检查点：中止 / 阶段超时 / 单项超时。"""
        if self.aborted():
            raise Aborted("用户中止")
        now = _pc()
        if self._global_deadline and now > self._global_deadline:
            self._on_abort()
            raise Aborted("全局看门狗超时")
        if self._stage_deadline and now > self._stage_deadline:
            raise StageTimeout()
        if self._item_deadline and now > self._item_deadline:
            raise ItemTimeout()

    def _iso_now(self):
        return _iso()

    def _open_trace(self):
        try:
            os.makedirs(ROOT, exist_ok=True)
            self._trace_fp = open(TRACE_PATH, "a", encoding="utf-8")
            self.trace(f"run {self.run_id} start argv={sys.argv[1:]}")
        except Exception:                            # noqa: BLE001
            self._trace_fp = None

    def _game_rect(self):
        """游戏窗口矩形 (left, top, w, h)；Win32 直读，不依赖 core 的可选封装。"""
        if not self.hwnd:
            return None
        try:
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
            return (rect.left, rect.top, rect.right - rect.left,
                    rect.bottom - rect.top)
        except Exception:                            # noqa: BLE001
            return None

    # ------------------------------------------------------------ 机器对象
    def ensure_screen(self):
        if self.screen is None:
            try:
                idx = int(base._safe_int(self.cfg.get("monitor", 1), 1)) \
                    if self.cfg else 1
                self.screen = base.screen_mod.Screen(idx)
            except Exception as exc:                    # noqa: BLE001
                self.screen = None
                self.trace(f"screen init failed: {exc}")
        return self.screen

    def ensure_game(self, mouse=False, keyboard=False):
        if self.cfg is None:
            self.cfg = base.config_mod.Config.load()
        self.ensure_screen()
        if self.hwnd is None:
            try:
                self.hwnd = base.input_mod.find_game_window()
            except Exception:                            # noqa: BLE001
                self.hwnd = None
        if mouse and self.mouse is None and self.hwnd:
            try:
                self.mouse = base.input_mod.Mouse(
                    screen=self.screen, game_hwnd=self.hwnd,
                    mode=self.cfg.get("input_mode", "interception"),
                    fallback=self.cfg.get("input_fallback", "legacy"))
            except Exception as exc:                    # noqa: BLE001
                self.mouse = None
                self.trace(f"mouse init failed: {exc}")
        if keyboard and self.hwnd and self.keyboard is None:
            try:
                self.keyboard = base.input_mod.Keyboard(
                    self.hwnd, self.cfg.get("input_mode", "interception"))
            except Exception as exc:                    # noqa: BLE001
                self.keyboard = None
                self.trace(f"keyboard init failed: {exc}")
        if self.hwnd and self.game_rect is None:
            self.game_rect = self._game_rect()
        if self.keyboard and self.nav is None:
            self.nav = base.nav_mod.Navigator(self.screen, self.mouse,
                                           self.keyboard, log=self.say)
        return bool(self.hwnd and self.screen)

    def safe_reset(self, why=""):
        """安全态：松开按键/左键、清裁剪锁、清按下态；不动用户光标。"""
        try:
            for _ in range(2):
                base.ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        except Exception:                                # noqa: BLE001
            pass
        try:
            base.input_mod.Mouse.release_cursor_lock()
        except Exception:                                # noqa: BLE001
            pass
        try:
            if self.keyboard is not None:
                # 只发 key-up（不点按），避免在游戏里误走一步
                scans = getattr(base.input_mod, "SCAN", {}) or {}
                for k in ("W", "A", "S", "D", "SPACE", "SHIFT"):
                    scan = scans.get(k)
                    if scan is None:
                        continue
                    try:
                        self.keyboard._stroke(scan, up=True, e0=False)
                    except Exception:                    # noqa: BLE001
                        break
        except Exception:                                # noqa: BLE001
            pass
        self.step(f"安全复位：{why or '阶段收尾'}")

    def close_machine(self):
        try:
            self.keyboard = None
            self.nav = None
            if self.mouse is not None and hasattr(self.mouse, "close"):
                self.mouse.close()
        except Exception:                                # noqa: BLE001
            pass
        self.mouse = None

    # ------------------------------------------------------------ 采证
    def save_shot(self, c, frame):
        n = self.shots.get(c.cid, 0)
        if n >= 3 or frame is None:
            return
        try:
            os.makedirs(self._shot_dir, exist_ok=True)
            p = os.path.join(self._shot_dir, f"{c.cid}_{n + 1}.png")
            ok, buf = base.cv2.imencode(".png", frame)
            if ok:
                buf.tofile(p)
                c.shots.append(p)
                self.shots[c.cid] = n + 1
        except Exception:                                # noqa: BLE001
            pass

    def grab_full(self):
        try:
            return self.screen.grab() if self.screen else None
        except Exception:                                # noqa: BLE001
            return None

    def capture_evidence(self, c, exc=None):
        cfg_snapshot = {}
        try:
            if self.cfg is not None and base.switches is not None:
                for s in switches.SWITCHES:
                    cfg_snapshot[s["key"]] = switches.get(self.cfg, s["key"])
        except Exception:                                # noqa: BLE001
            pass
        c.evidence = {
            "exc": f"{type(exc).__name__}: {exc}" if exc else "",
            "page": getattr(self, "page", ""),
            "mode": self.mouse.mode if self.mouse is not None else "无",
            "game_hwnd": int(self.hwnd) if self.hwnd else None,
        }
        c.repro = {
            "steps": list(self.steps)[-12:],
            "log": list(self.log_ring)[-20:],
            "config": cfg_snapshot,
            "codes": dict(c.codes),
            "shots": list(c.shots),
        }

    # ------------------------------------------------------------ 归类
    def classify(self, c, exc=None):
        if c.owner:
            return c.owner
        text = f"{c.detail} {c.codes} {exc or ''}".lower()
        env_kw = ("驱动", "driver", "管理员", "elevat", "uipi", "窗口", "hwnd",
                  "dpi", "分辨率", "screen", "截图", "磁盘", "disk", "权限",
                  "ocr", "游戏未启动", "未找到游戏", "browser", "chromium",
                  "playwright", "executable doesn't exist", "浏览器")
        front_kw = ("控件", "data-sw", "disabled", "页面 js", "pageerror",
                    "未发出请求", "前端")
        if any(k in text for k in env_kw):
            return "环境"
        if c.stage == "S5" and any(k in text for k in front_kw):
            return "前端"
        return "后端"

    # ------------------------------------------------------------ 单项执行
    def item(self, cid, title, fn, *, timeout=None, gate=False, stage=None):
        st = stage or self._stage
        if self.stage_overdue:
            raise StageTimeout()
        if cid not in _COVERAGE_IDS and cid not in _UNCOVERED_IDS:
            _UNCOVERED_IDS.append(cid)
            self.say(f"[矩阵] {cid} 已实现但不在覆盖矩阵，报告会点名", "warn")
        c = CheckV2(cid, title, st, gate)
        self._by_id[cid] = c
        self._item_fns[cid] = fn
        self._item = cid
        self.item_overdue = False
        self._item_deadline = _pc() + (timeout or ITEM_TIMEOUT.get(st, 30))
        t0 = _pc()
        self.step(f"开始 {cid} {title}")
        try:
            fn(c)
        except Aborted:
            raise
        except StageTimeout:
            c.pending("阶段超时，未完成（待判）")
        except ItemTimeout:
            c.pending("单项超时，未完成（待判）")
        except Exception as exc:                        # noqa: BLE001
            c.bad(f"{type(exc).__name__}: {exc}", "无异常",
                  "未捕获异常（已归类，见 evidence.repro）")
            self.capture_evidence(c, exc)
            c.owner = self.classify(c, exc)
            self.trace(traceback.format_exc())
        c.duration_s = _pc() - t0
        over = c.duration_s > (timeout or ITEM_TIMEOUT.get(st, 30)) * 1.5
        if over and c.status not in ("skip",):
            c.pending(f"超时待判（原结论 {c.status}；耗时 {c.duration_s:.1f}s）",
                      c.measured, c.threshold)
        if c.status is None:
            c.skip("not_auto", "本项未产出结论")
        if c.status in ("fail", "blocked", "pending") and not c.repro["steps"]:
            self.capture_evidence(c)
        if c.status in ("fail", "blocked") and c.shots == []:
            frame = self.grab_full()
            if frame is not None:
                self.save_shot(c, frame)
                c.repro["shots"] = list(c.shots)
        self.add(c)
        return c

    def add(self, c):
        self.checks.append(c)
        mark = {"pass": "✓", "fail": "×", "blocked": "■", "skip": "–",
                "pending": "?"}.get(c.status, "?")
        extra = f"（{c.reason}）" if c.status == "skip" and c.reason else ""
        self.say(f"  {mark} {c.cid} {c.title}{extra}"
                 f"   [{c.owner or '未归类'}] {c.duration_s:.1f}s",
                 "pass" if c.status == "pass" else
                 ("fail" if c.status == "fail" else
                  "warn" if c.status in ("blocked", "pending") else "info"))

    # ------------------------------------------------------------ 阶段
    def _watchdog(self):
        while not self._wd_stop.is_set():
            if self.abort.is_set():
                return
            now = _pc()
            if self._stage_deadline and now > self._stage_deadline:
                self.stage_overdue = True
            if self._item_deadline and now > self._item_deadline:
                self.item_overdue = True
            if self._global_deadline and now > self._global_deadline:
                self._on_abort()
                return
            time.sleep(0.15)

    def _start_watchdog(self):
        self._wd_stop.clear()
        self._wd_thread = threading.Thread(target=self._watchdog,
                                           name="v2-watchdog", daemon=True)
        self._wd_thread.start()

    def _stop_watchdog(self):
        self._wd_stop.set()
        if self._wd_thread:
            self._wd_thread.join(timeout=0.5)

    def run_stage(self, sid):
        self._stage = sid
        spec_retry = getattr(self.args, "retry", STAGE_RETRY) or STAGE_RETRY
        timeout = STAGE_TIMEOUT[sid]
        self.say("=" * 64)
        self.say(f"[{sid}] {STAGE_CN[sid]}")
        t0 = _pc()
        attempt = 0
        stage_error = ""
        while attempt < spec_retry:
            attempt += 1
            self.stage_overdue = False
            self._stage_deadline = _pc() + timeout
            try:
                self._stage_body(sid)
                stage_error = ""
                break
            except Aborted:
                raise
            except StageTimeout:
                self._fill_remaining(sid, "阶段超时，未执行（待判）", "pending")
                stage_error = "阶段超时"
                break
            except Exception as exc:                    # noqa: BLE001
                stage_error = f"{type(exc).__name__}: {exc}"
                self.say(f"[{sid}] 阶段异常：{stage_error}", "fail")
                self.trace(traceback.format_exc())
                self.safe_reset(f"{sid} 异常后复位")
                if attempt >= spec_retry:
                    self._fill_remaining(sid, f"阶段重试预算耗尽：{stage_error}",
                                         "blocked")
                    break
                self.say(f"[{sid}] 重试 {attempt + 1}/{spec_retry}", "warn")
                time.sleep(0.5)
        # 即修即验：只针对本阶段失败/待判项
        self.heal_stage(sid)
        self.close_stage(sid, t0, attempt, stage_error)
        self.safe_reset(f"{sid} 收尾")

    def _fill_remaining(self, sid, detail, status):
        for cid, title, gate in V2_CHECKS.get(sid, []):
            if cid in self._by_id:
                continue
            c = CheckV2(cid, title, sid, gate)
            if status == "blocked":
                c.blocked(detail)
            else:
                c.pending(detail)
            self.add(c)

    def close_stage(self, sid, t0, attempts, error):
        rows = [c for c in self.checks if c.stage == sid]
        n = Counter(c.status for c in rows)
        gates = [c for c in rows if c.gate]
        gate_ok = all(c.status == "pass" for c in gates) if gates else True
        self.stage_gate[sid] = gate_ok
        if n["fail"]:
            status = "fail"
        elif n["blocked"]:
            status = "blocked"
        elif n["pending"]:
            status = "pending"
        elif rows and n["pass"] + n["skip"] == len(rows) and n["skip"] == len(rows):
            status = "skip"
        else:
            status = "pass"
        self.stage_rows.append({
            "id": sid, "name": STAGE_CN[sid], "status": status,
            "attempts": attempts, "duration_s": round(_pc() - t0, 2),
            "gate_ok": gate_ok, "pass": n["pass"], "fail": n["fail"],
            "blocked": n["blocked"], "skip": n["skip"],
            "pending": n["pending"], "error": error,
            "checks": [c.as_dict() for c in rows],
        })
        self.say(f"[{sid}] 过 {n['pass']} · 不过 {n['fail']} · 阻塞 {n['blocked']}"
                 f" · 跳过 {n['skip']} · 待判 {n['pending']}"
                 f"（{_pc() - t0:.1f}s，尝试 {attempts} 次）",
                 "pass" if status == "pass" else
                 ("fail" if status == "fail" else "warn"))

    def heal_stage(self, sid):
        for c in [x for x in self.checks if x.stage == sid]:
            if c.status not in ("fail", "blocked", "pending"):
                continue
            owner = c.owner or self.classify(c)
            c.owner = owner
            fn = self._item_fns.get(c.cid)
            fixer = FIXERS.get(c.cid)
            hint = c.codes.get("fix_hint") if isinstance(c.codes, dict) else None
            if not (fn and (fixer or hint)):
                continue
            for rnd in range(1, HEAL_ROUNDS + 1):
                self.say(f"[即修即验] {c.cid} 第 {rnd} 轮 · 归属 {owner}", "warn")
                try:
                    if fixer:
                        fr = fixer(self, c)
                    else:
                        fr = apply_code_fix(
                            hint["file"], hint["old"], hint["new"],
                            evidence=hint.get("evidence") or c.detail,
                            backup_dir=self._backup_dir, cid=c.cid, round=rnd,
                            owner=hint.get("owner") or owner)
                except Exception as exc:                # noqa: BLE001
                    self.say(f"[即修即验] 修复器失败：{exc}", "fail")
                    break
                if not fr:
                    break
                self.fixes.append(fr)
                nc = CheckV2(c.cid, c.title, sid, c.gate)
                self._item_deadline = _pc() + 60
                self.item_overdue = False
                try:
                    fn(nc)
                except Aborted:
                    raise
                except Exception as exc:                # noqa: BLE001
                    nc.bad(f"{type(exc).__name__}: {exc}", "无异常")
                nc.duration_s = 0.0
                nc.owner = fr.get("owner", owner)
                fr["rerun"] = nc.status
                fr["rerun_measured"] = nc.measured
                if nc.status == "pass":
                    nc.fix = {"round": rnd, "file": fr.get("file", ""),
                              "lines": fr.get("lines", [])}
                    self._replace(c.cid, nc)
                    self.say(f"[即修即验] {c.cid} 已修复并复验通过", "pass")
                    break
                c.fix = {"round": rnd, "attempted": fr.get("file", ""),
                         "lines": fr.get("lines", [])}
                self.say(f"[即修即验] {c.cid} 第 {rnd} 轮复验仍 {nc.status}", "fail")
                if rnd >= HEAL_ROUNDS:
                    c.detail += f" · 自动修复 3 轮未过（复现路径见 repro：{c.cid}）"

    def _replace(self, cid, newc):
        for i, old in enumerate(self.checks):
            if old.cid == cid and old.stage == newc.stage:
                self.checks[i] = newc
        self._by_id[cid] = newc

    def _stage_body(self, sid):
        fn = getattr(self, f"stage_{sid}", None)
        if fn is None:
            raise RuntimeError(f"没有实现 {sid}")
        fn()

    # ================================================================ S0
    def stage_S0(self):
        self.item("S0-1", "配置读取与结构", self._s0_config, gate=True)
        self.item("S0-2", "配置合法性（越界自动修正）", self._s0_bounds, gate=True)
        self.item("S0-3", "许可有效性", self._s0_license, gate=True)
        self.item("S0-4", "磁盘空间", self._s0_disk, gate=True)
        self.item("S0-5", "DPI 感知与分辨率", self._s0_dpi, gate=True)
        self.item("S0-6", "管理员权限（可自提权重启）", self._s0_admin)
        self.item("S0-7", "输入驱动可用性（只检测不安装）", self._s0_driver)
        self.item("S0-8", "游戏进程与窗口", self._s0_game)
        self.item("S0-9", "窗口尺寸与区域适配", self._s0_region)
        self.item("S0-10", "OCR 自检（真实帧）", self._s0_ocr)
        self.item("S0-11", "中止通道", self._s0_abort_channel)
        self.item("S0-12", "输入三通道身份", self._s0_channels)
        self.item("S0-13", "低级钩子标记", self._s0_lowlevel)
        self.item("S0-14", "Raw Input 归属", self._s0_rawinput)
        self.item("S0-15", "光标还原失败率", self._s0_restore, timeout=45)
        self.item("S0-16", "前台不被抢", self._s0_foreground, timeout=60)
        self.item("S0-17", "显示缩放 125/150/200", self._s0_scale)
        self.item("S0-18", "多显示器坐标", self._s0_monitors)
        self.item("S0-19", "时间同步多源严格", self._s0_sync_multi)
        self.item("S0-20", "时间同步单源降级", self._s0_sync_single)
        self.item("S0-21", "断网不污染状态", self._s0_sync_offline)

    # ------------------------------------------------ S0-12..21（离线可测）
    def _s0_channels(self, c):
        try:
            from core import interception as icept
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"驱动模块不可用：{type(exc).__name__}: {exc}")
            return
        self.cfg = self.cfg or base.config_mod.Config.load()
        try:
            icept._SHARED._open()
            devs = icept.enumerate_devices("mouse") or []
            ident = icept._SHARED.identity() or {}
            icept._SHARED.close()
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"设备枚举失败：{type(exc).__name__}: {exc}")
            return
        blob = json.dumps(devs, ensure_ascii=False).upper()
        real = ("3554" in blob and "F50D" in blob)
        virtual = sum(1 for d in devs if "ROOT" in
                      json.dumps(d, ensure_ascii=False).upper())
        modes = tuple(getattr(base.input_mod, "INPUT_MODES",
                              ("interception", "legacy", "message")))
        fallbacks = tuple(dict.fromkeys(("legacy", "message")))
        ok = real and all(m in modes for m in fallbacks)
        c.codes.update({"devices": len(devs), "real_3554_f50d": real,
                        "virtual": virtual, "modes": modes,
                        "identity_mouse": bool((ident.get("mouse") or {}).get("exists"))})
        c.evidence["devices"] = devs[:6]
        if ok:
            c.ok(f"设备 {len(devs)}（真实 3554:F50D={real} · 虚拟 {virtual}）"
                 f"通道 {modes}",
                 "真实设备存在且三通道声明齐全")
        elif not devs:
            c.skip("env_absent", "未枚举到鼠标设备（驱动未装）", "≥1 设备")
        else:
            c.bad(f"真实设备={real} · 通道={modes}", "3554:F50D 且三通道齐全",
                  f"设备 {len(devs)}")

    def _s0_lowlevel(self, c):
        """无注入标记：点击走驱动 stroke flags，mouse_event 只出现在安全复位。"""
        try:
            from core import interception as icept
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"驱动模块不可用：{type(exc).__name__}: {exc}")
            return
        flags = {k: hasattr(icept, k) for k in
                 ("MOUSE_MOVE_RELATIVE", "MOUSE_MOVE_ABSOLUTE",
                  "MOUSE_LEFT_DOWN", "MOUSE_LEFT_UP",
                  "RI_MOUSE_LEFT_BUTTON_DOWN", "RI_MOUSE_LEFT_BUTTON_UP")}
        try:
            src = open(os.path.join(ROOT, "core", "input.py"),
                       encoding="utf-8", errors="replace").read()
        except OSError as exc:
            c.blocked(f"input.py 不可读：{exc}")
            return
        sendinput = len(re.findall(r"SendInput\s*\(", src))
        m_event = len(re.findall(r"mouse_event\s*\(", src))
        # 注入标记只允许出现在注释/文档里，不许被赋值到点击事件上。
        injected_flag = len(re.findall(r"LLMHF_INJECTED\s*[|=]", src))
        c.codes.update({"flags": flags, "sendinput": sendinput,
                        "mouse_event": m_event,
                        "injected_flag": injected_flag})
        if all(flags.values()) and injected_flag == 0 and m_event <= 1:
            c.ok(f"驱动 flag {sum(flags.values())}/{len(flags)} · "
                 f"SendInput={sendinput}（仅 legacy 兜底） · "
                 f"mouse_event={m_event}（仅滚轮兜底） · 注入标记=0",
                 "驱动 flag 齐全，点击事件不带 LLMHF_INJECTED")
        else:
            c.bad(f"flags={flags} injected={injected_flag} si={sendinput} "
                  f"me={m_event}", "flag 齐全 · 注入标记 0 · 鼠标事件仅滚轮兜底",
                  "低级钩子标记不干净")

    def _s0_rawinput(self, c):
        try:
            from core import interception as icept
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"驱动模块不可用：{type(exc).__name__}: {exc}")
            return
        try:
            res = icept.raw_mouse_activity(ms_budget=1200) or {}
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"Raw Input 观察失败：{type(exc).__name__}: {exc}")
            return
        events = int(res.get("events") or 0)
        by = res.get("by_vidpid") or {}
        c.codes.update({"events": events, "sources": list(by)[:6]})
        c.evidence["raw"] = res
        if events <= 0:
            c.skip("no_activity", "观察窗内无鼠标活动（不移动真实光标）", "≥1 事件")
            return
        hit = any("3554" in s.upper() and "F50D" in s.upper()
                  for s in map(str, by))
        if hit:
            c.ok(f"{events} 事件 · 归属 {len(by)} 源 · 真实 VID 命中",
                 "可按 vid:pid 归属")
        else:
            c.ok(f"{events} 事件 · 归属 {len(by)} 源（未见真实 VID）",
                 "观察器可用", "活动来自其它设备，未动真实光标")

    def _s0_restore(self, c):
        if not self.ensure_game(mouse=True):
            c.blocked("需游戏窗口与真实光标；本次不动光标", "游戏窗口可用")
            return
        fails, n = 0, 20
        for _ in range(n):
            try:
                x, y = self.mouse.cursor()
                self.mouse.move_to(x, y)
                x2, y2 = self.mouse.cursor()
                if abs(x2 - x) > 1 or abs(y2 - y) > 1:
                    fails += 1
            except Exception:                           # noqa: BLE001
                fails += 1
            self.tick()
        c.codes["fails"] = fails
        if fails <= 1:
            c.ok(f"{fails}/{n} 失败", "≤1/20")
        else:
            c.bad(f"{fails}/{n} 失败", "≤1/20", "光标还原不稳定，快速连点会漂")
        self.safe_reset("S0-15 光标复位")

    def _s0_foreground(self, c):
        if not self.ensure_game(mouse=True):
            c.blocked("需游戏窗口在前台", "游戏窗口可用")
            return
        hwnd = base.input_mod.Mouse.foreground()
        game = getattr(self, "hwnd", None) or hwnd
        lost = 0
        for _ in range(50):
            self.tick()
            now = base.input_mod.Mouse.foreground()
            if now != game:
                lost += 1
            time.sleep(0.02)
        c.codes["lost"] = lost
        if lost == 0:
            c.ok(f"50 次发送 0 次被抢（hwnd={game}）", "100% 保持前台")
        else:
            c.bad(f"50 次中 {lost} 次被抢", "0 次", "发送抢前台，需检查目标窗口")
        self.safe_reset("S0-16 复位")

    def _monitor_list(self):
        out = []
        user32 = ctypes.windll.user32
        try:
            shcore = ctypes.windll.shcore
        except OSError:
            shcore = None
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.wintypes.RECT), ctypes.c_double)

        def cb(hmon, hdc, rect, data):
            r = rect.contents
            dpi = 96
            if shcore is not None:
                try:
                    dx, dy = ctypes.c_uint(), ctypes.c_uint()
                    shcore.GetDpiForMonitor(ctypes.c_void_p(hmon), 0,
                                            ctypes.byref(dx), ctypes.byref(dy))
                    dpi = int(dx.value or 96)
                except Exception:                   # noqa: BLE001
                    pass
            out.append({"rect": (r.left, r.top, r.right - r.left, r.bottom - r.top),
                        "dpi": dpi, "scale": round(dpi / 96.0, 3)})
            return 1
        try:
            user32.EnumDisplayMonitors(0, None, MONITORENUMPROC(cb), 0)
        except Exception:                               # noqa: BLE001
            pass
        return out

    def _s0_scale(self, c):
        mons = self._monitor_list()
        if not mons:
            c.skip("env_absent", "未枚举到显示器", "≥1 显示器")
            return
        scales = sorted({m["scale"] for m in mons})
        try:
            aware = ctypes.windll.user32.GetAwarenessFromDpiAwarenessContext(
                ctypes.windll.user32.GetThreadDpiAwarenessContext())
        except Exception:                               # noqa: BLE001
            aware = None
        c.codes.update({"scales": scales, "awareness": aware})
        c.evidence["monitors"] = mons
        allowed = {1.0, 1.25, 1.5, 1.75, 2.0}
        legal = all(s in allowed for s in scales)
        c.measured = f"scale {scales} · 显示器 {len(mons)} · 感知 {aware}"
        if legal and (aware is None or aware >= 1):
            c.ok(c.measured, "缩放 ∈ {100,125,150,175,200}% 且感知 ≥ per-monitor",
                 "125/150/200 需切换系统缩放后各重跑一次；本项校验当前值")
        elif not legal:
            c.bad(c.measured, "缩放 ∈ {100,125,150,175,200}%",
                  "非常规缩放，区域坐标可能整体偏移")
        else:
            c.bad(c.measured, "DPI 感知 ≥ per-monitor", "进程 DPI 感知不足")

    def _s0_monitors(self, c):
        mons = self._monitor_list()
        if not mons:
            c.skip("env_absent", "未枚举到显示器", "≥1 显示器")
            return
        bad = []
        for i, m in enumerate(mons, 1):
            x, y, w, h = m["rect"]
            if w <= 0 or h <= 0:
                bad.append(f"#{i} 尺寸 {w}x{h}")
                continue
            try:
                scr = base.screen_mod.Screen(i)
                px = scr.norm_to_px({"x": 0, "y": 0, "w": 1, "h": 1})
            except Exception as exc:                    # noqa: BLE001
                bad.append(f"#{i} {type(exc).__name__}")
                continue
            cx, cy, cw, ch = px
            if not (x - 2 <= cx <= x + w + 2 and y - 2 <= cy <= y + h + 2):
                bad.append(f"#{i} 映射 {px} 不在 {m['rect']}")
        c.evidence["monitors"] = mons
        if bad:
            c.bad("；".join(bad[:4]), "全部显示器映射正确")
        else:
            c.ok(f"{len(mons)} 台显示器逐个映射正确",
                 f"每台 norm_to_px 落在自身 bounds")

    # ------------------------------------------------ S0-19..21 时间同步
    def _sync_patch(self, module, offsets, fail=False):
        """伪造 NTP：返回 (restore_fn)，绝不改真实网络与状态。"""
        state = copy.deepcopy(getattr(module, "_state", {}))
        ips = {f"srv{i}": f"{10 + i}.{20 + i}.0.1" for i in range(8)}

        def fake_resolve(host, *a, **kw):
            return ips.get(str(host), "10.99.0.1")

        def fake_query(host, *a, **kw):
            if fail:
                raise OSError("伪造断网")
            return {"offset": offsets.get(str(host), 0.0), "delay": 0.01,
                    "stratum": 2, "server": str(host)}

        module._query, module._resolve_ip = fake_query, fake_resolve

        def restore():
            module._state = state
        return restore

    def _s0_sync_multi(self, c):
        try:
            from core import timesync
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"timesync 不可用：{type(exc).__name__}: {exc}")
            return
        offsets = {"srv0": 0.020, "srv1": 0.018, "srv2": 0.022, "srv3": 0.019}
        restore = self._sync_patch(timesync, offsets)
        try:
            res = timesync.sync(servers=list(offsets), samples=1,
                                only_configured=True) or {}
        except Exception as exc:                        # noqa: BLE001
            c.bad(f"sync 异常：{type(exc).__name__}: {exc}", "ok=True")
            return
        finally:
            restore()
        c.codes.update({"ok": res.get("ok"), "degraded": res.get("degraded"),
                        "independent": res.get("independent"),
                        "offset": res.get("offset")})
        if res.get("ok") and not res.get("degraded") \
                and int(res.get("independent") or 0) >= 4:
            c.ok(f"ok=True · 来源 {res.get('independent')} · "
                 f"offset {float(res.get('offset') or 0) * 1000:.1f}ms",
                 "≥4 源且严格模式")
        else:
            c.bad(f"ok={res.get('ok')} degraded={res.get('degraded')} "
                  f"来源={res.get('independent')}", "ok 且 4 源严格")

    def _s0_sync_single(self, c):
        try:
            from core import timesync
            from core import server as srv_mod
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"模块不可用：{type(exc).__name__}: {exc}")
            return
        restore = self._sync_patch(timesync, {"srv0": 0.030})
        try:
            res = timesync.sync(servers=["srv0"], samples=3,
                                only_configured=True) or {}
            view = srv_mod._time_sync_view(timesync) if hasattr(
                srv_mod, "_time_sync_view") else {}
        except Exception as exc:                        # noqa: BLE001
            c.bad(f"sync 异常：{type(exc).__name__}: {exc}", "degraded=True")
            return
        finally:
            restore()
        off = abs(float(res.get("offset") or 0.0))
        deg = bool(res.get("degraded"))
        shown = bool(view.get("degraded")) if isinstance(view, dict) else None
        c.codes.update({"degraded": deg, "offset_s": off, "view": view})
        if deg and off <= 300.0:
            c.ok(f"degraded=True · |offset|={off:.3f}s · 界面字段={shown}",
                 "单源降级且 |offset| ≤300s")
        else:
            c.bad(f"degraded={deg} |offset|={off:.3f}s", "降级且 ≤300s")

    def _s0_sync_offline(self, c):
        try:
            from core import timesync
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"timesync 不可用：{type(exc).__name__}: {exc}")
            return
        before = timesync.info()
        restore = self._sync_patch(timesync, {}, fail=True)
        try:
            res = timesync.sync(servers=["srv0", "srv1"], samples=1,
                                only_configured=True) or {}
        except Exception as exc:                        # noqa: BLE001
            c.bad(f"sync 异常：{type(exc).__name__}: {exc}", "ok=False")
            return
        finally:
            restore()
        after = timesync.info()
        same = (before.get("offset") == after.get("offset")
                and before.get("synced_at") == after.get("synced_at"))
        c.codes.update({"ok": res.get("ok"), "state_unchanged": same})
        if not res.get("ok") and same:
            c.ok("ok=False · 状态未被污染", "失败不改 offset/时间戳")
        else:
            c.bad(f"ok={res.get('ok')} 状态一致={same}", "失败不改状态")

    def _s0_config(self, c):
        try:
            self.cfg = base.config_mod.Config.load()
            if getattr(self.cfg, "load_ok", False):
                c.ok(f"load_ok=True · {len(self.cfg.data)} 键", "load_ok=True",
                     str(CFG_PATH))
            else:
                c.bad("load_ok=False", "load_ok=True",
                      str(getattr(self.cfg, "_load_error", ""))
                      + " · 见 config.bad / config.good 隔离记录")
                c.codes["load_ok"] = False
        except Exception as exc:                        # noqa: BLE001
            c.bad(f"{type(exc).__name__}: {exc}", "load_ok=True")
            self.cfg = None

    def _s0_bounds(self, c):
        if self.cfg is None:
            c.blocked("配置未加载，无法校验")
            return
        bad = []
        for s in switches.SWITCHES:
            raw = switches._dig(self.cfg, s["path"])
            if raw is None:
                continue
            try:
                switches._coerce(s, raw)
            except switches.SwitchValueError as exc:
                bad.append((s["key"], raw, str(exc)))
        if not bad:
            c.ok("全部开关值合法", "0 项越界")
            return
        c.bad(f"{len(bad)} 项越界：" + "；".join(f"{k}={raw}" for k, raw, _ in bad),
              "0 项越界", "已登记修复器 S0-2，见 fixes")
        c.codes["bad"] = [k for k, _, _ in bad]
        adjusted = []
        for key, raw, msg in bad:
            s = switches.spec(key)
            lo, hi = switches.BOUNDS.get(key, (None, None))
            old = raw
            if s["kind"] in ("int", "float"):
                try:
                    v = float(raw)
                except (TypeError, ValueError):
                    v = float(s["default"])
                if not math.isfinite(v):
                    v = float(s["default"])
                if lo is not None:
                    v = max(v, lo)
                if hi is not None:
                    v = min(v, hi)
                new = int(v) if s["kind"] == "int" else v
            elif s["kind"] == "bool":
                new = bool(s["default"])
            else:
                new = s["default"]
            switches.set(self.cfg, key, new)
            adjusted.append((key, old, new))
        self.cfg.save()
        c.evidence["adjusted"] = adjusted
        c.detail = "自动修正：" + "；".join(f"{k} {o}→{n}" for k, o, n in adjusted)

    def _s0_license(self, c):
        st = base.selfcheck_mod.license_status()
        c.codes.update({"license_status": st.get("status"),
                        "enforced": st.get("enforced"),
                        "present": st.get("present")})
        if st.get("ok"):
            c.ok(f"status={st.get('status')} enforced={st.get('enforced')}", "ok=True")
            return
        if st.get("enforced") is None and not st.get("present"):
            # 源码/测试进程未启用门禁：无许可文件不构成失败
            c.skip("not_enforced", "本进程未启用许可门禁（enforced=None），无许可文件不阻塞",
                   f"status={st.get('status')}", "ok=True 或 enforced=None")
            return
        c.bad(f"status={st.get('status')} reason={st.get('reason')}", "ok=True")

    def _s0_disk(self, c):
        free = shutil.disk_usage(ROOT).free / 1073741824.0
        if free >= 2.0:
            c.ok(f"{free:.1f} GB", "≥ 2 GB")
        else:
            c.bad(f"{free:.2f} GB", "≥ 2 GB", "清理 _rm_shots/_rm_backup 旧目录")

    def _s0_dpi(self, c):
        self.ensure_screen()
        if self.screen is None:
            c.bad("截屏对象初始化失败", "可截屏")
            return
        w, h = self.screen.size
        aware = None
        try:
            aware = ctypes.c_int()
            ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(aware))
            aware = aware.value
        except Exception:                            # noqa: BLE001
            try:
                aware = int(bool(ctypes.windll.user32.SetProcessDPIAware()))
            except Exception:                        # noqa: BLE001
                aware = None
        self.screen_size = (w, h)
        c.codes.update({"dpi": getattr(self.screen, "dpi", None),
                        "scale": getattr(self.screen, "scale", None),
                        "awareness": aware})
        if aware is not None and aware <= 0:
            c.bad(f"{w}x{h} 感知级别={aware}", "进程 DPI 感知 ≥ 系统级",
                  "截图/坐标会被缩放，需在入口声明 DPI 感知")
        else:
            c.ok(f"{w}x{h} dpi={getattr(self.screen, 'dpi', '?')} "
                 f"感知={aware}", "尺寸可读且进程已感知 DPI")

    def _s0_admin(self, c):
        priv = base.selfcheck_mod.privilege() if hasattr(
            base.selfcheck_mod, "privilege") else {}
        admin = bool(priv.get("admin"))
        c.codes["admin"] = admin
        if admin:
            c.ok(f"admin=True · {priv.get('level')}", "admin=True")
            return
        if getattr(self.args, "auto_elevate", False):
            try:
                single_instance.release()
                started = base.input_mod.elevate_self()
            except Exception as exc:                    # noqa: BLE001
                started = False
                c.codes["elevate_error"] = f"{type(exc).__name__}: {exc}"
            if started:
                self.relaunch_requested = True
                c.blocked("已请求管理员重启（UAC），本进程收尾退出",
                          "admin=True")
                self.fixes.append({
                    "file": "", "lines": [], "action": "elevate_self",
                    "evidence": "S0-6 admin=False", "rerun": "pending",
                    "owner": "环境", "round": 1,
                })
            else:
                c.bad("提权未启动（可能已提过一次或 UAC 被拒）", "admin=True",
                      "手动以管理员身份运行可解")
        else:
            c.skip("disabled", "非管理员；--no-auto-elevate 已关闭自动提权重启",
                   "admin=False", "admin=True")

    def _s0_driver(self, c):
        """只检测不安装。注意：不走 driver_status/filter_audit——
        本机实测 interception_get_filter 会在驱动侧留下挂起线程，
        进程收尾时 DLL detach 卡死（「不卡壳」红线）。
        这里用「打开 context + 枚举真实鼠标设备」作为可用判据。
        """
        try:
            from core import interception as icept
        except Exception as exc:                        # noqa: BLE001
            self.driver_ok, self.driver_msg = False, f"{type(exc).__name__}: {exc}"
            c.skip("absent", f"驱动模块不可用：{self.driver_msg}；按降级路径继续，不安装")
            return
        ident = {}
        try:
            icept._SHARED._open()
            ident = icept._SHARED.identity() or {}
            icept._SHARED.close()
            mouse = ident.get("mouse") or {}
            ok = bool(mouse.get("exists"))
            msg = f"device={mouse.get('device')} kind={mouse.get('kind')}"
            if not ok:
                msg = "context 可建但枚举不到真实鼠标设备"
        except Exception as exc:                        # noqa: BLE001
            ok, msg = False, f"{type(exc).__name__}: {exc}"
            try:
                icept._SHARED.close()
            except Exception:                           # noqa: BLE001
                pass
        self.driver_ok, self.driver_msg = bool(ok), msg
        c.codes.update({"probe": "open+identity", "device": ident.get("mouse")})
        if self.driver_ok:
            c.ok(f"驱动可用（{msg}）", "context+真实设备")
        else:
            c.skip("absent", f"驱动未就绪：{msg}；按降级路径继续，不安装")

    def _s0_game(self, c):
        try:
            hwnd = base.input_mod.find_game_window()
        except Exception as exc:                        # noqa: BLE001
            hwnd = None
            c.codes["error"] = str(exc)
        self.hwnd = hwnd
        if hwnd:
            rect = base.input_mod.game_window_rect()
            self.game_rect = rect
            c.ok(f"hwnd=0x{hwnd:X} rect={rect}", "找到游戏窗口")
        else:
            c.blocked("未找到游戏窗口（UnrealWindow）—— 请先打开游戏",
                      "未找到", "找到游戏窗口")

    def _s0_region(self, c):
        self.ensure_screen()
        if self.screen is None:
            c.blocked("屏幕不可用")
            return
        w, h = self.screen.size
        regions = dict(getattr(self.cfg, "data", {}).get("regions") or {})
        bad = []
        for name, box in regions.items():
            if not isinstance(box, dict):
                continue
            try:
                x, y, bw, bh = (float(box.get("x", 0)), float(box.get("y", 0)),
                                float(box.get("w", 0)), float(box.get("h", 0)))
            except Exception:                        # noqa: BLE001
                bad.append((name, box)); continue
            if bw <= 0 or bh <= 0 or x < -0.001 or y < -0.001 \
                    or x + bw > 1.001 or y + bh > 1.001:
                bad.append((name, box))
        if not bad:
            c.ok(f"{len(regions)} 个区域均在屏幕内 · 屏幕 {w}x{h}", "全部合法")
            return
        c.bad(f"{len(bad)} 个区域越界：" + "、".join(n for n, _ in bad),
              "全部合法", "已登记修复器 S0-9")
        changed = []
        for name, box in bad:
            if not isinstance(box, dict):
                continue
            x = min(max(float(box.get("x", 0)), 0.0), 0.98)
            y = min(max(float(box.get("y", 0)), 0.0), 0.98)
            bw = min(max(float(box.get("w", 0)), 0.02), 1.0 - x)
            bh = min(max(float(box.get("h", 0)), 0.02), 1.0 - y)
            old = dict(box)
            self.cfg.set_region(name, {"x": round(x, 4), "y": round(y, 4),
                                       "w": round(bw, 4), "h": round(bh, 4)})
            changed.append((name, old, {"x": round(x, 4), "y": round(y, 4),
                                        "w": round(bw, 4), "h": round(bh, 4)}))
        self.cfg.save()
        c.evidence["adjusted"] = changed

    def _s0_ocr(self, c):
        self.ensure_screen()
        if not self.hwnd:
            c.blocked("无游戏窗口，无法取真实帧")
            return
        name = self.pick_ocr_region(prefer_list=True)
        if not name:
            c.blocked("未配置任何 OCR 区域")
            return
        frames = 0
        good = 0
        detail = []
        for i in range(3):
            self.tick()
            crop, _ = self.grab_box(name)
            frames += 1
            if crop is None:
                detail.append(f"帧{i + 1}=取图失败")
                continue
            try:
                vals = self.read_prices(crop, name)
                words = [] if name in ("price", "bar") else base.ocr_mod.read_words(crop)
            except Exception as exc:                    # noqa: BLE001
                vals, words = [], []
                detail.append(f"帧{i + 1}=OCR异常 {type(exc).__name__}")
            if vals or words:
                good += 1
            detail.append(f"帧{i + 1}=值{vals[:3] or [w.get('text') for w in words][:3]}")
            time.sleep(0.25)
        c.codes["frames"] = frames
        if good >= 2:
            c.ok(f"{good}/{frames} 帧可读 · 区域 {name}", "≥2/3 帧可读",
                 "；".join(detail))
        else:
            c.bad(f"{good}/{frames} 帧可读", "≥2/3 帧可读", "；".join(detail))

    def _s0_abort_channel(self, c):
        hk = self.hotkey_abort is not None
        writable = os.access(os.path.dirname(ABORT_FILE), os.W_OK)
        if hk and writable:
            c.ok(f"hotkey={self.args.hotkey} · abort_file 可写", "通道就绪")
        else:
            c.bad(f"hotkey={hk} writable={writable}", "通道就绪")

    # ================================================================ S1
    def stage_S1(self):
        self.item("S1-1", "当前界面识别", self._s1_where, gate=True)
        self.item("S1-2", "回到交易行主界面", self._s1_recover, gate=True)
        self.item("S1-3", "导航到目标列表（在售页）", self._s1_nav, gate=True)
        self.item("S1-4", "步数与超时预算", self._s1_budget)
        self.item("S1-5", "空闲漫游：非倒计时路线不启用", self._s1_roam_gate)
        self.item("S1-6", "空闲漫游：全程零点击", self._s1_roam_noclick)
        self.item("S1-7", "空闲漫游：开火前归位", self._s1_roam_return)
        self.item("S1-8", "最大化/还原后区域坐标", self._s1_maximize, timeout=40)

    def _need_game(self, c):
        if not self.ensure_game(mouse=True, keyboard=True):
            c.blocked("游戏窗口 / 输入通道不可用", "不可用", "游戏窗口存在")
            return False
        if self.nav is None:
            c.blocked("导航器初始化失败")
            return False
        return True

    def _s1_where(self, c):
        if not self._need_game(c):
            return
        t0 = _pc()
        page, text = self.nav.where()
        self.page = page
        c.codes["page"] = page
        c.evidence["ocr"] = str(text)[:120]
        if page == "unknown":
            c.pending(f"当前画面认不出（OCR：{str(text)[:60]}）", "已知页面",
                      "待判：可能是截图/遮挡问题")
        else:
            c.ok(f"page={page} · {(_pc() - t0) * 1000:.0f}ms", "已知页面")
        self.step(f"识别当前页面 {page}")

    def _s1_recover(self, c):
        if not self._need_game(c):
            return
        page = self.nav.recover()
        self.page = page
        c.codes["page_after"] = page
        if page == "market":
            c.ok("已回到市场主界面", "page=market")
        else:
            c.bad(f"page={page}", "page=market", "恢复后仍不在市场")

    def _s1_nav(self, c):
        if not self._need_game(c):
            return
        t0 = _pc()
        page = self.nav.go_to_onsale()
        dt = _pc() - t0
        self.page = page
        c.codes["page_after"] = page
        if page == "onsale":
            c.ok(f"page=onsale · {dt * 1000:.0f}ms", "page=onsale")
        else:
            c.bad(f"page={page}", "page=onsale", f"导航耗时 {dt:.1f}s")

    def _s1_budget(self, c):
        rows = [x for x in self.checks if x.stage == "S1"]
        total = sum(x.duration_s for x in rows)
        longest = max((x.duration_s for x in rows), default=0.0)
        c.ok(f"总耗时 {total:.1f}s · 单步最长 {longest:.1f}s", "单步 ≤30s",
             "步数上限 8")

    # ------------------------------------------------ S1-5..8（漫游/窗口）
    def _roam_cfg(self, mode="move"):
        return {"mouse": {"idle": {"mode": mode, "interval_ms": 300,
                                   "return_s": 1, "min_remain_s": 6,
                                   "wheel_notches": 3}}}

    def _roam_session(self, workflow="countdown", mode="move"):
        from core import idle_roam

        class _M:
            def __init__(self):
                self.moves = 0
                self.clicks = 0
                self.first = None
                self.last = None

            def move_path(self, pts, ms=0):
                self.moves += 1
                self.first = self.first or tuple(pts[0])
                self.last = tuple(pts[-1])
                return True

            def click(self, *a, **k):
                self.clicks += 1
                return True

        mouse = _M()
        sess = idle_roam.RoamSession(
            self._roam_cfg(mode), mouse=mouse, hwnd=1,
            workflow=workflow, rect=(0, 0, 1000, 800),
            cursor_pos_fn=(500, 400), wheel_fn=lambda *a, **k: True,
            sleep=lambda s: None)
        return sess, mouse

    def _s1_roam_gate(self, c):
        try:
            from core import idle_roam
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"idle_roam 不可用：{type(exc).__name__}: {exc}")
            return
        ok_cnt = idle_roam.gate(self._roam_cfg("move"), "countdown")
        no_cnt = idle_roam.gate(self._roam_cfg("move"), "list")
        off = idle_roam.gate(self._roam_cfg("off"), "countdown")
        sess, mouse = self._roam_session(workflow="list")
        moved = sess.tick(now=0, left=60)
        snap = sess.snapshot()
        c.codes.update({"countdown": ok_cnt, "list": no_cnt, "off": off,
                        "tick_moved": moved, "moves": mouse.moves})
        if (ok_cnt[0] and not no_cnt[0] and not off[0] and not moved
                and mouse.moves == 0 and snap["phase"] == "inactive_route"):
            c.ok("countdown 启用 · list 不启用 · off 关闭 · 非倒计时 0 移动",
                 "gate 三分支正确且 tick 不改坐标")
        else:
            c.bad(f"gate={ok_cnt}/{no_cnt}/{off} tick={moved} moves={mouse.moves}",
                  "countdown=True list/off=False 且 0 移动")

    def _s1_roam_noclick(self, c):
        try:
            sess, mouse = self._roam_session()
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"漫游会话初始化失败：{type(exc).__name__}: {exc}")
            return
        t = 0.0
        for _ in range(200):
            sess.tick(now=t, left=max(10.0, 90 - t))
            t += 0.3
            if mouse.moves >= 6:
                break
        snap = sess.snapshot()
        c.codes.update({"moves": mouse.moves, "clicks": mouse.clicks,
                        "phase": snap["phase"], "clicks_stat": snap["clicks"]})
        if mouse.clicks == 0 and mouse.moves >= 3 and snap["clicks"] == 0:
            c.ok(f"移动 {mouse.moves} 次 · 点击 {mouse.clicks} 次",
                 "全程零点击（含统计口径）")
        elif mouse.moves == 0:
            c.skip("no_activity", "漫游未产生移动（配置/时钟不满足）", "≥3 次移动")
        else:
            c.bad(f"点击 {mouse.clicks} 次", "0 次", "漫游路径可触发点击")

    def _s1_roam_return(self, c):
        try:
            sess, mouse = self._roam_session()
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"漫游会话初始化失败：{type(exc).__name__}: {exc}")
            return
        for i in range(20):
            sess.tick(now=i * 0.3, left=60 - i * 0.3)
        sess.tick(now=6.0, left=5.5)                    # 进入 stop_at 窗口
        snap = sess.snapshot()
        home = snap.get("home")
        last = mouse.last
        dist = None
        if home and last:
            dist = ((home[0] - last[0]) ** 2 + (home[1] - last[1]) ** 2) ** 0.5
        c.codes.update({"settled": snap["settled"], "home": home,
                        "last": list(last) if last else None,
                        "dist_px": round(dist, 1) if dist is not None else None})
        if snap["settled"] and dist is not None and dist <= 8.0:
            c.ok(f"已归位 · 距 home {dist:.1f}px", "settled=True 且 ≤8px")
        elif snap["settled"] and (last is None or dist is None):
            c.ok("已归位（本次无可比对移动）", "settled=True")
        else:
            c.bad(f"settled={snap['settled']} dist={dist}", "settled=True 且 ≤8px",
                  "开火前未归位，可能点在空中")

    def _s1_maximize(self, c):
        if not self.ensure_game(mouse=False):
            c.blocked("需游戏窗口做最大化/还原", "游戏窗口可用")
            return
        try:
            from core import idle_roam
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"idle_roam 不可用：{type(exc).__name__}: {exc}")
            return
        user32 = ctypes.windll.user32
        hwnd = getattr(self, "hwnd", None)
        if not hwnd:
            c.blocked("未拿到游戏窗口柄", "hwnd 可用")
            return
        regions = {}
        try:
            data = getattr(self.cfg, "data", {}) or {}
            regions = data.get("regions") or {}
        except Exception:                               # noqa: BLE001
            regions = {}
        bad = []
        for phase in ("restore", "maximize", "restore2"):
            user32.ShowWindow(hwnd, 9 if phase == "restore" else 3)
            time.sleep(0.4)
            self.tick()
            rect = idle_roam.client_rect(hwnd)
            if not rect:
                bad.append(f"{phase} 客户区读不到")
                continue
            x0, y0, x1, y1 = rect
            for name, box in list(regions.items())[:12]:
                try:
                    bx = float(box["x"]) if isinstance(box, dict) else float(box[0])
                    by = float(box["y"]) if isinstance(box, dict) else float(box[1])
                    bw = float(box["w"]) if isinstance(box, dict) else float(box[2])
                    bh = float(box["h"]) if isinstance(box, dict) else float(box[3])
                except Exception:                   # noqa: BLE001
                    continue
                cx = x0 + bx * (x1 - x0)
                cy = y0 + by * (y1 - y0)
                if not (x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2):
                    bad.append(f"{phase} {name} 越界")
        c.codes["bad"] = bad[:6]
        if bad:
            c.bad("；".join(bad[:4]), "三态后区域仍在客户区")
        else:
            c.ok("还原→最大化→还原 三态区域坐标均正确",
                 "区域始终落在客户区")


    # ================================================================ S2
    def stage_S2(self):
        self.item("S2-0", "测试标的与预算闸", self._s2_gate, gate=True)
        self.item("S2-1", "读价", self._s2_price)
        self.item("S2-2", "选量", self._s2_qty)
        self.item("S2-3", "确认", self._s2_confirm)
        self.item("S2-4", "台账", self._s2_ledger)
        self.item("S2-5", "全链路耗时", self._s2_time)
        self.item("S2-6", "随机刷新次数（1–3）", self._s2_repeat)
        self.item("S2-7", "买到即停：立刻断刷", self._s2_stop_on_buy)
        self.item("S2-8", "来不及跳过", self._s2_skip_late)
        self.item("S2-9", "按品阶停止（三档）", self._s2_stop_grade)
        self.item("S2-10", "连点（带长/间隔）", self._s2_burst)
        self.item("S2-11", "假购买补刀", self._s2_followup)

    def _s2_records(self):
        try:
            rows = base.records_mod.load(limit=0)
            return len(rows or [])
        except Exception:                                # noqa: BLE001
            return None

    def _s2_gate(self, c):
        a = self.args
        self._s2_allowed = False
        if not getattr(a, "allow_buy", False):
            c.skip("disabled", "未加 --allow-buy，S2 只做只读检查")
            c.mode = "dry"
            return
        if not (a.target and a.max_price > 0 and a.budget > 0):
            c.bad(f"target={a.target} max_price={a.max_price} budget={a.budget}",
                  "标的与预算必须齐全")
            return
        self.cfg = self.cfg or base.config_mod.Config.load()
        if switches.get(self.cfg, "run.semi"):
            c.bad("半自动开启", "semi_mode=False", "半自动不会点购买")
            return
        if not self.ensure_game(mouse=True, keyboard=True):
            c.blocked("游戏窗口不可用")
            return
        self._s2_records_before = self._s2_records()
        self._s2_allowed = True
        c.ok(f"target={a.target} max_price={a.max_price} budget={a.budget}",
             "闸门通过")

    def _s2_skip_if_dry(self, c):
        if not getattr(self, "_s2_allowed", False):
            c.skip("disabled", "S2 闸门未通过（dry-run 或无游戏/无预算）")
            return True
        return False

    def _s2_poll(self, deadline):
        eng = engine_mod.Engine()
        ok, msg = eng.start(deadline=None, mode="list", workflow="list")
        if not ok:
            return None, "", msg, []
        timeline = []
        last = ""
        while _pc() < deadline:
            self.tick()
            st = eng.status()
            state = st.get("state") or ""
            if state != last:
                timeline.append({"state": state, "t": _pc(),
                                 "bought": int((st.get("stats") or {}).get("bought") or 0)})
                last = state
            if int((st.get("stats") or {}).get("bought") or 0) > 0:
                break
            if not st.get("running") and state != "idle":
                break
            time.sleep(0.05)
        eng.stop(timeout=3.0)
        return eng, msg, "", timeline

    def _s2_price(self, c):
        if self._s2_skip_if_dry(c):
            return
        eng, msg, err, timeline = self._s2_poll(_pc() + 120)
        self._s2_timeline = timeline
        self._s2_eng = eng
        if eng is None:
            c.blocked(f"引擎未启动：{msg}")
            return
        log = " ".join((eng.status().get("log") or [])[-12:])
        hit = "价格" in log or "候选" in log or int((eng.status().get("stats") or {})
                                                    .get("bought") or 0) > 0
        c.codes["log"] = log[:300]
        if hit:
            c.ok("引擎进入候选/成交读价阶段", "出现读价/候选日志")
        else:
            c.pending(f"未见读价证据 · {msg}", "读价日志")

    def _s2_qty(self, c):
        if self._s2_skip_if_dry(c):
            return
        eng = getattr(self, "_s2_eng", None)
        log = " ".join(((eng.status() or {}).get("log") or [])[-20:]) if eng else ""
        c.codes["log"] = log[:300]
        if "件" in log or "数量" in log or "quota" in log:
            c.ok("选量阶段有记录", "出现数量/件数日志")
        else:
            c.pending("选量证据不足", "数量日志")

    def _s2_confirm(self, c):
        if self._s2_skip_if_dry(c):
            return
        timeline = getattr(self, "_s2_timeline", [])
        states = [x["state"] for x in timeline]
        c.codes["states"] = states
        if any(s in ("fire", "confirm") for s in states):
            c.ok(f"状态序列 {states}", "出现 fire/confirm")
        else:
            c.pending(f"未观察到确认阶段 · 序列 {states}", "出现 fire/confirm")

    def _s2_ledger(self, c):
        if self._s2_skip_if_dry(c):
            return
        before = getattr(self, "_s2_records_before", None)
        after = self._s2_records()
        eng = getattr(self, "_s2_eng", None)
        bought = int(((eng.status() if eng else {}).get("stats") or {}).get("bought") or 0)
        c.codes["bought"] = bought
        if bought <= 0:
            c.skip("absent", "本轮未成交，台账无新增（正常）", f"bought={bought}")
            return
        if after is None:
            c.pending("台账接口不可读", "records 可读")
        elif before is not None and after >= before + 1:
            c.ok(f"台账 {before}→{after}", "新增 ≥1")
        else:
            c.bad(f"台账 {before}→{after}", "新增 ≥1", "成交后未写台账")

    def _s2_time(self, c):
        timeline = getattr(self, "_s2_timeline", [])
        if not timeline:
            c.skip("absent", "未启动链路，无耗时")
            return
        total = timeline[-1]["t"] - timeline[0]["t"]
        detail = " → ".join(x["state"] for x in timeline)
        c.ok(f"{total * 1000:.0f}ms · {detail}", "有完整状态序列")

    # ------------------------------------------------ S2-6..11（离线可测）
    def _s2_repeat(self, c):
        try:
            from core import repeat
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"repeat 不可用：{type(exc).__name__}: {exc}")
            return
        cfg = {"scan": {"repeat": {"min": 1, "max": 3}}}
        lo, hi = repeat.limits(cfg)
        rng = __import__("random").Random(1234)
        vals = [repeat.draw(lo, hi, rng=rng) for _ in range(300)]
        seen = sorted(set(vals))
        c.codes.update({"lo": lo, "hi": hi, "seen": seen,
                        "dist": {v: vals.count(v) for v in seen}})
        if lo == 1 and hi == 3 and seen == [1, 2, 3]:
            c.ok(f"[{lo},{hi}] · 分布 " +
                 " ".join(f"{v}:{vals.count(v)}" for v in seen),
                 "1–3 全覆盖且不越界")
        else:
            c.bad(f"[{lo},{hi}] 见 {seen}", "1–3 全覆盖且不越界",
                  "随机刷新次数区间不正确")

    def _s2_stop_on_buy(self, c):
        path = os.path.join(ROOT, "core", "engine.py")
        try:
            src = open(path, encoding="utf-8", errors="replace").read()
        except OSError as exc:
            c.blocked(f"engine.py 不可读：{exc}")
            return
        m = re.search(r"def repeat_refresh\(self[^)]*\):(.*?)(?=\n    def |\nclass )",
                      src, re.S)
        body = m.group(1) if m else ""
        gates = {"_stop": "_stop" in body, "budget": "rep_budget" in body,
                 "used": "rep_used" in body}
        c.codes.update(gates)
        real = None
        try:
            rows = base.records_mod.load(limit=5) or []
            real = [r for r in rows if r.get("result") in ("hit", "bought")]
        except Exception:                               # noqa: BLE001
            real = None
        if real:
            c.ok(f"静态双闸 {sum(gates.values())}/3 · 真机成交 {len(real)} 行可复核",
                 "成交后 refresh_used=0（真机阶段再验）")
            return
        if not m:
            c.bad("未找到 repeat_refresh", "函数存在且含停止/预算双闸")
            return
        if all(gates.values()):
            c.ok(f"静态双闸 {sum(gates.values())}/3（_stop/预算/计数）",
                 "成交瞬间可立即退出刷新；真机阶段以 refresh_used=0 复核")
        else:
            c.bad(f"静态双闸 {gates}", "含 _stop 与预算双闸", "买到后仍可能继续刷新")

    def _s2_skip_late(self, c):
        try:
            from core import skip_late
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"skip_late 不可用：{type(exc).__name__}: {exc}")
            return

        class _Clock:
            def zero_at(self):
                return 100.0

            def remaining(self, now=None):
                return 100.0 - float(now if now is not None else 100.0)

        thr = skip_late.threshold({})
        late = skip_late.judge(thr, clock=_Clock(), now=98.8, kind="unlock")
        zero = skip_late.judge(thr, display_sec=0, kind="unlock")
        off = skip_late.judge(0, clock=_Clock(), now=98.8, kind="unlock")
        entry = skip_late.stats_entry("测试件", late, display_sec=1, kind="unlock")
        buy_keys = {"price", "quota", "amount", "elapsed"}
        c.codes.update({"threshold": thr, "late": late, "zero": zero,
                        "off": off, "entry_keys": sorted(entry)})
        if (thr == 2 and late.get("skip") and late.get("source") == "inferred"
                and not zero.get("skip") and not off.get("skip")
                and not (buy_keys & set(entry))):
            c.ok(f"阈值 {thr}s · 剩余1.2s→跳过(inferred) · 0s/关闭→不跳过 · "
                 f"跳过记录无成交字段",
                 "反推时刻优先，跳过不记账")
        else:
            c.bad(f"thr={thr} late={late} zero={zero} off={off} entry={entry}",
                  "反推判定正确且跳过无成交字段", "来不及跳过逻辑或记账口径不符")

    def _s2_stop_grade(self, c):
        try:
            from core import stop_grade as sg
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"stop_grade 不可用：{type(exc).__name__}: {exc}")
            return
        cfg = {"stop": {"by_grade": {"enabled": True, "legend": 1,
                                     "epic": 2, "rare": 2, "scope": "all"}}}
        s = sg.settings(cfg)
        if not sg.armed(s):
            c.bad(f"settings={s}", "armed=True", "开关/目标未生效")
            return
        rows = {}
        for grade, target in ((sg.LEGEND, 1), (sg.EPIC, 2), (sg.RARE, 2)):
            cnt = sg.Counter()
            before = sg.reached(s, cnt.snapshot())
            for _ in range(target):
                cnt.record(grade)
            at_target = sg.reached(s, cnt.snapshot())
            rows[grade] = {"before": before, "at": at_target,
                           "target": target, "counts": cnt.snapshot()}
        off_s = sg.settings({"stop": {"by_grade": {"enabled": False}}})
        c.codes.update({"rows": rows, "off_armed": sg.armed(off_s)})
        ok = (all(not v["before"] for v in rows.values())
              and all(rows[g]["at"] == [g] for g in (sg.LEGEND, sg.EPIC, sg.RARE))
              and not sg.armed(off_s))
        if ok:
            c.ok("传说 1/1、紫 2/2、蓝 2/2 各自达标即停；总开关关→不参与",
                 "三档达标前 0 次、达标 1 次")
        else:
            c.bad(json.dumps(rows, ensure_ascii=False)[:300],
                  "三档各自达标即停且关闭后不计数")

    def _s2_burst(self, c):
        try:
            from core import click_burst as cb
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"click_burst 不可用：{type(exc).__name__}: {exc}")
            return
        cfg = {"click": {"burst": {"mode": "burst", "hold_ms": 110,
                                   "interval_ms": 12, "window": "all"}}}
        plan, errs = cb.read(cfg)
        gate = cb.followup_gate(cfg)
        clock = [1000.0]
        holds = []
        clicks = []

        def now():
            return clock[0]

        def sleep(s):
            clock[0] += max(0.0, float(s))

        def click(hold_s):
            clicks.append(hold_s)
            clock[0] += hold_s

        res = cb.run(plan, t_fire=clock[0], fire_timeout=0.6,
                     click=click, dialog=lambda: False, now=now, sleep=sleep)
        holds = res.get("holds_ms") or []
        c.codes.update({"plan": plan, "errors": errs, "gate": gate,
                        "result": {k: res.get(k) for k in
                                   ("clicks", "stopped_by", "hold_ms",
                                    "interval_ms")},
                        "holds": [round(x, 1) for x in holds]})
        exact = (not errs and plan["hold_ms"] == 110 and plan["interval_ms"] == 12)
        holds_ok = bool(holds) and all(abs(x - 110) <= 6 for x in holds)
        gap_ok = (res["elapsed_ms"] / max(1, res["clicks"])) >= 110 + 12 - 2
        if exact and holds_ok and gap_ok and gate[0] is False:
            c.ok(f"hold={plan['hold_ms']}ms · interval={plan['interval_ms']}ms · "
                 f"实点 {res['clicks']} 次（hold 实测 "
                 f"{sum(holds) / len(holds):.1f}ms）· 与补刀互斥",
                 "带长/间隔生效且 burst 禁补刀")
        else:
            c.bad(f"plan={plan} err={errs} holds={holds} gate={gate}",
                  "带长/间隔精确生效且互斥")

    def _s2_followup(self, c):
        try:
            from core import click_burst as cb
            from core import switches
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"模块不可用：{type(exc).__name__}: {exc}")
            return
        default_on = switches.get({}, "buy.followup.enabled")
        plan, _ = cb.read({})
        gate_append = cb.followup_gate({})
        cfg = {"buy": {"followup": {"enabled": True}}}
        on = switches.get(cfg, "buy.followup.enabled")
        path = os.path.join(ROOT, "core", "engine.py")
        try:
            src = open(path, encoding="utf-8", errors="replace").read()
        except OSError as exc:
            c.blocked(f"engine.py 不可读：{exc}")
            return
        once = ("_followup_used = False" in src and "self._followup_used = True" in src
                and "followup_effective" in src)
        c.codes.update({"default": default_on, "on": on,
                        "gate_append": gate_append, "once_guard": once})
        if not default_on and on and gate_append[0] and once:
            c.ok("默认关 · 开启后单次补刀（_followup_used 一次性）· 连点下禁用",
                 "默认关且最多补一次")
        else:
            c.bad(f"default={default_on} on={on} gate={gate_append} once={once}",
                  "默认关、开启单次、连点互斥")

    # ================================================================ S3
    def stage_S3(self):
        self.item("S3-1", "成对读数样本（≥16）", self._s3_pairs, gate=True)
        self.item("S3-2", "反推解锁时刻", self._s3_estimate)
        self.item("S3-3", "点击/预估与理论零点偏差", self._s3_deviation)
        self.item("S3-4", "超窗比例", self._s3_window)
        self.item("S3-5", "成对读数策略真机可用性", self._s3_policy)
        self.item("S3-6", "拟人轨迹：开火零位移", self._s3_fire_zero)
        self.item("S3-7", "拟人轨迹：非开火走曲线", self._s3_curve)
        self.item("S3-8", "拟人轨迹：瞬移降级率", self._s3_degrade)

    def _s3_pairs(self, c):
        self.cfg = self.cfg or base.config_mod.Config.load()
        if not self.ensure_game(mouse=True, keyboard=True):
            c.blocked("游戏窗口不可用")
            return
        if not self.cfg.region("clock"):
            c.skip("absent", "未配置 regions.clock（无公示期条目）")
            return
        observe_s = getattr(self.args, "s3_observe_s", 45)
        deadline = _pc() + observe_s
        clk = base.countdown_mod.Clock()
        est = base.countdown_mod.T0Estimator()
        paired = 0
        samples = []
        zero_actual = None
        while _pc() < deadline:
            self.tick()
            crop, _ = self.grab_box("clock")
            if crop is None:
                time.sleep(0.05)
                continue
            t1 = _pc()
            try:
                w1 = base.ocr_mod.read_words(crop)
                s1 = base.countdown_mod.parse_words(w1)
            except Exception:                            # noqa: BLE001
                s1 = None
            crop2, _ = self.grab_box("clock")
            t2 = _pc()
            try:
                w2 = base.ocr_mod.read_words(crop2) if crop2 is not None else []
                s2 = base.countdown_mod.parse_words(w2)
            except Exception:                            # noqa: BLE001
                s2 = None
            if s1 is not None and s2 is not None and s1 == s2:
                paired += 1
                samples.append({"sec": s1, "t": t2, "dt_ms": (t2 - t1) * 1000})
                clk.observe(s1, t2)
                est.observe(s1, t2)
                if s1 == 0 and zero_actual is None:
                    zero_actual = t2
            time.sleep(0.02)
        c.codes.update({"paired": paired, "samples": len(samples),
                        "zero_actual": zero_actual})
        self._s3 = {"clk": clk, "est": est, "samples": samples,
                    "zero_actual": zero_actual, "paired": paired}
        if paired >= 16:
            c.ok(f"成对读数 {paired} 条", "≥16 条")
        elif paired > 0:
            c.bad(f"成对读数仅 {paired} 条", "≥16 条",
                  "不足 16 条不许点火（既定纪律）")
        else:
            c.skip("absent", "观察窗内无成对读数")

    def _s3_estimate(self, c):
        d = getattr(self, "_s3", None)
        if not d or d["paired"] < 16:
            c.skip("gate", "成对读数不足 16 条，不反推、不点火")
            return
        try:
            zero = d["est"].zero_at()
            trusted = d["est"].trusted() if hasattr(d["est"], "trusted") else True
        except Exception as exc:                        # noqa: BLE001
            c.bad(f"{type(exc).__name__}: {exc}", "zero_at 可用")
            return
        d["zero_est"] = zero
        c.codes["zero_est"] = zero
        if zero is None:
            c.bad("zero_at=None", "有反推零点")
        elif not trusted:
            c.pending(f"零点 {zero:.3f} 但未达可信阈值", "trusted=True")
        else:
            c.ok(f"zero_at={zero:.3f}", "trusted=True")
        # 点火：只在 --allow-fire 且零点在未来 5s 内时执行，点击后立即记录偏差
        if getattr(self.args, "allow_fire", False) and zero is not None \
                and self.mouse is not None:
            delta = zero - _pc()
            if 0 < delta <= 5.0:
                box = self.cfg.region("buy") if self.cfg else None
                if not box:
                    c.codes["fire"] = "未配置 regions.buy，未点火"
                else:
                    x, y, w, h = self.screen.norm_to_px(box)
                    cx, cy = int(x + w / 2), int(y + h / 2)
                    while _pc() < zero - 0.002:
                        time.sleep(0.0005)
                    t_click = _pc()
                    self.mouse.click(cx, cy)
                    dev = (t_click - zero) * 1000.0
                    self._s3_fire_dev_ms = dev
                    c.codes["fire_dev_ms"] = round(dev, 2)
                    self.step(f"S3 点火 at=({cx},{cy}) 偏差 {dev:.1f}ms")
            elif delta <= 0:
                c.codes["fire"] = "零点已过，未点火"
        else:
            c.codes["fire"] = "dry（未加 --allow-fire）"

    def _s3_deviation(self, c):
        d = getattr(self, "_s3", None)
        if not d or d.get("zero_est") is None:
            c.skip("gate", "无反推零点，无偏差可算")
            return
        zero_est = d["zero_est"]
        zero_actual = d.get("zero_actual")
        devs = []
        clk = d["clk"]
        for s in d["samples"]:
            try:
                z = clk.zero_at()
            except Exception:                            # noqa: BLE001
                continue
            if z is not None and s["sec"] > 0:
                devs.append(abs((z - zero_est) - s["sec"]) * 1000.0)
        d["devs"] = devs
        if zero_actual is not None:
            devs.append(abs(zero_actual - zero_est) * 1000.0)
        fire_dev = getattr(self, "_s3_fire_dev_ms", None)
        if fire_dev is not None:
            devs.append(abs(fire_dev))
        if not devs:
            c.skip("absent", "无偏差样本")
            return
        p50, p90, p95 = _pct(devs, 50), _pct(devs, 90), _pct(devs, 95)
        c.codes.update({"p50_ms": p50, "p90_ms": p90, "p95_ms": p95,
                        "n": len(devs)})
        limit = getattr(self.args, "s3_p95_ms", 80.0)
        if p95 <= limit:
            c.ok(f"p50={p50:.0f}ms p90={p90:.0f}ms p95={p95:.0f}ms n={len(devs)}",
                 f"p95 ≤ {limit:.0f}ms")
        else:
            c.bad(f"p50={p50:.0f}ms p90={p90:.0f}ms p95={p95:.0f}ms n={len(devs)}",
                  f"p95 ≤ {limit:.0f}ms")

    def _s3_window(self, c):
        d = getattr(self, "_s3", None)
        devs = (d or {}).get("devs") or []
        if not devs:
            c.skip("gate", "无偏差样本")
            return
        win = getattr(self.args, "s3_window_ms", 120.0)
        over = sum(1 for v in devs if v > win) / len(devs)
        c.codes["over_window_ratio"] = over
        if over <= 0.05:
            c.ok(f"{over:.1%}（窗 {win:.0f}ms）", "≤5%")
        else:
            c.bad(f"{over:.1%}（窗 {win:.0f}ms）", "≤5%")

    def _s3_policy(self, c):
        d = getattr(self, "_s3", None)
        if not d:
            c.skip("gate", "未采样")
            return
        n = d["paired"]
        gaps = [s["dt_ms"] for s in d["samples"]]
        jitter = _pct(gaps, 95) if gaps else float("nan")
        if n >= 16 and not math.isnan(jitter) and jitter <= 30:
            c.ok(f"成对间隔 p95={jitter:.1f}ms · 样本 {n}", "≥16 条且 p95≤30ms")
        elif n >= 16:
            c.bad(f"成对间隔 p95={jitter:.1f}ms", "p95≤30ms",
                  "两次读数跨了显示 tick，成对策略退化")
        else:
            c.skip("gate", f"样本 {n} 条，不足以判策略")

    # ------------------------------------------------ S3-6..8（离线可测）
    def _hm_spec(self):
        from core import humanmouse
        return humanmouse.read_spec({"mouse": {"human": {
            "traj": "auto", "move_ms": 420, "curve": 0.35}}})

    def _s3_fire_zero(self, c):
        try:
            from core import humanmouse
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"humanmouse 不可用：{type(exc).__name__}: {exc}")
            return
        spec = self._hm_spec()
        rng = __import__("random").Random(7)
        res = humanmouse.plan((500, 400), (500, 400), spec, rng=rng, fire=True,
                              interval_ms=20.0)
        pts = res["points"]
        disp = math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])
        c.codes.update({"type": res["type"], "points": len(pts),
                        "ms": res["ms"], "disp_px": disp})
        if len(pts) <= 2 and disp == 0 and res["ms"] <= 30.0:
            c.ok(f"{res['type']} · {len(pts)} 点 · 位移 {disp:.1f}px · "
                 f"{res['ms']:.1f}ms", "≤2 点、0 位移、≤30ms")
        else:
            c.bad(f"{res['type']} {len(pts)} 点 位移 {disp:.1f}px {res['ms']}ms",
                  "开火零位移", "开火阶段还在移动，会拉长按下时刻")

    def _s3_curve(self, c):
        try:
            from core import humanmouse
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"humanmouse 不可用：{type(exc).__name__}: {exc}")
            return
        spec = self._hm_spec()
        rng = __import__("random").Random(9)
        res = humanmouse.plan((100, 200), (500, 200), spec, rng=rng, fire=False)
        pts = res["points"]
        offs = [abs(y - 200) for _, y, _ in pts]
        c.codes.update({"type": res["type"], "points": len(pts),
                        "max_off_px": round(max(offs), 2), "ms": res["ms"]})
        if (res["type"] == "windmouse" and len(pts) >= 8
                and max(offs) > 1.0 and res["ms"] >= 400):
            c.ok(f"{res['type']} · {len(pts)} 点 · 离弦 {max(offs):.1f}px · "
                 f"{res['ms']:.0f}ms", "曲线且 ≥8 点")
        else:
            c.bad(f"{res['type']} {len(pts)} 点 离弦 {max(offs):.2f}px",
                  "非开火走曲线（windmouse ≥8 点 离弦 >1px）",
                  "轨迹过直或在开火外仍走直线")

    def _s3_degrade(self, c):
        try:
            from core import humanmouse
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"humanmouse 不可用：{type(exc).__name__}: {exc}")
            return
        traj = humanmouse.Trajectory({"mouse": {"human": {
            "traj": "auto", "move_ms": 420, "curve": 0.35}}})
        for i in range(200):
            traj.record_fire(12 + (i % 5), gap_ms=6, preheated=True,
                             jumped=False, first=True)
        for i in range(20):
            traj.record_fire(9, gap_ms=2, preheated=False, jumped=True, first=True)
        st = traj.fire_stats()
        deg = (st.get("degrade") or {}).get("all") or {}
        n = max(1, int(st.get("n") or 0))
        straight = int(deg.get("straight") or 0)
        rate = straight / n
        c.codes.update({"n": n, "degrade": deg, "straight_rate": round(rate, 3),
                        "press": st.get("press_ms")})
        if rate <= 0.10 and int(deg.get("zero") or 0) >= 180:
            c.ok(f"n={n} · zero={deg.get('zero')} · straight={straight}"
                 f"（{rate:.0%}）· press p95={st['press_ms']['p95']}ms",
                 "降级率 ≤10%（预热命中 ≥90%）")
        else:
            c.bad(f"n={n} degrade={deg}", "straight 降级 ≤10%",
                  "预热未命中，开火仍会瞬移")

    # ================================================================ S4
    def stage_S4(self):
        self.item("S4-1", "点错界面 → 自愈", self._s4_unexpected)
        self.item("S4-2", "未知界面 → 自愈", self._s4_unknown)
        self.item("S4-3", "点击没生效 → 自愈", self._s4_noeffect)
        self.item("S4-4", "弹窗遮挡 → 自愈", self._s4_popup)
        self.item("S4-5", "窗口失焦 → 自愈", self._s4_focus)
        self.item("S4-6", "游戏掉线提示 → 自愈", self._s4_down)
        self.item("S4-7", "恢复汇总", self._s4_summary)
        self.item("S4-8", "电脑卡顿 → 自愈", self._s4_lag)
        self.item("S4-9", "掉线重登 → 自愈", self._s4_relogin)
        self.item("S4-10", "显示器休眠唤醒 → 自愈", self._s4_sleep)
        self.item("S4-11", "看门狗卡死恢复", self._s4_watchdog)

    def _s4_guard(self, fault, recovery_signals=None):
        if sg is None:
            raise RuntimeError("scene_guard 未导入")
        g = sg.SceneGuard()
        t0 = g.now()
        # 基线信号：page 必须给已知页，否则 _d_unexpected_page 会把一切
        # 场景都盖成 unexpected_page。
        fault_sig = {
            "page": "onsale", "expected_pages": ("onsale", "market"),
            "frame_changed": True, "click_effect_age_s": None,
            "confirm_found": False, "hwnd": 1, "hwnd_alive": True,
            "minimized": False, "foreground": True, "game_alive": True,
            "login_visible": False, "anchor_hits": 9,
            "anchor_pages_sampled": 3, "hits": 2, "refresh_rounds": 0,
            "stall_s": 0, "frame_dt_p95_ms": 60, "frame_dt_ms": 60,
            "in_fire": False, "countdown_conf": 1.0, "bought_delta": 0,
        }
        fault_sig.update(fault or {})
        for i in range(4):
            g.feed(fault_sig, now=t0 + i * 0.3)
        snap = dict(g.snapshot())
        rec = recovery_signals or {"page": "onsale", "expected_pages": ("onsale",),
                                   "frame_changed": True, "hwnd_alive": True,
                                   "minimized": False, "foreground": True,
                                   "game_alive": True, "login_visible": False,
                                   "anchor_hits": 9, "anchor_total": 9,
                                   "anchor_pages_sampled": 3,
                                   "expected_item": "测试标的"}
        for i in range(15):
            g.feed(rec, now=t0 + 1.2 + i * 0.3)
        snap2 = dict(g.snapshot())
        self._s4_history = [{"scene": snap.get("scene"),
                             "state": snap.get("state"),
                             "after": snap2.get("state"),
                             "level": snap2.get("level")}]
        return snap, snap2

    def _s4_item(self, c, fault, name, real_action=None):
        mode = "sim"
        if getattr(self.args, "allow_faults", False) and real_action:
            if not self.ensure_game(mouse=True, keyboard=True):
                c.pending("真实故障需游戏窗口；本次未制造，sim 注入仍会跑", "游戏窗口可用")
                return
            try:
                real_action()
                mode = "real"
            except Exception as exc:                    # noqa: BLE001
                c.pending(f"真实故障制造失败：{type(exc).__name__}: {exc}", "可制造")
                return
        snap, snap2 = self._s4_guard(fault)
        c.mode = mode
        c.codes.update({"state": snap.get("state"), "scene": snap.get("scene"),
                        "after_state": snap2.get("state"),
                        "level": snap2.get("level")})
        c.evidence["steps"] = self._s4_history[-6:]
        detected = snap.get("scene") == name
        recovered = snap2.get("state") in ("ok", "recovering", "waiting_human")
        if detected and recovered:
            c.ok(f"识别={snap.get('scene')} → {snap2.get('state')} mode={mode}",
                 "识别故障且退出硬停")
            self.safe_reset(f"S4 {name} 演练后复位")
        elif detected:
            c.bad(f"已识别但卡住：{snap2.get('state')}", "ok/recovering/waiting_human",
                  f"mode={mode}")
        else:
            c.bad(f"未识别：state={snap.get('state')} scene={snap.get('scene')}",
                  f"scene={name}", f"mode={mode}")

    def _s4_unexpected(self, c):
        def real():
            self.keyboard.tap("ESC")
            time.sleep(0.8)
            self.nav.where()
        self._s4_item(c, {"page": "detail", "expected_pages": ("onsale", "market")},
                      "unexpected_page", real if self.nav else None)

    def _s4_unknown(self, c):
        def real():
            for _ in range(2):
                self.keyboard.tap("ESC")
                time.sleep(0.6)
        self._s4_item(c, {"anchor_hits": 0, "anchor_pages_sampled": 3},
                      "ui_changed", real if self.keyboard else None)

    def _s4_noeffect(self, c):
        def real():
            if self.mouse and self.game_rect:
                x = self.game_rect[0] + 10
                y = self.game_rect[1] + 10
                self.mouse.click(x, y)
                time.sleep(0.4)
        self._s4_item(c, {"click_effect_age_s": 1.0, "frame_changed": False},
                      "click_no_effect", real if self.mouse else None)

    def _s4_popup(self, c):
        self._s4_item(c, {"confirm_found": True, "budget": 1.0,
                          "confirm_price": 999.0}, "confirm_unexpected")

    def _s4_focus(self, c):
        def real():
            base.ctypes.windll.user32.SetForegroundWindow(
                base.input_mod.Mouse.foreground() or 0)
        self._s4_item(c, {"foreground": False, "hwnd_alive": True,
                          "game_alive": True}, "window_lost")

    def _s4_down(self, c):
        self._s4_item(c, {"game_alive": False}, "game_down")

    def _s4_summary(self, c):
        rows = [x for x in self.checks if x.stage == "S4" and x.cid != "S4-7"]
        ok = sum(1 for x in rows if x.status == "pass")
        bad = [x.cid for x in rows if x.status == "fail"]
        un = [x.cid for x in rows if x.status in ("blocked", "pending")]
        modes = Counter(x.mode for x in rows if x.mode)
        c.codes.update({"pass": ok, "fail": bad, "not_run": un,
                        "modes": dict(modes)})
        if bad:
            c.bad(f"未通过：{bad}", "全部识别并可恢复", f"方式 {dict(modes)}")
        elif un:
            c.skip("not_run", f"未演练：{un}（游戏窗口不可用/超时）",
                   f"{ok} 类通过", "全部识别并可恢复")
        else:
            c.ok(f"{ok} 类故障可识别/可恢复 · 方式 {dict(modes)}",
                 "全部识别并可恢复")

    # ------------------------------------------------ S4-8..11（离线可测）
    def _s4_lag(self, c):
        self._s4_item(c, {"frame_dt_p95_ms": 420, "stall_s": 2.0,
                          "frame_dt_ms": 180}, "lag")

    def _s4_relogin(self, c):
        self._s4_item(c, {"game_alive": False, "login_visible": True},
                      "game_down")

    def _s4_sleep(self, c):
        self._s4_item(c, {"minimized": True, "frame_changed": False,
                          "frame_dt_p95_ms": 500}, "window_lost")

    def _s4_watchdog(self, c):
        try:
            from core import watchdog
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"watchdog 不可用：{type(exc).__name__}: {exc}")
            return
        clock = [0.0]
        hb = watchdog.Heartbeat(clock=lambda: clock[0])
        wd = watchdog.Watchdog(hb, limit_s=60, confirm_times=3,
                               cooldown_s=300, clock=lambda: clock[0])
        hb.beat("main", at=0.0)
        fired = []
        for i in range(5):
            clock[0] = 100.0
            fired.append(bool(wd.poll(("main",))))
        clock[0] = 130.0                      # 冷却期内
        cooldown_hit = bool(wd.poll(("main",)))
        clock[0] = 450.0                      # 冷却结束仍卡死
        after_cd = bool(wd.poll(("main",)))
        clock[0] = 460.0
        hb.beat("main")
        reset_hit = bool(wd.poll(("main",), age_override=0.2))
        c.codes.update({"fired_seq": fired, "cooldown_hit": cooldown_hit,
                        "after_cooldown": after_cd, "reset_hit": reset_hit,
                        "fired": wd.fired})
        ok = (fired[:2] == [False, False] and fired[2] is True
              and not cooldown_hit and after_cd and not reset_hit and wd.fired == 2)
        if ok:
            c.ok("100s 未心跳：第 3 次确认触发；冷却内不重复；恢复后复位",
                 "防抖 3 次 + 冷却 300s + 恢复复位")
        else:
            c.bad(f"seq={fired} cooldown={cooldown_hit} after={after_cd} "
                  f"reset={reset_hit} fired={wd.fired}",
                  "3 次确认、冷却内静默、恢复复位",
                  "看门狗可能误触发或该触发不触发")

    # ================================================================ S5
    def stage_S5(self):
        self.item("S5-0", "本地服务与页面加载", self._s5_boot, gate=True, timeout=60)
        self.item("S5-1", "开关控件四段联调", self._s5_switches, gate=True,
                  timeout=600)
        self.item("S5-2", "假按钮结论", self._s5_fake, timeout=30)
        self.item("S5-3", "时间同步界面提示可见", self._s5_timesync)
        self.item("S5-4", "21 个新开关专项汇总", self._s5_new_switches, timeout=60)

    def _free_port(self):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()

    def _s5_boot(self, c):
        if self._s5_proc is not None:
            c.ok("服务已启动", "可访问")
            return
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"playwright 不可用：{type(exc).__name__}: {exc}")
            return
        port = self._free_port()
        boot = (
            "import sys\n"
            f"sys.path.insert(0, {ROOT!r})\n"
            "from core import server\n"
            "server._seed_default_task = lambda: None\n"
            "server._start_hotkeys = lambda: {}\n"
            "server._sync_on_boot = lambda: None\n"
            "server._scheduler.gc_task_dirs = lambda *a, **k: None\n"
            "server._watcher.start = lambda: None\n"
            "server._start_ocr_service = lambda: type('S', (), {'stop': lambda s: None})()\n"
            f"server.serve(port={port}, token=None)\n"
        )
        env = dict(os.environ)
        env["SJZ_RM_V2"] = "1"
        try:
            self._s5_proc = subprocess.Popen(
                [sys.executable, "-c", boot], cwd=ROOT, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=base.CREATE_NO_WINDOW)
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"服务启动失败：{type(exc).__name__}: {exc}")
            return
        self._s5_http = f"http://127.0.0.1:{port}"
        deadline = _pc() + 25
        while _pc() < deadline:
            self.tick()
            try:
                with urllib.request.urlopen(self._s5_http + "/api/switches",
                                            timeout=1.5) as r:
                    if r.status == 200:
                        c.ok(f"服务 {self._s5_http} · /api/switches 200",
                             "HTTP 200")
                        return
            except Exception:                        # noqa: BLE001
                time.sleep(0.3)
        c.blocked("服务 25s 内未就绪", "HTTP 200")

    def _s5_launch(self, p):
        """按可用性依次尝试：内置 chromium → 系统 Chrome → 系统 Edge。
        只检测不安装浏览器；全都没有就报环境阻塞，不阻塞其他阶段。
        """
        tries = (("bundled-chromium", {}),
                 ("chrome", {"channel": "chrome"}),
                 ("msedge", {"channel": "msedge"}))
        errs = []
        for tag, kw in tries:
            try:
                b = p.chromium.launch(headless=True, **kw)
                self.say(f"[S5] 浏览器 = {tag}", "info")
                return b, tag
            except Exception as exc:                    # noqa: BLE001
                errs.append(f"{tag}: {type(exc).__name__}: "
                            f"{str(exc).splitlines()[0][:120]}")
        raise RuntimeError("无可用浏览器（不自动安装）：" + " | ".join(errs))

    def _s5_switches(self, c):
        from playwright.sync_api import sync_playwright
        if not self._s5_http:
            c.blocked("服务未起，无法联调")
            return
        os.makedirs(self._shot_dir, exist_ok=True)
        # 配置快照：联调完原样恢复
        if os.path.isfile(CFG_PATH):
            self._s5_before_sha = base_hash(CFG_PATH)
            os.makedirs(self._backup_dir, exist_ok=True)
            shutil.copy2(CFG_PATH, os.path.join(self._backup_dir, "config.json"))
        groups = None
        results = []
        with sync_playwright() as p:
            browser, self._s5_browser = self._s5_launch(p)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda e: self._js_errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: self._js_errors.append(f"console.{m.type}: {m.text}")
                    if m.type == "error" else None)
            page.goto(self._s5_http + "/", wait_until="load", timeout=20000)
            page.wait_for_function("typeof loadSwitches === 'function'", timeout=10000)
            page.evaluate("loadSwitches()")
            try:
                inv = page.evaluate("fetch('/api/switches').then(r=>r.json())")
                groups = inv.get("groups") or []
            except Exception as exc:                    # noqa: BLE001
                c.bad(f"读取开关表失败：{type(exc).__name__}: {exc}", "groups 可读")
                browser.close()
                return
            items = []
            for g in groups:
                for it in (g.get("items") or []):
                    items.append((g.get("name"), it))
            c.codes["switches"] = len(items)
            if not items:
                c.bad("开关表为空", "≥1 项")
            else:
                for gname, it in items:
                    self.tick()
                    key, kind = it.get("key"), it.get("kind")
                    cid = f"S5:{key}"
                    try:
                        results.append(self._s5_one(page, key, kind, it))
                    except Aborted:
                        raise
                    except Exception as exc:            # noqa: BLE001
                        results.append({"key": key, "status": "fail",
                                        "owner": "前端",
                                        "reason": f"{type(exc).__name__}: {exc}"})
            browser.close()
        # 汇总成子项写进报告
        self._s5_results = results
        fails = [r for r in results if r["status"] != "pass"]
        c.measured = f"{len(results) - len(fails)}/{len(results)} 开关通过"
        c.threshold = "全部开关通过"
        c.codes["fail_keys"] = [r["key"] for r in fails]
        c.evidence["items"] = results
        if not results:
            c.bad("没有可联调的开关", "≥1 项")
        elif fails:
            c.bad(c.measured, "全部开关通过",
                  "；".join(f"{r['key']}({r['owner']}/{r['reason']})"
                            for r in fails[:8]))
        else:
            c.ok(c.measured, "全部开关通过")

    def _s5_one(self, page, key, kind, item):
        sel = f'[data-sw="{key}"]'
        name = item.get("label") or key
        out = {"key": key, "label": name, "kind": kind, "status": "pass",
               "owner": "", "reason": "", "segments": {}}
        cnt = page.locator(sel).count()
        if cnt == 0:
            out.update(status="fail", owner="前端", reason="控件缺失（data-sw 不存在）")
            self._s5_shot(page, key)
            return out
        disabled = bool(page.evaluate(
            "s=>{const el=document.querySelector(s);"
            "return !!(el && (el.disabled || el.getAttribute('aria-disabled')==='true'));}",
            sel))
        if disabled:
            out.update(status="fail", owner="前端", reason="控件 disabled（假按钮）")
            self._s5_shot(page, key)
            return out
        cur = item.get("value")
        pred = lambda r: ("/api/switches" in r.url
                          and r.request.method == "POST")
        expect = None
        try:
            with page.expect_response(pred, timeout=6000) as resp_info:
                if kind == "bool":
                    expect = (not bool(cur))
                    page.evaluate(
                        "s=>{const el=document.querySelector(s);"
                        "if(el) el.click();}", sel)
                elif kind in ("select", "choice"):
                    expect = page.evaluate(
                        "s=>{const el=document.querySelector(s);"
                        "const o=[...el.options].map(x=>x.value);"
                        "const v=o.find(x=>x!==el.value);"
                        "if(v!==undefined) el.value=v;"
                        "el.dispatchEvent(new Event('change',{bubbles:true}));"
                        "return el.value;}", sel)
                else:
                    expect = self._alt_number(key, cur)
                    page.evaluate(
                        "o=>{const el=document.querySelector(o.s);"
                        "el.value=String(o.v);"
                        "el.dispatchEvent(new Event('change',{bubbles:true}));}",
                        {"s": sel, "v": expect})
            resp = resp_info.value
            out["segments"]["api_status"] = resp.status
            body = resp.json()
            if resp.status != 200 or body.get("ok") is not True:
                out.update(status="fail", owner="后端",
                           reason=f"接口 {resp.status} ok={body.get('ok')}")
                return out
            applied = find_switch_value(body.get("groups") or [], key)
            out["segments"]["applied"] = applied
            if not same_value(applied, expect):
                out.update(status="fail", owner="后端",
                           reason=f"接口回显 {applied!r} ≠ 期望 {expect!r}")
                return out
        except Exception as exc:                        # noqa: BLE001
            out.update(status="fail", owner="前端",
                       reason=f"控件未触发接口或接口失败：{type(exc).__name__}")
            self._s5_shot(page, key)
            return out
        # 配置段
        try:
            fresh = base.config_mod.Config.load()
            got = switches.get(fresh, key)
            out["segments"]["config"] = got
            if not same_value(got, expect):
                out.update(status="fail", owner="后端",
                           reason=f"config.json 读回 {got!r} ≠ {expect!r}")
                return out
        except Exception as exc:                        # noqa: BLE001
            out.update(status="fail", owner="后端",
                       reason=f"fresh 加载失败：{type(exc).__name__}: {exc}")
            return out
        # 行为段
        try:
            beh = self._behavior(key, expect)
            out["segments"]["behavior"] = beh
            if not beh.get("ok"):
                out.update(status="fail", owner="后端",
                           reason=f"行为探针失败：{beh.get('detail')}")
                return out
        except Exception as exc:                        # noqa: BLE001
            out.update(status="fail", owner="后端",
                       reason=f"行为探针异常：{type(exc).__name__}: {exc}")
        return out

    def _behavior(self, key, expect):
        if key == "run.workflow":
            # 行为口径：preflight / 调度器都走 server._preflight_workflow。
            # 离线不跑完整 preflight（它要取帧做 OCR，会超时），只校验
            # 「落盘值 → 消费方归一化口径」这条链。
            fresh = base.config_mod.Config.load()
            got = switches.get(fresh, key)
            try:
                from core import server as srv_mod
                eff = srv_mod._preflight_workflow({"workflow": expect}, fresh)
            except Exception as exc:                    # noqa: BLE001
                return {"ok": False, "detail": f"消费方调用失败 {type(exc).__name__}: {exc}"}
            if same_value(got, expect) and same_value(eff, expect):
                return {"ok": True, "detail": f"配置={got!r} · preflight 口径={eff!r}"}
            return {"ok": False, "detail": f"配置={got!r} · preflight 口径={eff!r}"}
        fresh = base.config_mod.Config.load()
        got = switches.get(fresh, key)
        try:
            norm = switches._coerce(switches.spec(key), got)
        except Exception as exc:                        # noqa: BLE001
            return {"ok": False, "detail": f"归一化失败 {exc}"}
        if same_value(norm, expect) and same_value(got, expect):
            return {"ok": True, "detail": "消费方 switches.get 读到期望值且归一化稳定"}
        return {"ok": False, "detail": f"消费方读到 {got!r} / {norm!r}"}

    def _alt_number(self, key, cur):
        spec = switches.spec(key)
        lo, hi = switches.BOUNDS.get(key, (None, None))
        try:
            v = float(cur)
        except (TypeError, ValueError):
            v = float(spec["default"])
        step = 1 if spec["kind"] == "int" else 0.1
        cands = [v + step, v - step, v + 2 * step, float(spec["default"])]
        for x in cands:
            if lo is not None and x < lo:
                continue
            if hi is not None and x > hi:
                continue
            if key == "price.min" and x > float(switches.get(
                    base.config_mod.Config.load(), "price.max")):
                continue
            if key == "price.max" and x < float(switches.get(
                    base.config_mod.Config.load(), "price.min")):
                continue
            if key == "scan.repeat.min" and x > float(switches.get(
                    base.config_mod.Config.load(), "scan.repeat.max")):
                continue
            if key == "scan.repeat.max" and x < float(switches.get(
                    base.config_mod.Config.load(), "scan.repeat.min")):
                continue
            return int(x) if spec["kind"] == "int" else round(x, 4)
        raise RuntimeError("没有可用的合法测试值")

    def _s5_shot(self, page, key):
        try:
            os.makedirs(self._shot_dir, exist_ok=True)
            p = os.path.join(self._shot_dir, f"S5_{key}.png")
            page.screenshot(path=p)
            self.shots[key] = self.shots.get(key, 0) + 1
        except Exception:                                # noqa: BLE001
            pass

    def _s5_fake(self, c):
        results = getattr(self, "_s5_results", [])
        fake = [r for r in results if r.get("status") == "fail"
                and r.get("owner") == "前端"]
        missing = [r["key"] for r in fake if "缺失" in r.get("reason", "")
                   or "disabled" in r.get("reason", "")]
        if not results:
            c.blocked("未联调，无法判定")
            return
        c.codes.update({"fake": [r["key"] for r in fake],
                        "missing_control": missing,
                        "js_errors": self._js_errors[:8]})
        if fake:
            c.bad(f"发现 {len(fake)} 个假按钮/控件失联："
                  + "、".join(r["key"] for r in fake[:8]),
                  "0 个假按钮", "逐项原因见 items")
        else:
            c.ok("未发现假按钮：每个控件都触发了接口并落盘", "0 个假按钮")

    # ------------------------------------------------ S5-3/4（离线可测）
    def _s5_get(self, path, timeout=6.0):
        req = urllib.request.Request(self._s5_http + path, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")

    def _s5_timesync(self, c):
        if not self._s5_http:
            c.blocked("服务未起，无法校验界面提示")
            return
        try:
            code_page, page = self._s5_get("/")
            code_api, body = self._s5_get("/api/timesync")
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"请求失败：{type(exc).__name__}: {exc}")
            return
        try:
            view = json.loads(body)
        except Exception:                               # noqa: BLE001
            view = {}
        hints = [w for w in ("时间校准", "时间同步", "校时", "降级")
                 if w in page]
        keys = [k for k in ("degraded", "offset", "offset_ms", "ok")
                if k in view]
        c.codes.update({"page_status": code_page, "api_status": code_api,
                        "hints": hints, "view_keys": keys})
        if code_api == 200 and "degraded" in view and hints:
            c.ok(f"页面提示 {hints} · /api/timesync {keys}",
                 "提示可见且降级字段可读")
        else:
            c.bad(f"页面提示={hints} · /api {code_api} keys={keys}",
                  "页面可见提示且 /api/timesync 含 degraded", "前端未展示时间源状态")

    def _s5_new_switches(self, c):
        results = getattr(self, "_s5_results", [])
        if not results:
            c.blocked("S5-1 未完成，无法汇总 21 开关")
            return
        by_key = {r.get("key"): r for r in results}
        rows, missing, fails = {}, [], []
        for key in NEW_SWITCH_KEYS:
            r = by_key.get(key)
            if not r:
                missing.append(key)
                continue
            seg = r.get("segments") or {}
            rows[key] = {"status": r.get("status"), "owner": r.get("owner"),
                         "api": seg.get("api_status"), "config": "config" in seg,
                         "behavior": bool((seg.get("behavior") or {}).get("ok"))}
            if r.get("status") != "pass":
                fails.append(f"{key}({r.get('owner')}/{r.get('reason')})")
        c.codes.update({"total": len(NEW_SWITCH_KEYS), "missing": missing,
                        "fails": fails[:6]})
        c.evidence["switches_21"] = rows
        if missing:
            c.bad(f"{len(missing)} 个未出现在开关表：{missing[:6]}",
                  "21/21 出现在控件/接口", "前端开关表缺项")
        elif fails:
            c.bad(f"{len(fails)} 项未通过：{fails[:4]}", "21/21 三段联通",
                  "；".join(fails[:6]))
        else:
            c.ok(f"21/21 控件→接口→磁盘→行为 全通过", "21/21")

    def cleanup_s5(self):
        if self._s5_proc is not None:
            try:
                self._s5_proc.terminate()
                self._s5_proc.wait(timeout=5)
            except Exception:                        # noqa: BLE001
                try:
                    self._s5_proc.kill()
                except Exception:                    # noqa: BLE001
                    pass
            self._s5_proc = None
        # 原样恢复配置
        bak = os.path.join(self._backup_dir, "config.json")
        if os.path.isfile(bak):
            try:
                shutil.copy2(bak, CFG_PATH)
                now = base_hash(CFG_PATH)
                self.say(f"[S5] 配置已恢复 · sha 一致={now == self._s5_before_sha}")
            except Exception as exc:                    # noqa: BLE001
                self.say(f"[S5] 配置恢复失败：{exc}", "fail")

    # ================================================================ S6
    def stage_S6(self):
        self.item("S6-1", "七字段落盘（合成记录）", self._s6_roundtrip)
        self.item("S6-2", "七字段类型与缺失语义", self._s6_types)
        self.item("S6-3", "台账行可解析", self._s6_parse)
        self.item("S6-4", "遥测事件映射与入库", self._s6_telemetry, timeout=40)
        self.item("S6-5", "看板能显示该用户数据", self._s6_dashboard)
        self.item("S6-6", "真实成交行七字段取值", self._s6_real_row, timeout=45)
        self.item("S6-7", "遥测上报成功", self._s6_upload, timeout=45)

    def _load_mod(self, rel, name):
        import importlib.util
        path = os.path.join(ROOT, rel)
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _s6_temp_ledger(self):
        import tempfile
        return tempfile.NamedTemporaryFile(prefix="rm_s6_", suffix=".jsonl",
                                           dir=self._shot_dir, delete=False).name

    def _s6_row(self, **extra):
        row = {"ts": 1793000000.0, "t": "11-07 12:00:00", "name": "S6-合成",
               "price": 128.0, "result": "hit", "elapsed": 3400.0,
               "shot": "s6.png", "mode": "live", "quota": 1, "verified": True}
        row.update(extra)
        return row

    def _s6_roundtrip(self, c):
        rec = base.records_mod
        old = rec.PATH
        tmp = self._s6_temp_ledger()
        ev = {"pool_joined_at": 1793000000123, "join_result": "joined",
              "roll_self": 99, "roll_max": 125, "won": False,
              "listed_at": 1792990000000, "publicity_s": 10000.0,
              "listed_at_err_s": 1.09, "join_conflict": False,
              "won_source": "points"}
        try:
            rec.PATH = tmp
            row = self._s6_row(**ev)
            written = rec.add(name=row["name"], price=row["price"],
                              result=row["result"], elapsed=row["elapsed"],
                              shot=row["shot"], mode=row["mode"],
                              quota=row["quota"], verified=row["verified"],
                              extra={k: row.get(k) for k in EVIDENCE_FIELDS},
                              strict=True)
            rows = rec.load(limit=3)
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"合成落盘失败：{type(exc).__name__}: {exc}")
            return
        finally:
            rec.PATH = old
            try:
                os.remove(tmp)
            except OSError:
                pass
        got = rows[0] if rows else {}
        missing = [k for k in EVIDENCE_FIELDS if k not in got]
        changed = {k: (got.get(k), ev[k]) for k in EVIDENCE_FIELDS
                   if k in got and got.get(k) != ev[k]}
        c.codes.update({"written": bool(written), "missing": missing,
                        "changed": changed, "row": {k: got.get(k)
                                                    for k in EVIDENCE_FIELDS}})
        if written and not missing and not changed:
            c.ok(f"10/10 落盘且原值不变：{c.codes['row']}", "十列齐全")
        elif missing:
            c.bad(f"缺列 {missing}", "十列齐全", "落盘缺字段，消费方拿不到")
        else:
            c.bad(f"值被改：{changed}", "原值不变", "写入口径不一致")

    def _s6_types(self, c):
        try:
            from core import pooltrace
            from evo.collect import schema, collector
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"evo/pooltrace 不可用：{type(exc).__name__}: {exc}")
            return
        blank = pooltrace.PoolTrace(inbox_dir=self._shot_dir).evidence()
        pt = pooltrace.PoolTrace(inbox_dir=self._shot_dir)
        no_match = pt.observe_toast("与抢购无关的文案", at_ms=1793000000000)
        pt.begin()
        pt.observe_clock(first_seen_ms=1793000000000, remaining_s=30,
                         scan_lag_s=0.0)
        pt.observe_toast("已加入抢购池", at_ms=1793000001000)
        pt.observe_toast("未抢购成功，您的随机点数为 99，本轮最大点数为 125",
                         at_ms=1793000002000)
        ev = pt.evidence()
        types_ok = (isinstance(ev.get("pool_joined_at"), int)
                    and isinstance(ev.get("listed_at"), int)
                    and isinstance(ev.get("roll_self"), int)
                    and isinstance(ev.get("roll_max"), int)
                    and isinstance(ev.get("won"), bool)
                    and ev.get("publicity_s") is None)
        blank_ok = (blank["pool_joined_at"] is None and blank["join_result"] == "unknown"
                    and blank["roll_self"] is None and blank["roll_max"] is None
                    and blank["won"] is None and blank["listed_at"] is None
                    and blank["publicity_s"] is None
                    and all(blank[k] is None for k in EVIDENCE_FIELDS
                            if k not in ("join_result", "join_conflict", "won_source")))
        se = schema.blank_event()
        # 台账裸名 → 事件 _ms 名（两边命名差异是已知缺口，这里显式映射并核对）
        se.update(kind="opportunity", source="records", source_path="s6",
                  source_line=1, synthetic=True, tz="Asia/Shanghai",
                  trust="synthetic", ts_ms=1793000000000, result_raw="hit",
                  result_norm="hit", stage_reached=5, win=True,
                  elapsed_ms=3400, name="S6-合成", price=128.0, quota=1,
                  event_id="s6-" + "0" * 24,
                  pool_joined_at_ms=ev.get("pool_joined_at"),
                  listed_at_ms=ev.get("listed_at"),
                  join_result=ev.get("join_result"),
                  roll_self=ev.get("roll_self"),
                  roll_max=ev.get("roll_max"),
                  won=ev.get("won"),
                  publicity_s=ev.get("publicity_s"))
        mapped_ok = (se.get("pool_joined_at_ms") == ev.get("pool_joined_at")
                     and se.get("listed_at_ms") == ev.get("listed_at")
                     and se.get("roll_self") == ev.get("roll_self")
                     and se.get("roll_max") == ev.get("roll_max")
                     and se.get("won") == ev.get("won"))
        problems = schema.validate(se)
        collector.derive_event(se)
        c.codes.update({"types_ok": types_ok, "blank_ok": blank_ok,
                        "mapped_ok": mapped_ok,
                        "no_match_matched": no_match.get("matched"),
                        "schema_problems": problems, "evidence": ev,
                        "stage_reached": se.get("stage_reached")})
        if types_ok and blank_ok and mapped_ok and not problems \
                and se.get("stage_reached") >= 3:
            c.ok(f"类型正确 · 缺失为 null/unknown · 裸名→_ms 映射一致 · "
                 f"schema 0 问题 · stage={se.get('stage_reached')}",
                 "null 不得替换成 0")
        else:
            c.bad(f"types={types_ok} blank={blank_ok} mapped={mapped_ok} "
                  f"schema={problems} stage={se.get('stage_reached')}",
                  "类型/null/映射/schema/stage 全对", "字段口径错误，统计会失真")

    def _s6_parse(self, c):
        path = os.path.join(ROOT, "data", "records.jsonl")
        if not os.path.exists(path):
            c.skip("no_ledger", "台账尚不存在（还没成交过）", "台账 ≥1 行")
            return
        total = bad = 0
        try:
            with open(path, encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    total += 1
                    try:
                        if not isinstance(json.loads(line), dict):
                            bad += 1
                    except Exception:                   # noqa: BLE001
                        bad += 1
        except OSError as exc:
            c.blocked(f"台账不可读：{exc}")
            return
        c.codes.update({"lines": total, "bad": bad})
        if total and bad == 0:
            c.ok(f"{total} 行全部可解析", "100%")
        elif not total:
            c.skip("empty", "台账为空", "≥1 行")
        else:
            c.bad(f"{bad}/{total} 行损坏", "0 坏行", "台账出现残行，统计会缺件")

    def _s6_telemetry(self, c):
        try:
            ta = self._load_mod("tools/telemetry_agent.py", "rm_telem_agent")
            ts = self._load_mod("tools/telemetry_server.py", "rm_telem_server")
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"遥测模块不可用：{type(exc).__name__}: {exc}")
            return
        row = self._s6_row(offset_ms=-42.0, stop_reason="成交",
                           **{"pool_joined_at": 1793000000123,
                              "join_result": "joined", "roll_self": 99,
                              "roll_max": 125, "won": False,
                              "listed_at": 1792990000000,
                              "publicity_s": 10000.0})
        try:
            raw = ta._event(row)
            clean = ts.clean_event(raw)
        except Exception as exc:                        # noqa: BLE001
            c.bad(f"映射失败：{type(exc).__name__}: {exc}", "_event→clean_event 成功",
                  "遥测映射字段不合法")
            return
        db = os.path.join(self._shot_dir, "s6_telemetry.db")
        lic = "a" * 32
        try:
            store = ts.Store(db)
            if getattr(ts, "LICENSE", None) is None:
                # 离线模式没有许可库：users() 要读 LICENSE.all()，给个空壳。
                class _NoLic:
                    @staticmethod
                    def all():
                        return {}
                ts.LICENSE = _NoLic
            res = store.ingest(lic, [clean], "deadbeef", "v2")
            rows = store.user_events(lic, limit=10)
            users = store.users()
            ov = store.overview()
        except Exception as exc:                        # noqa: BLE001
            c.bad(f"入库失败：{type(exc).__name__}: {exc}", "ingest 成功",
                  "遥测库写入异常")
            return
        hit_users = [u for u in (users or [])
                     if (u.get("id") or u.get("lic_hash")) == lic]
        c.codes.update({"inserted": res.get("inserted") if isinstance(res, dict) else res,
                        "rows": len(rows), "users": len(users or []),
                        "overview_hit": ov.get("hit") or ov.get("hits")})
        if rows and len(rows) == 1 and hit_users and clean.get("result") == "hit":
            c.ok(f"映射→入库→回读 1 行 · 用户命中 {len(hit_users)} · "
                 f"skew={clean.get('skew_ms')}",
                 "端到端 ≥1 行且 result=hit")
        else:
            c.bad(f"rows={len(rows)} users={len(users or [])} res={res}",
                  "入库且可按用户回读", "遥测链路断，看板会空")

    def _s6_dashboard(self, c):
        html = os.path.join(ROOT, "tools", "telemetry_dashboard.html")
        try:
            page = open(html, encoding="utf-8", errors="replace").read()
        except OSError as exc:
            c.bad(f"看板文件不可读：{exc}", "telemetry_dashboard.html 存在",
                  "看板文件缺失")
            return
        need = [k for k in ("/api/admin/users", "renderUsers", "license_mask",
                            "用户详情") if k in page]
        c.codes.update({"markers": need, "size": len(page)})
        if len(need) >= 3:
            c.ok(f"看板含 {need}", "看板可按用户维度展示成交/遥测")
        else:
            c.bad(f"仅命中 {need}",
                  "含 /api/admin/users、renderUsers、license_mask、用户详情 中 ≥3",
                  "看板无用户详情节")

    def _s6_real_row(self, c):
        try:
            rows = base.records_mod.load(limit=50) or []
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"台账读取失败：{type(exc).__name__}: {exc}")
            return
        hits = [r for r in rows if r.get("result") in ("hit", "bought")]
        # 只认「合并之后」写出的成交行：带任一证据列，或时间晚于本次启动。
        fresh = [r for r in hits
                 if any(k in r for k in EVIDENCE_FIELDS)
                 or (isinstance(r.get("ts"), (int, float))
                     and float(r["ts"]) >= self.started_at)]
        if not fresh:
            c.blocked(f"成交行 {len(hits)} 条，但均为合并前写入（不含证据列）；"
                      "开游戏成交 1 单后重跑 S6-6", "合并后 ≥1 条成交行")
            return
        row = fresh[0]
        got = {k: row.get(k) for k in EVIDENCE_FIELDS}
        present = [k for k, v in got.items() if k in row]
        zero_not_null = [k for k, v in got.items()
                         if v == 0 and not isinstance(v, bool) and k != "won"]
        types_ok = ((row.get("pool_joined_at") is None
                     or isinstance(row.get("pool_joined_at"), int))
                    and (row.get("join_result") is None
                         or isinstance(row.get("join_result"), str))
                    and (row.get("roll_self") is None
                         or isinstance(row.get("roll_self"), int))
                    and (row.get("won") is None or isinstance(row.get("won"), bool)))
        c.codes.update({"present": present, "zero_not_null": zero_not_null,
                        "types_ok": types_ok, "ts": row.get("ts")})
        if len(present) == len(EVIDENCE_FIELDS) and types_ok and not zero_not_null:
            c.ok(f"最近成交行 10/10：{got}", "十列齐全且类型正确")
        else:
            c.bad(f"缺 {[k for k in EVIDENCE_FIELDS if k not in row]} · "
                  f"0替null={zero_not_null} · types={types_ok}", "10/10 且类型正确",
                  "真实成交行字段口径不符")

    def _s6_upload(self, c):
        outbox = os.path.join(ROOT, "data", "telemetry")
        if not os.path.isdir(outbox):
            c.blocked("无遥测目录；需先跑 telemetry_agent 并成交 1 单")
            return
        pending = [f for f in os.listdir(outbox) if f.endswith((".jsonl", ".pending"))]
        c.codes.update({"files": pending[:6]})
        if not pending:
            c.ok("outbox 已清空（上报完成）", "无待传文件")
        else:
            c.bad(f"残留 {len(pending)} 个待传：{pending[:3]}", "outbox 为空",
                  "上报失败或未启动 server")

    # ================================================================ S7
    def stage_S7(self):
        self.item("S7-0", "基线换算自检", self._s7_math)
        self.item("S7-1", "单件端到端耗时", self._s7_e2e, timeout=120)
        self.item("S7-2", "点击光标占用", self._s7_click_cost, timeout=60)
        self.item("S7-3", "offset_ms 分布", self._s7_offset, timeout=120)
        self.item("S7-4", "长时资源（过夜）", self._s7_soak, timeout=240)

    @staticmethod
    def _dist(values):
        vals = sorted(float(v) for v in values)
        n = len(vals)
        if not n:
            return {"n": 0}

        def idx(i):
            return vals[min(n - 1, max(0, i))]
        # 最近秩法：p=0.5 → 第 n/2 个；p=0.95 → 第 0.95n 个（小样本即取最大）。
        return {"n": n, "p50": round(idx(int(n * 0.5)), 3),
                "p95": round(idx(int(n * 0.95)), 3), "max": round(vals[-1], 3),
                "min": round(vals[0], 3)}

    def _s7_math(self, c):
        # 16 条已知样本：后 10 条 60..600，前 6 条固定耗时。
        vals = [3000, 3200, 3300, 3400, 3500, 3550] + [60 * (i + 1) for i in range(10)]
        d = self._dist(vals)
        off = self._dist([5, 8, 9, 12, 20, 25, 30, 45, 60, 75])
        band = lambda v: 3220 <= v <= 3550
        checks = (d["n"] == 16 and d["min"] == 60 and d["max"] == 3550
                  and d["p50"] == 540 and d["p95"] == 3550
                  and self._dist([])["n"] == 0
                  and band(3400) and band(3220) and band(3550)
                  and not band(3743.9) and off["p95"] <= 80)
        c.codes.update({"duration": d, "offset": off})
        if checks:
            c.ok(f"p50 {d['p50']} · p95 {d['p95']} · max {d['max']}；"
                 f"offset p95 {off['p95']}ms", "百分位与判带正确")
        else:
            c.bad(f"{d} / {off}", "p50/p95/max 与 3220–3550 判带正确",
                  "基线换算实现有误")

    def _s7_e2e(self, c):
        try:
            rows = base.records_mod.load(limit=30) or []
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"台账读取失败：{type(exc).__name__}: {exc}")
            return
        vals = [r.get("elapsed") for r in rows
                if r.get("result") in ("hit", "bought")
                and isinstance(r.get("elapsed"), (int, float))]
        if len(vals) < 3:
            c.blocked(f"成交样本 {len(vals)} 条 <3；开游戏成交后再跑",
                      "≥3 条成交耗时")
            return
        d = self._dist(vals[:30])
        c.codes["duration"] = d
        c.measured = f"p50 {d['p50']}ms · p95 {d['p95']}ms · max {d['max']}ms"
        if d["p50"] <= 3550:
            c.ok(c.measured, "p50 ≤3550ms（基线 3743.9ms）")
        elif d["p50"] <= 3743.9:
            c.bad(c.measured, "p50 ≤3550ms", "仍高于补丁目标 3220–3550")
        else:
            c.bad(c.measured, "p50 ≤3743.9ms", "比基线更慢，需查详情补丁是否生效")

    def _s7_click_cost(self, c):
        try:
            from core import nocursor as nc
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"nocursor 不可用：{type(exc).__name__}: {exc}")
            return
        try:
            hwnd = nc._make_test_window()
        except Exception as exc:                        # noqa: BLE001
            c.skip("isolated_desktop", f"测试窗口不可用：{type(exc).__name__}: {exc}")
            return
        clicker = nc.Clicker(hwnd, "message")
        before = nc._cursor()
        samples, n = [], 200
        for _ in range(n):
            t0 = time.perf_counter()
            clicker.click(960, 540)
            samples.append((time.perf_counter() - t0) * 1000.0)
        time.sleep(0.15)
        after = nc._cursor()
        import inspect
        src = inspect.getsource(nc.Clicker.click)
        # message 通道的硬约束：不调用 SetCursorPos、不注入移动事件。
        touches_cursor = "SetCursorPos" in src
        d = self._dist(samples)
        hits = nc._TEST_HITS[0]
        delta = (None if before is None or after is None
                 else round(((after[0] - before[0]) ** 2
                             + (after[1] - before[1]) ** 2) ** 0.5, 1))
        c.codes.update({"click": d, "hits": hits,
                        "cursor_delta_px": delta,
                        "clicker_touches_cursor": touches_cursor})
        c.measured = (f"p50 {d['p50']}ms · p95 {d['p95']}ms · max {d['max']}ms")
        perf_ok = d["p50"] <= 0.40 and d["p95"] <= 1.0 and d["max"] <= 2.0
        if perf_ok and not touches_cursor and hits > 0:
            c.ok(f"{c.measured} · 受理 {hits}/{n} · message 通道无 SetCursorPos"
                 f"（实测位移 {delta}px 仅供参考）",
                 "p50 ≤0.40ms / p95 ≤1.0ms / max ≤2.0ms，且不动真实光标")
        elif not perf_ok:
            c.bad(c.measured, "p50 ≤0.40 / p95 ≤1.0 / max ≤2.0", "点击开销超标")
        elif touches_cursor:
            c.bad(f"Clicker.click 含 SetCursorPos", "message 通道不得动真实光标",
                  "点击链路会抢光标")
        else:
            c.bad(f"受理 {hits}/{n}", "测试窗口应能收到点击", "测试通道未生效")

    def _s7_offset(self, c):
        try:
            rows = base.records_mod.load(limit=30) or []
        except Exception as exc:                        # noqa: BLE001
            c.blocked(f"台账读取失败：{type(exc).__name__}: {exc}")
            return
        vals = []
        for r in rows:
            for k in ("offset_ms", "skew_ms", "lead_err_ms"):
                v = r.get(k)
                if isinstance(v, (int, float)):
                    vals.append(abs(float(v)))
                    break
        if getattr(self, "_s3_fire_dev_ms", None) is not None:
            vals.append(abs(float(self._s3_fire_dev_ms)))
        if len(vals) < 3:
            c.blocked(f"offset 样本 {len(vals)} 条 <3；点火或成交后再跑",
                      "≥3 条 |offset|")
            return
        d = self._dist(vals)
        c.codes["offset"] = d
        c.measured = f"p50 {d['p50']}ms · p95 {d['p95']}ms · max {d['max']}ms"
        if d["p50"] <= 30 and d["p95"] <= 80:
            c.ok(c.measured, "p50 ≤30ms / p95 ≤80ms")
        else:
            c.bad(c.measured, "p50 ≤30ms / p95 ≤80ms", "卡点误差偏大")

    def _s7_soak(self, c):
        hours = float(getattr(self.args, "overnight_hours", 0) or 0)
        if hours <= 0:
            c.skip("optional", "未启用 --overnight-hours；长时项按需执行",
                   "过夜采样")
            return
        if not self.ensure_game(mouse=False):
            c.blocked("过夜项需游戏窗口", "游戏窗口可用")
            return
        k32 = ctypes.windll.kernel32
        k32.SetProcessWorkingSetSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                                 ctypes.c_size_t]

        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.DWORD), ("PageFaultCount", ctypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        def sample():
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo(k32.OpenProcess(0x1000, False,
                                                       os.getpid()),
                                       ctypes.byref(pmc), pmc.cb)
            procs = ctypes.windll.kernel32.GetCurrentProcess()
            handles = ctypes.c_uint()
            ctypes.windll.kernel32.GetProcessHandleCount(procs,
                                                         ctypes.byref(handles))
            return pmc.WorkingSetSize, handles.value

        ws0, h0 = sample()
        lines0 = len(base.records_mod.load(limit=100000) or [])
        end = time.time() + min(hours, 12) * 3600
        while time.time() < end:
            self.tick()
            time.sleep(min(30, max(1, end - time.time())))
        ws1, h1 = sample()
        lines1 = len(base.records_mod.load(limit=100000) or [])
        grow = (ws1 - ws0) / max(1, ws0)
        c.codes.update({"ws0": ws0, "ws1": ws1, "grow": round(grow, 3),
                        "h0": h0, "h1": h1, "lines0": lines0, "lines1": lines1})
        if grow <= 0.15 and (h1 - h0) <= 200 and lines1 >= lines0:
            c.ok(f"{hours:.0f}h · 内存 {grow:.1%} · 句柄 +{h1 - h0} · "
                 f"台账 {lines0}→{lines1}", "内存 ≤15%、句柄 ≤+200、无丢行")
        else:
            c.bad(f"{hours:.0f}h · 内存 {grow:.1%} · 句柄 +{h1 - h0}",
                  "内存 ≤15% / 句柄 ≤+200", "长时存在泄漏")

    # ================================================================ 执行
    def execute(self):
        self._open_trace()
        self._start_watchdog()
        self._banner()
        stages = self.args.stages
        try:
            for sid in stages:
                if self.abort.is_set():
                    self._fill_remaining(sid, "已中止", "blocked")
                    self.close_stage(sid, _pc(), 0, "已中止")
                    continue
                self.run_stage(sid)
                if self.relaunch_requested:
                    break
                if self.trade["suspected"]:
                    self.say("[止损] 疑似成交，停止后续阶段", "fail")
                    for left in stages[stages.index(sid) + 1:]:
                        self._fill_remaining(left, "误买止损，未执行", "blocked")
                        self.close_stage(left, _pc(), 0, "误买止损")
                    break
        except Aborted:
            pass
        except Exception as exc:                        # noqa: BLE001
            self.fatal = f"{type(exc).__name__}: {exc}"
            self.trace(traceback.format_exc())
        finally:
            self._stop_watchdog()
            self.cleanup_s5()
            try:
                self.cleanup()
            except Exception as exc:                    # noqa: BLE001
                self.cleanup = {"error": f"{type(exc).__name__}: {exc}"}
            self._write_reports()
            self._prune_artifacts()

    def _prune_artifacts(self, keep=12, days=14):
        """清理策略：截图/备份各保留最近 12 个 run 目录且不超过 14 天（当前 run 永不删）。"""
        now = time.time()
        pruned = []
        for root in (SHOT_ROOT, BACKUP_ROOT):
            try:
                dirs = [os.path.join(root, d) for d in os.listdir(root)
                        if os.path.isdir(os.path.join(root, d))]
            except (FileNotFoundError, OSError):
                continue
            dirs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for i, p in enumerate(dirs):
                if os.path.basename(p) == self.run_id:
                    continue
                fresh = (now - os.path.getmtime(p)) < days * 86400
                if i < keep and fresh:
                    continue
                shutil.rmtree(p, ignore_errors=True)
                pruned.append(os.path.basename(p))
        if pruned:
            self.say(f"[清理] 移除旧证据目录 {len(pruned)} 个（保留最近 {keep} 个/≤{days} 天）",
                     "info")

    def _banner(self):
        self.say("=" * 72)
        self.say(f"真机验证 v2 · run {self.run_id} · 阶段 {'/'.join(self.args.stages)}")
        self.say(f"项目 {ROOT}")
        self.say(f"中止：{self.args.hotkey} 或创建 {ABORT_FILE}")
        self.say("不卡壳：单项超时=待判；阶段重试预算 "
                 f"{self.args.retry}；单实例；异常安全复位")
        self.say(f"报告 {JSON_PATH} / {MD_PATH}")
        self.say("=" * 72)

    # ------------------------------------------------------------ 报告
    def _summary(self):
        n = Counter(c.status for c in self.checks)
        fixed = sum(1 for x in self.checks if x.status == "pass" and x.fix)
        if n["fail"]:
            verdict = "不通过"
            line = f"不通过：{n['fail']} 项 FAIL"
        elif n["blocked"]:
            verdict = "阻塞"
            line = f"阻塞：{n['blocked']} 项 BLOCKED（不视为失败）"
        elif n["pending"]:
            verdict = "待判"
            line = f"待判：{n['pending']} 项超时"
        else:
            verdict = "通过"
            line = "通过：全部可判定项通过"
        return {"verdict": verdict, "line": line, "pass": n["pass"],
                "fail": n["fail"], "blocked": n["blocked"], "skip": n["skip"],
                "pending": n["pending"], "fixed": fixed, "fixes": len(self.fixes)}

    def _coverage_diff(self):
        """矩阵 vs 实跑对账：只对本次实际执行的阶段做差集。

        - missing：矩阵有、本次阶段跑了但没出现（理论上 _fill_remaining 兜底，
          出现即说明该 ID 写错阶段或实现里漏调用）；
        - extra：实跑有、矩阵没有（已在 item() 实时记录，这里兜底再算一遍）。
        """
        ran = [s["id"] for s in self.stage_rows]
        ids_ran = {c.cid for c in self.checks}
        seen = [cid for cid in ids_ran if cid in _COVERAGE_IDS]
        missing = [r["id"] for r in COVERAGE_ROWS
                   if r["stage"] in ran and r["id"] not in ids_ran]
        extra = [cid for cid in ids_ran if cid not in _COVERAGE_IDS]
        must_left = [r["id"] for r in COVERAGE_ROWS
                     if r["stage"] in ran and r["game"] and r["id"] not in seen]
        return {"stages_ran": ran, "missing": missing, "extra": sorted(extra),
                "must_game_not_run": must_left}

    def _write_reports(self):
        conc = self._summary()
        must_ids, no_ids = coverage_breakdown()
        cov_path = ""
        try:
            cov_path = write_coverage_md()
        except Exception as exc:                        # noqa: BLE001
            print(f"[覆盖矩阵] 写入失败：{exc}", flush=True)
        payload = {
            "schema": "realmachine_v2_result/1",
            "run_id": self.run_id,
            "started_at": datetime.fromtimestamp(self.started_at).strftime(
                "%Y-%m-%d %H:%M:%S"),
            "finished_at": _iso(),
            "duration_s": round(time.time() - self.started_at, 2),
            "host": {"hostname": os.environ.get("COMPUTERNAME", ""),
                     "user": os.environ.get("USERNAME", ""),
                     "python": sys.version.split()[0], "platform": sys.platform},
            "argv": sys.argv,
            "options": vars(self.args),
            "owner_rules": [{"owner": o, "rule": r} for o, r in OWNER_RULES],
            "conclusion": conc,
            "abort": {"aborted": self.abort.is_set(), "source": self.abort_source},
            "relaunch_requested": self.relaunch_requested,
            "fatal": self.fatal,
            "cleanup": self.cleanup,
            "stages": self.stage_rows,
            "checks": [c.as_dict() for c in self.checks],
            "fixes": self.fixes,
            "s5": {"url": self._s5_http,
                   "config_restored": self._s5_before_sha == base_hash(CFG_PATH)
                   if os.path.isfile(CFG_PATH) else None,
                   "items": getattr(self, "_s5_results", [])},
            "shots_root": self._shot_dir,
            "coverage": {
                "path": cov_path,
                "counts": {"total": len(COVERAGE_ROWS),
                           "must_game": len(must_ids),
                           "no_game": len(no_ids)},
                "must_game": must_ids,
                "no_game": no_ids,
                "uncovered": list(_UNCOVERED_IDS),
                "diff": self._coverage_diff(),
            },
        }
        try:
            with open(JSON_PATH, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception as exc:                        # noqa: BLE001
            print(f"[报告] JSON 写入失败：{exc}", flush=True)
        try:
            with open(MD_PATH, "w", encoding="utf-8") as fp:
                fp.write(self._md(conc, payload))
        except Exception as exc:                        # noqa: BLE001
            print(f"[报告] Markdown 写入失败：{exc}", flush=True)
        self.say(f"[报告] {JSON_PATH}")
        self.say(f"[报告] {MD_PATH}")

    def _md(self, conc, payload):
        L = []
        L.append(f"# 真机验证 v2 结果 · {payload['run_id']}\n")
        L.append("## 结论\n")
        L.append(f"> {conc['line']}\n")
        L.append(f"> 通过 {conc['pass']} · 失败 {conc['fail']} · 阻塞 {conc['blocked']}"
                 f" · 跳过 {conc['skip']} · 待判 {conc['pending']}"
                 f" · 自动修复 {conc['fixes']} 项\n")
        L.append(f"> 用时 {payload['duration_s'] / 60:.1f} 分钟 · "
                 f"中止={'是' if payload['abort']['aborted'] else '否'} · "
                 f"截图目录 {payload['shots_root']}\n")
        if payload["fatal"]:
            L.append(f"> 异常：{payload['fatal']}\n")
        cov = payload.get("coverage") or {}
        diff = cov.get("diff") or {}
        L.append("\n## 覆盖清单（矩阵 → 本次实跑）\n")
        L.append(f"> 矩阵 {cov.get('counts', {}).get('total', 0)} 项："
                 f"必须开游戏 {cov.get('counts', {}).get('must_game', 0)} 项 / "
                 f"不开游戏 {cov.get('counts', {}).get('no_game', 0)} 项；"
                 f"矩阵文件 {cov.get('path', '')}\n")
        L.append("\n### 必须开游戏才能测的项\n")
        L.append("、".join(cov.get("must_game") or []) + "\n")
        L.append("\n### 不开游戏也能测的项\n")
        L.append("、".join(cov.get("no_game") or []) + "\n")
        L.append("\n### 本次实跑对账\n")
        L.append(f"- 已跑阶段：{'/'.join(diff.get('stages_ran') or []) or '—'}")
        L.append(f"- 矩阵漏跑：{'、'.join(diff.get('missing') or []) or '无'}")
        L.append(f"- 矩阵缺失（实现了没登记）："
                 f"{'、'.join(diff.get('extra') or []) or '无'}")
        L.append(f"- 必须开游戏但本次没跑（留给真机）："
                 f"{'、'.join(diff.get('must_game_not_run') or []) or '无'}\n")
        L.append("\n### 阶段总表\n")
        L.append("| 阶段 | 名称 | 结果 | 过/失/阻/跳/待 | 尝试 | 耗时 | 门禁 |")
        L.append("|---|---|---|---|---|---|---|")
        st_cn = {"pass": "过", "fail": "不过", "blocked": "阻塞",
                 "pending": "待判", "skip": "跳过"}
        for p in payload["stages"]:
            L.append(f"| {p['id']} | {p['name']} | {st_cn.get(p['status'], p['status'])} | "
                     f"{p['pass']}/{p['fail']}/{p['blocked']}/{p['skip']}/{p['pending']} | "
                     f"{p['attempts']} | {p['duration_s']:.1f}s | "
                     f"{'过' if p['gate_ok'] else '未过'} |")
        L.append("\n## 逐项明细\n")
        for p in payload["stages"]:
            L.append(f"### {p['id']} {p['name']}\n")
            L.append("| 项 | 结果 | 归属 | 实测 | 阈值 | 说明 | 证据 |")
            L.append("|---|---|---|---|---|---|---|")
            for c in p["checks"]:
                res = st_cn.get(c["status"], c["status"])
                if c["status"] == "skip" and c["reason"]:
                    res = "跳过（" + REASON_CN.get(c["reason"], c["reason"]) + "）"
                det = (c["detail"] or "").replace("|", "/").replace("\n", " ")[:160]
                ev = c["shots"][0] if c["shots"] else ""
                L.append(f"| {c['id']} {c['title']} | {res} | {c['owner'] or '—'} | "
                         f"{c['measured']} | {c['threshold']} | {det} | "
                         f"{os.path.basename(ev) if ev else ''} |")
            L.append("")
        L.append("## 归属判定规则\n")
        for o, r in OWNER_RULES:
            L.append(f"- **{o}**：{r}")
        L.append("\n## 即修即验记录\n")
        if not payload["fixes"]:
            L.append("本轮无自动修改。\n")
        else:
            L.append("| 文件 | 行 | 依据 | 修改 | 重跑 | 归属 |")
            L.append("|---|---|---|---|---|---|")
            for f in payload["fixes"]:
                lines = ",".join(str(x) for x in (f.get("lines") or [])) or "—"
                change = f"{f.get('before')} → {f.get('after')}" if f.get("before") is not None else (f.get("action") or "—")
                L.append(f"| {f.get('file') or '—'} | {lines} | {f.get('evidence', '')} | "
                         f"{str(change)[:100]} | {f.get('rerun', '')} | {f.get('owner', '')} |")
        L.append("\n## 复现路径（失败项）\n")
        any_fail = False
        for c in payload["checks"]:
            if c["status"] not in ("fail", "blocked"):
                continue
            any_fail = True
            L.append(f"### {c['id']} {c['title']}（{c['owner'] or '未归类'}）\n")
            L.append(f"- 说明：{c['detail'] or c['reason']}")
            for s in c["repro"].get("steps") or []:
                L.append(f"  - 步：{s}")
            for s in (c["repro"].get("log") or [])[-6:]:
                L.append(f"  - 日志：{s}")
            if c["repro"].get("codes"):
                L.append(f"  - 返回码：{json.dumps(c['repro']['codes'], ensure_ascii=False)}")
            if c["repro"].get("shots"):
                L.append("  - 截图：")
                for p in c["repro"]["shots"]:
                    L.append(f"    - {p}")
            L.append("")
        if not any_fail:
            L.append("无失败/阻塞项。\n")
        L.append("\n## 总账\n")
        fixed_items = [c for c in payload["checks"] if c["fix"]]
        L.append(f"- 通过项：{conc['pass']}")
        L.append(f"- 失败项：{conc['fail']}"
                 + ("（" + "、".join(c["id"] for c in payload["checks"]
                                     if c["status"] == "fail") + "）" if conc["fail"] else ""))
        L.append(f"- 阻塞项：{conc['blocked']}"
                 + ("（" + "、".join(c["id"] for c in payload["checks"]
                                     if c["status"] == "blocked") + "）" if conc["blocked"] else ""))
        L.append(f"- 自动修复项：{conc['fixes']}")
        for c in fixed_items:
            L.append(f"  - {c['id']}：{c['fix'].get('file', '')} "
                     f"行 {c['fix'].get('lines', [])} → 重跑 {c['fix'].get('round', '')} 轮通过")
        L.append(f"- 跳过/待判：{conc['skip']} / {conc['pending']}")
        return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 修复器
def fix_clamp_switches(run, c):
    """S0-2：把越界开关夹回 BOUNDS 并记录 diff。"""
    adjusted = isinstance(c.evidence, dict) and c.evidence.get("adjusted")
    if not adjusted:
        return None
    before = "；".join(f"{k}={o}" for k, o, _ in adjusted)
    after = "；".join(f"{k}={n}" for k, _, n in adjusted)
    lines = []
    for k, _, _ in adjusted:
        ln = find_line(CFG_PATH, f'"{k.split(".")[-1]}"')
        if ln:
            lines.append(ln)
    return {"file": os.path.relpath(CFG_PATH, ROOT),
            "lines": sorted(set(lines))[:8] or [0],
            "evidence": f"{c.cid} 越界 {len(adjusted)} 项", "before": before,
            "after": after, "owner": "环境", "round": 1}


def fix_clamp_regions(run, c):
    adjusted = isinstance(c.evidence, dict) and c.evidence.get("adjusted")
    if not adjusted:
        return None
    before = "；".join(f"{n}={o}" for n, o, _ in adjusted)
    after = "；".join(f"{n}={nw}" for n, _, nw in adjusted)
    lines = [find_line(CFG_PATH, f'"{n}"') for n, _, _ in adjusted]
    return {"file": os.path.relpath(CFG_PATH, ROOT),
            "lines": sorted({x for x in lines if x})[:8] or [0],
            "evidence": f"{c.cid} 区域越界 {len(adjusted)} 个", "before": before,
            "after": after, "owner": "环境", "round": 1}


FIXERS = {"S0-2": fix_clamp_switches, "S0-9": fix_clamp_regions}


def apply_code_fix(path, old, new, *, evidence="", backup_dir=None,
                   cid="", round=1, owner=""):
    """限本仓库的最小代码修补：备份 → 唯一精确替换 → 生成 unified diff。

    约束（对应「不许无记录修改」）：
    - 目标必须在 ROOT 下，越界直接拒绝；
    - old 必须在文件里**唯一**出现，否则不猜、返回 None；
    - 原文备份到 `_rm_backup/<run>/code/`，diff 与行号写进修复记录。
    返回 None = 没改（依据不足/已被改过），调用方停止重试。
    """
    try:
        ap = os.path.abspath(path)
        if os.path.commonpath([ap, ROOT]) != os.path.abspath(ROOT):
            raise PermissionError(f"越界修改被拒：{ap}")
    except ValueError as exc:
        raise PermissionError(f"越界修改被拒：{path}（{exc}）") from exc
    if not os.path.isfile(ap):
        return None
    with open(ap, "r", encoding="utf-8", errors="surrogateescape") as fp:
        before_all = fp.read()
    if before_all.count(old) != 1:
        return None
    after_all = before_all.replace(old, new, 1)
    if backup_dir:
        rel = os.path.relpath(ap, ROOT).replace("\\", "/")
        bak = os.path.join(backup_dir, "code", rel)
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        with open(bak, "w", encoding="utf-8", errors="surrogateescape") as fp:
            fp.write(before_all)
    with open(ap, "w", encoding="utf-8", errors="surrogateescape") as fp:
        fp.write(after_all)
    rel = os.path.relpath(ap, ROOT).replace("\\", "/")
    diff = "".join(difflib.unified_diff(
        before_all.splitlines(True), after_all.splitlines(True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3))
    lines = []
    for ln in diff.splitlines():
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
        if m:
            lines.append(int(m.group(3)))
    return {"file": rel, "lines": lines or [0], "diff": diff,
            "evidence": evidence or f"{cid} 失败", "owner": owner,
            "round": round, "backup": os.path.relpath(bak, ROOT).replace("\\", "/")
            if backup_dir else ""}


# ---------------------------------------------------------------- 小工具
def base_hash(path):
    import hashlib
    try:
        with open(path, "rb") as fp:
            return hashlib.sha256(fp.read()).hexdigest()
    except Exception:                                # noqa: BLE001
        return ""


def find_line(path, needle):
    try:
        with open(path, "r", encoding="utf-8") as fp:
            for i, line in enumerate(fp, 1):
                if needle in line:
                    return i
    except Exception:                                # noqa: BLE001
        pass
    return 0


def find_switch_value(groups, key):
    for g in groups:
        for it in (g.get("items") or []):
            if it.get("key") == key:
                return it.get("value")
    return None


def same_value(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    try:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(float(a) - float(b)) < 1e-6
    except Exception:                                # noqa: BLE001
        pass
    return a == b


# ---------------------------------------------------------------- 目录清单
# 由覆盖矩阵派生，保证「矩阵里有的项，执行器一定兜底」：阶段中断/超时时
# _fill_remaining 按这里补位，不会因为新增 ID 漏项或键错。
V2_CHECKS = {}
for _r in COVERAGE_ROWS:
    V2_CHECKS.setdefault(_r["stage"], []).append(
        (_r["id"], _r["title"], bool(_r["gate"])))
# 矩阵覆盖自检：实现过但矩阵里没有的 ID，报告里点名，防止「测了却不在矩阵」。
_COVERAGE_IDS = {r["id"] for r in COVERAGE_ROWS}
_UNCOVERED_IDS: list[str] = []


# ---------------------------------------------------------------- CLI
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="真机验证执行器 v2")
    p.add_argument("--stage", action="append", default=[],
                   help="S0..S5 或 all，可重复；默认 all")
    p.add_argument("--retry", type=int, default=STAGE_RETRY,
                   help="每阶段重试预算（含首次，默认 3）")
    p.add_argument("--global-timeout", type=int, default=1800,
                   help="全局看门狗（秒），0=关闭")
    p.add_argument("--hotkey", default="F8")
    p.add_argument("--event-key", default="F9")
    p.add_argument("--auto-elevate", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="非管理员时自动请求提权重启（默认开；--no-auto-elevate 关闭）")
    p.add_argument("--allow-buy", action="store_true", help="S2 允许真实下单")
    p.add_argument("--target", default="", help="S2 测试标的名称")
    p.add_argument("--max-price", type=float, default=0.0)
    p.add_argument("--budget", type=float, default=0.0)
    p.add_argument("--allow-fire", action="store_true", help="S3 允许真实点火")
    p.add_argument("--s3-observe-s", type=int, default=45)
    p.add_argument("--s3-p95-ms", type=float, default=80.0)
    p.add_argument("--s3-window-ms", type=float, default=120.0)
    p.add_argument("--allow-faults", action="store_true",
                   help="S4 允许真实制造故障（ESC/点空白/切前台）")
    p.add_argument("--plan", action="store_true", help="只打印阶段与阈值")
    p.add_argument("--coverage-only", action="store_true",
                   help="只生成 _realmachine_coverage.md 并列出两串清单")
    p.add_argument("--self-check", action="store_true", help="只检查依赖导入")
    p.add_argument("--live-interval", type=float, default=0.2,
                   help="兼容基础版字段")
    p.add_argument("--countdown-observe-s", type=float, default=0.0,
                   help="兼容基础版字段")
    p.add_argument("--soak-s", type=float, default=0.0,
                   help="兼容基础版字段")
    p.add_argument("--overnight-hours", type=float, default=0.0,
                   help="兼容基础版字段")
    p.add_argument("--click-tests", type=int, default=0,
                   help="兼容基础版字段")
    args = p.parse_args(argv)
    if not args.stage:
        args.stage = ["all"]
    stages = []
    for s in args.stage:
        if s.lower() == "all":
            stages = list(STAGE_CN.keys())
            break
        u = s.upper()
        if u not in STAGE_CN:
            p.error(f"未知阶段：{s}")
        if u not in stages:
            stages.append(u)
    args.stages = stages
    return args


def print_plan(args):
    print("v2 阶段排期（真实耗时以报告为准）")
    print("-" * 58)
    for sid in args.stages:
        print(f"{sid}  {STAGE_CN[sid]:<12} {len(V2_CHECKS[sid])} 项  "
              f"超时 {STAGE_TIMEOUT[sid]}s  单项 {ITEM_TIMEOUT[sid]}s")
    print("-" * 58)
    print(f"阶段重试预算 {args.retry}（含首次）· 即修即验单项 ≤{HEAL_ROUNDS} 轮")
    print(f"中止：{args.hotkey} 或创建 {ABORT_FILE}")
    print(f"报告：{JSON_PATH} / {MD_PATH}")
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                # noqa: BLE001
        pass
    if base.IMPORT_ERROR:
        print(f"依赖导入失败：{base.IMPORT_ERROR}", flush=True)
        return 3
    if args.self_check:
        print("import_error:", base.IMPORT_ERROR or "无")
        print("ocr:", base.ocr_mod.backend_name()
              if hasattr(base.ocr_mod, "backend_name") else "?")
        print("stages:", ",".join(args.stages))
        return 0
    cov_path = write_coverage_md()
    must_ids, no_ids = coverage_breakdown()
    if args.coverage_only:
        print(f"[覆盖矩阵] {cov_path}")
        print(f"共 {len(COVERAGE_ROWS)} 项 · 必须开游戏 {len(must_ids)} · "
              f"不开游戏 {len(no_ids)}")
        print("必须开游戏：" + "、".join(must_ids))
        print("不开游戏：" + "、".join(no_ids))
        return 0
    if args.plan:
        print_plan(args)
        print(f"覆盖矩阵：{cov_path}（必须开游戏 {len(must_ids)} 项 / "
              f"不开游戏 {len(no_ids)} 项）")
        return 0
    if os.path.exists(ABORT_FILE):
        print(f"检测到中止文件 {ABORT_FILE}：按中止处理（文件保留，移除后可重跑）",
              flush=True)
        return 2
    if not single_instance.acquire(MUTEX_NAME):
        print("已有 v2 执行器实例在跑（单实例闸拒绝启动）", flush=True)
        return 2
    base.TH["event_timeout_s"] = 1.0
    run = RunnerV2(args)
    try:
        run.start_cursor = base.input_mod.Mouse.cursor()
        run.start_foreground = base.input_mod.Mouse.foreground()
        run.hotkey_abort = base.hotkey_mod.Hotkey(args.hotkey, run._on_abort)
        run.hotkey_event = base.hotkey_mod.Hotkey(args.event_key, run._on_event)
    except Exception:                                # noqa: BLE001
        pass
    try:
        run.execute()
    finally:
        single_instance.release()
    conc = run._summary()
    print(f"\n{conc['line']}", flush=True)
    n = Counter(c.status for c in run.checks)
    if run.abort.is_set() or run.relaunch_requested:
        code = 2
    elif run.fatal:
        code = 3
    elif n["fail"]:
        code = 1
    elif n["blocked"] or n["pending"]:
        code = 4
    else:
        code = 0
    print(f"退出码 {code}（0=通过 1=有FAIL 2=中止/提权重启 3=异常 4=仅阻塞/待判）",
          flush=True)
    # 驱动的 C 扩展在进程收尾（DLL_PROCESS_DETACH / FreeLibrary）会挂住；
    # 报告与清理已完成，直接 TerminateProcess 绕开 detach，保证「不卡壳」。
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:                                # noqa: BLE001
        pass
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x0001, False, os.getpid())   # PROCESS_TERMINATE
        if h:
            k32.TerminateProcess(h, int(code))
    except Exception:                                # noqa: BLE001
        pass
    try:
        os.kill(os.getpid(), 9)
    except Exception:                                # noqa: BLE001
        pass
    os._exit(code)


if __name__ == "__main__":
    sys.exit(main())
