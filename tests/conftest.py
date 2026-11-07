"""pytest 公共夹具。

OCR 只剩一个后端了（PaddleOCR PP-OCRv6，走本机服务），而 tests/ 里有一批
用例是「拿录屏帧跑真 OCR，再断言解析结果」—— 它们必须有 OCR 才能跑。

所以这里做两件事：
  1. 整个会话开始前问一次 OCR 服务在不在；
  2. 不在的话，把「依赖真 OCR」的模块整体 skip，并说清原因。

为什么是 skip 而不是假装通过：那些用例断言的是真实识别结果（`2,001` 被
读坏时要返回 None 之类），没有真 OCR 就没有意义。**跳过是诚实的，通过是
骗人的。** 起服务的方法见 说明.txt 第九节。
"""
import builtins
import io
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _isolation import groups as iso_groups  # noqa: E402
from _isolation import lock as iso_lock        # noqa: E402
from _isolation import priority as iso_priority  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 本次 pytest 运行的标识：xdist worker 通常继承 controller 的环境变量，
# 于是同一场运行的所有进程写同一批等待统计文件，汇总时不会和并发的另一场混。
RUN_ID = os.environ.get("SJZ_TEST_RUN_ID") or "%d-%d" % (int(time.time()), os.getpid())
os.environ["SJZ_TEST_RUN_ID"] = RUN_ID
# 区域坐标快照写到临时目录：测试不许污染真实的 %LOCALAPPDATA%\dafors。
os.environ.setdefault("DAFORS_USER_DIR",
                      os.path.join(tempfile.gettempdir(),
                                   "dafors-test-user-" + RUN_ID))
# 模板自愈（core/template_heal.py）默认会按当前画面重采模板并写
# templates/*.png + meta.json。测试机器上「当前画面」是桌面，绝不能拿它
# 覆盖仓库里的模板 —— 这里统一关掉写盘，需要验证写盘行为的用例自己用
# tmp_path 打开（见 tests/test_template_heal.py）。
os.environ.setdefault("DAFORS_TEMPLATE_WRITE", "0")
_SESSION_START = time.time()

# 项目里的真实数据文件：任何测试都不许改写它们（见 tests/ISOLATION.md）
GUARDED_FILES = (
    "config.json",
    "apps.json",
    "cu.json",
    "tuning_recommendation.json",
    os.path.join("data", "records.jsonl"),
)


@pytest.fixture(autouse=True)
def _guard_real_files(request, monkeypatch):
    """真实数据文件写保护：谁写谁当场失败，并指名道姓。

    判据为什么从「跑前跑后比 sha256」换成「拦 open」：2026-11-07 实测，别的
    分支的进程在持续重写 config.json（同目录还躺着 config.json.good /
    config.json.lock），比哈希会把**别人的写**算到当前用例头上 —— 一次全量
    因此报了 50 个 ERROR，还撞上一次 Windows 排他锁（PermissionError：读都
    读不进来）。判据只能认「本进程的写」，所以直接在 `open` 这一层拦：写
    模式命中受保护文件，立刻失败。
    """
    guarded = {os.path.normcase(os.path.join(ROOT, p)) for p in GUARDED_FILES}
    real_open = io.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            try:
                path = os.path.normcase(os.path.abspath(os.fspath(file)))
            except TypeError:              # 传的是文件描述符，管不着
                path = None
            if path in guarded:
                rel = os.path.relpath(path, ROOT)
                pytest.fail(
                    "测试 %s 正在以 %r 打开项目真实数据文件 %s —— 必须在 "
                    "tmp_path / monkeypatch 里做。" % (request.node.nodeid, mode, rel))
        return real_open(file, mode, *args, **kwargs)

    # builtins.open 与 io.open 是两个名字指向同一个函数：两个都得换
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(builtins, "open", guarded_open)
    yield


@pytest.fixture(autouse=True)
def _restore_process_env():
    """进程级环境变量兜底还原：测试里直接改 os.environ 也不会漏到下一个用例。

    monkeypatch 管得住显式 setenv；管不住 `os.environ["X"] = ...` 这种写法。
    """
    saved = dict(os.environ)
    yield
    for key in list(os.environ):
        if key not in saved:
            del os.environ[key]
    for key, val in saved.items():
        if os.environ.get(key) != val:
            os.environ[key] = val


@pytest.fixture(autouse=True)
def _restore_license_gate():
    """许可门禁是进程级全局单例，任何用例跑完都必须回到未判定状态。"""
    try:
        from core import license as lic
    except ImportError:                      # 许可模块还没并进来
        yield
        return
    saved = lic._ENFORCED
    yield
    lic._ENFORCED = saved


def _relax_timeout(ocr):
    """测试里把 OCR 超时放宽到 30 秒。

    真截图用例断言的是「认得出什么」，不是「多快」。游戏正占着显卡时，
    一次识别偶尔会超过生产用的 5 秒 —— 那会让用例随机拿到空结果，看起来
    像识别坏了。生产超时另有考虑（抢购等不起），别在这里跟着一起卡。
    """
    s = dict(ocr._settings or {})
    s["ppocr"] = {**s.get("ppocr", {}), "timeout_s": 30.0}
    ocr._settings = s
    ocr._backend = None


def _smoke(ocr):
    """真认一次字：能认出来才跑真截图用例。

    只探 TCP 不够 —— 服务活着但推理慢到超时的时候，用例会随机拿到空结果，
    看起来像「识别坏了」，其实是显卡被游戏占满了。这种情况整组跳过，
    并在原因里写清楚，比给一个假失败有用。
    """
    try:
        from core import vision
        img = vision.imread(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "fixtures", "market", "list_unfiltered.png"))
        if img is None:
            return True                      # 没有素材就不拦
        words = ocr.read_words(img)
        return bool(words)
    except Exception:                        # noqa: BLE001
        return False


# 这些模块不注入假 OCR，直接对录屏帧跑真识别
NEEDS_REAL_OCR = {
    "test_popup.py",
    "test_market_video.py",
    "test_engine_insufficient.py",
    # 典藏外观页 / 两套筛选面板的真截图回放（2026-11-02 补）
    "test_skin_ocr_real.py",
}


@pytest.fixture
def perf_machine():
    """墙钟 / 微秒级精度用例的前置条件：本机空载，且显式要求跑性能门槛。

    `test_popup::test_detection_is_fast_enough_for_the_buy_loop` 断的是
    「弹窗识别 < 300ms」，`test_precision::...deviation_distribution` 断的是
    「触发偏移 p95 < 1.5ms」—— 两者量的都是**机器空载时的成本**。

    实测本机同时跑多份 pytest 时，OCR 通路排队：前者的中位数从 ~50ms 涨到
    304ms（min 203 / max 336），后者的 p95 从 ~1.4ms 涨到 2.8ms。这种红与
    被测代码无关，却正好会掩盖真正的退化 —— 真退化是 4 倍以上的量级。

    所以只在显式设了 `SJZ_PERF=1` 的空载机器上跑；断言本身一个字没改。
    """
    if os.environ.get("SJZ_PERF", "").lower() not in ("1", "true", "yes"):
        pytest.skip("性能门槛用例：需空载机器，显式设 SJZ_PERF=1 才跑")


@pytest.fixture(autouse=True)
def _keep_user_data_clean(tmp_path_factory, monkeypatch):
    """测试**绝不**往用户真实的 data/ 里写东西。

    为什么必须放在这里：`shotproof.DATA_DIR`（成交截图的默认目录）和
    `records.PATH`（成交记录）都是 **import 时就算死**的常量 —— 只 monkeypatch
    `config.DATA_DIR` 对它们无效。于是任何走到成功分支的用例都会在
    `data/records.jsonl` 里留下一条「成交」、在 `data/shots/` 里留一张
    100×100 的黑图。实测真实记录里混着 `elapsed=200350`（那是开机秒数，
    因为用例传了 `t_start=0.0`）、名字为空的假成交 —— 和真成交混在一起，
    事后根本分不清哪条是真的。

    放在 conftest 里而不是每个用例各写一遍：漏一个就会重新开始污染。
    """
    from core import records
    from core import shotproof

    d = tmp_path_factory.mktemp("userdata")
    monkeypatch.setattr(shotproof, "DATA_DIR", str(d))
    monkeypatch.setattr(records, "PATH", str(d / "records.jsonl"))
    yield


def pytest_collection_modifyitems(config, items):
    _mark_heavy(items)
    from core import ocr

    # OCR 探测（可用性 + 真识别 smoke）会加载模型/起服务，本身就是重负载；
    # 四份 pytest 同时初始化时实测能拖到几分钟。它发生在 fixture 之外，
    # 所以单独套一次 heavy 锁（超时就退回原行为，绝不因为锁失败而漏判）。
    probe_locks = []
    probe_waited = 0.0
    probe_timed_out = False
    probe_t0 = time.monotonic()
    if not config.getoption("no_test_lock", False):
        probe_locks = iso_lock.build_locks(
            ("ocr",), "collection::ocr-probe",
            timeout_s=min(config.getoption("test_lock_timeout"), 240.0),
            stale_after_s=config.getoption("test_lock_stale"),
        )
        try:
            for lk in probe_locks:
                probe_waited += lk.acquire()
        except iso_lock.LockTimeout:
            probe_waited += sum(getattr(lk, "waited_s", 0.0) for lk in probe_locks)
            probe_timed_out = True
            for lk in reversed(probe_locks):
                lk.release()
            probe_locks = []
    try:
        if ocr.available():
            _relax_timeout(ocr)
            if _smoke(ocr):
                return
    finally:
        if probe_locks or probe_timed_out:
            held = time.monotonic() - probe_t0
            for lk in reversed(probe_locks):
                lk.release()
            _record_lock("collection::ocr-probe", ("ocr",), probe_waited, held,
                         probe_timed_out)
    if ocr.available():
        why = "服务活着但这次没认出字（多半是显卡被游戏占满，识别超时）"
    else:
        why = ocr.backend_name()
    skip = pytest.mark.skip(reason=f"需要可用的 PP-OCR：{why}")
    for item in items:
        if item.path.name in NEEDS_REAL_OCR:
            item.add_marker(skip)


# --------------------------------------------------------------------------- #
# 跨进程互斥：重负载用例串行，轻量用例照常并发
#
# 背景见 tests/_isolation/groups.py 的准入条件与仓库根 _test_isolation_lock.md：
# 同机并发跑多份 pytest 时，时序/子进程/Playwright/OCR/真实驱动用例互相抢资源，
# 产生与代码无关的假失败。这里用系统临时目录里的文件锁把它们在**所有进程**
# （普通 pytest、xdist worker、别的会话）之间串行化。
# --------------------------------------------------------------------------- #

def pytest_addoption(parser):
    grp = parser.getgroup("isolation", "跨进程测试互斥")
    grp.addoption("--no-test-lock", action="store_true", dest="no_test_lock",
                  default=False,
                  help="禁用 heavy 用例的跨进程互斥锁（逃生开关）")
    grp.addoption("--test-lock-timeout", action="store", dest="test_lock_timeout",
                  type=float,
                  default=float(os.environ.get("SJZ_TEST_LOCK_TIMEOUT", "300")),
                  help="等锁上限秒数（默认 300；超时给出可读诊断，不会死锁）")
    grp.addoption("--test-lock-stale", action="store", dest="test_lock_stale",
                  type=float,
                  default=float(os.environ.get("SJZ_TEST_LOCK_STALE", "900")),
                  help="锁年龄超过多少秒视为陈旧并接管（默认 900）")
    grp.addoption("--test-lock-groups", action="store_true", dest="test_lock_groups",
                  default=False,
                  help="打印 heavy 分组清单并退出")


def pytest_cmdline_main(config):
    if config.getoption("test_lock_groups", False):
        print(iso_groups.inventory_text())
        return 0
    return None


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "heavy(group, groups): 跨进程串行重负载用例组"
        "（清单见 tests/_isolation/groups.py）")


def pytest_report_header(config):
    if config.getoption("no_test_lock", False):
        return ["test isolation: heavy 锁已禁用（--no-test-lock）"]
    return [
        "test isolation: heavy 锁启用 dir=%s timeout=%.0fs stale=%.0fs"
        % (iso_lock.lock_dir(), config.getoption("test_lock_timeout"),
           config.getoption("test_lock_stale"))
    ]


def _mark_heavy(items):
    """把分组表落成 pytest 标注：``-m heavy`` 可选中，报告里也能看出归属。"""
    for item in items:
        groups = iso_groups.groups_for_nodeid(item.nodeid)
        if groups:
            item.add_marker(pytest.mark.heavy(group=iso_groups.primary_group(groups),
                                             groups=list(groups)))


def _record_lock(nodeid, groups, waited_s, held_s, timed_out):
    iso_lock.record_wait({
        "run": RUN_ID,
        "pid": os.getpid(),
        "nodeid": nodeid,
        "groups": list(groups),
        "waited_ms": round(waited_s * 1000.0, 1),
        "held_ms": round(held_s * 1000.0, 1),
        "timed_out": bool(timed_out),
    })


@pytest.fixture(autouse=True)
def _cross_process_heavy_lock(request):
    """heavy 用例的跨进程互斥。

    轻量用例直接 yield，零额外开销；heavy 用例先拿总闸、再拿各自的组锁，
    拿不到就等（有上限），超时给出「谁在持有、等了多久、怎么自救」的可读失败。
    """
    nodeid = request.node.nodeid
    groups = iso_groups.groups_for_nodeid(nodeid)
    if not groups:
        # 也认可用例自己写的 @pytest.mark.heavy(...)：
        # 临时/新增用例可以不进组表，直接标注重负载归属。
        marker = request.node.get_closest_marker("heavy")
        if marker:
            raw = marker.kwargs.get("groups") or marker.kwargs.get("group") or "full"
            groups = tuple(raw) if isinstance(raw, (list, tuple)) else (str(raw),)
    if not groups:
        yield
        return
    config = request.config
    if config.getoption("no_test_lock", False):
        yield
        return

    locks = iso_lock.build_locks(
        groups, nodeid,
        timeout_s=config.getoption("test_lock_timeout"),
        stale_after_s=config.getoption("test_lock_stale"),
    )
    t0 = time.monotonic()
    waited = 0.0
    try:
        for lk in locks:
            waited += lk.acquire()
    except iso_lock.LockTimeout as exc:
        waited += sum(getattr(lk, "waited_s", 0.0) for lk in locks)
        for lk in reversed(locks):
            lk.release()
        _record_lock(nodeid, groups, waited, 0.0, True)
        pytest.fail(str(exc), pytrace=False)
    # 时序组：拿到锁之后才提优先级（等待期间不抢别人的 CPU），跑完立刻还原。
    old_priority = iso_priority.boost_for_timing() if "timing" in groups else None
    try:
        yield
    finally:
        held = time.monotonic() - t0
        iso_priority.restore(old_priority)
        for lk in reversed(locks):
            lk.release()
        _record_lock(nodeid, groups, waited, held, False)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if config.getoption("no_test_lock", False):
        terminalreporter.write_sep("-", "test isolation: heavy 锁已禁用（--no-test-lock）")
        return
    entries = iso_lock.collect_waits(RUN_ID, since_ts=_SESSION_START)
    if not entries:
        return
    terminalreporter.write_sep("-", "test isolation: heavy 锁等待统计")
    for line in iso_lock.summary_lines(entries):
        terminalreporter.write_line(line)
