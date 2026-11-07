"""全局热键（停止用）。

鼠标甩左上角的急停有个问题：抢购时手在键盘上，鼠标可能根本不在你手边；
而且游戏全屏时鼠标位置不受控。热键更可靠，按一下就停。

实现用 RegisterHotKey + 独立线程的消息循环。不装任何依赖。
注意：热键是全局独占的，别的程序已经占了同一个键会注册失败 —— 这时要
老实告诉用户换一个键，不能假装注册成功。

低级钩子（下面 HookHotkey，生产首选）本身**没有独占语义**：两个实例各装
一个都成功，按键会双触发。所以两条实现共用一个按 (修饰键, 键码) 命名的
跨进程互斥体（CreateMutexW + ERROR_ALREADY_EXISTS）来判定「这个键此刻归谁」。
"""
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes

# **必须用 use_last_error=True 的 WinDLL**：ctypes.windll 不保存 LastError，
# 下面的 ctypes.get_last_error() 永远返回 0 —— 注册失败时日志写「错误码 0」，
# 用户拿不到「被别的程序占用(1409)」这类真正的原因。
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# **CreateMutexW 必须声明 restype**：默认按 c_int 返回，64 位句柄会被截成
# 32 位，CloseHandle 拿到错值 —— 名字就永远放不掉了，第二个实例永远注册失败。
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

# 命名文件映射：互斥体只能判「有没有人占」，说不出「谁在占」。旁边再开一块
# 同名映射写持有者信息（pid / 启动时间 / exe 路径），报错时才能给用户线索。
_kernel32.CreateFileMappingW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                         wintypes.DWORD, wintypes.DWORD,
                                         wintypes.DWORD, wintypes.LPCWSTR]
_kernel32.CreateFileMappingW.restype = wintypes.HANDLE
_kernel32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                       wintypes.LPCWSTR]
_kernel32.OpenFileMappingW.restype = wintypes.HANDLE
_kernel32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                    wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_size_t]
_kernel32.MapViewOfFile.restype = ctypes.c_void_p
_kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
_kernel32.UnmapViewOfFile.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE,
                                                 wintypes.DWORD,
                                                 wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = ctypes.c_void_p

_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_WM_NULL = 0x0000

_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008

_ERROR_ALREADY_EXISTS = 183             # ERROR_ALREADY_EXISTS：命名对象已存在
_ERROR_HOTKEY_REGISTERED = 1409         # ERROR_HOTKEY_ALREADY_REGISTERED

# 本进程里已经握住的占位名。183 有两种来源：别的进程占着，或者**本进程
# 上一轮注册没释放**（改键 / 重复注册）。两者文案必须分开，否则用户会被
# 指向「另一个实例」去排查，实际上是他自己的上一次注册没放干净。
_LOCAL_HELD = {}
_LOCAL_LOCK = threading.Lock()

_HOLDER_BYTES = 1024
_PAGE_READWRITE = 0x04
_FILE_MAP_WRITE = 0x0002
_FILE_MAP_READ = 0x0004
_INVALID_HANDLE = wintypes.HANDLE(-1)


def _mutex_name(mods, vk):
    """同一个 (修饰键, 键码) 在所有实例里必须叫同一个名字。

    不加 `Global\\` 前缀 = 当前登录会话（等价于 `Local\\`）。这正好和
    RegisterHotKey 的语义对齐：热键本来就是「每个会话一份」，不同 Windows
    用户各按各的互不打扰；同一个会话里开两个实例才该互斥。

    名字取解析后的数值而不是原始字符串：`"f8"` / `"F8"` / `" F8 "` 会被
    parse 归一成同一个键，不能让写法差异绕过互斥。
    """
    return f"Local\\dafors.hotkey.{mods:x}.{vk:02x}"


def _holder_name(mods, vk):
    return _mutex_name(mods, vk) + ".holder"


def _exe_path(pid):
    """进程完整路径。权限不够时返回空串（调用方退回只显示 pid）。"""
    try:
        h = _kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED
        if not h:
            return ""
        try:
            n = wintypes.DWORD(300)
            buf = ctypes.create_unicode_buffer(300)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return buf.value
        finally:
            _kernel32.CloseHandle(h)
    except Exception:                        # noqa: BLE001
        pass
    return ""


def _write_holder(mods, vk):
    """在自己的占位旁边写持有者信息，返回要保活的 (map, view)。

    故意用**命名文件映射**而不是事件/信号量：映射能直接存字符串，别的
    进程一读就知道是谁占的。对象在最后一个句柄关闭时自动销毁，不会留痕。
    """
    try:
        h = _kernel32.CreateFileMappingW(_INVALID_HANDLE, None, _PAGE_READWRITE,
                                         0, _HOLDER_BYTES, _holder_name(mods, vk))
        if not h:
            return None, None
        view = _kernel32.MapViewOfFile(h, _FILE_MAP_WRITE, 0, 0, _HOLDER_BYTES)
        if not view:
            _kernel32.CloseHandle(h)
            return None, None
        info = {"pid": os.getpid(), "started": time.time(),
                "exe": _exe_path(os.getpid()),
                "at": time.strftime("%H:%M")}
        raw = json.dumps(info, ensure_ascii=False).encode("utf-8")[:_HOLDER_BYTES - 1]
        ctypes.memmove(view, raw, len(raw))
        return h, view
    except Exception:                        # noqa: BLE001
        return None, None


def _read_holder(mods, vk):
    """读占用者的自述信息；读不到（老版本实例 / 权限）返回 None。"""
    for _ in range(3):
        h = None
        try:
            h = _kernel32.OpenFileMappingW(_FILE_MAP_READ, False,
                                           _holder_name(mods, vk))
            if not h:
                return None
            view = _kernel32.MapViewOfFile(h, _FILE_MAP_READ, 0, 0, _HOLDER_BYTES)
            if not view:
                return None
            try:
                raw = ctypes.string_at(view, _HOLDER_BYTES).split(b"\x00", 1)[0]
                if not raw:
                    time.sleep(0.05)          # 对方刚建映射，内容还没写进来
                    continue
                info = json.loads(raw.decode("utf-8", "ignore"))
                return info if isinstance(info, dict) else None
            finally:
                _kernel32.UnmapViewOfFile(view)
        except Exception:                     # noqa: BLE001
            return None
        finally:
            if h:
                _kernel32.CloseHandle(h)
    return None


def _holder_text(info):
    """「被 PID 1234（C:\\...\\SJZ-Control.exe，启动于 23:58）占用」。"""
    if not info:
        return ""
    pid = info.get("pid")
    exe = info.get("exe") or ""
    started = info.get("started")
    when = ""
    try:
        if started:
            when = time.strftime("%H:%M", time.localtime(float(started)))
    except Exception:                        # noqa: BLE001
        when = str(info.get("at") or "")
    tail = f"启动于 {when}" if when else ""
    where = exe or "路径未知"
    return f"被 PID {pid}（{where}{'，' + tail if tail else ''}）占用"


def _same_exe_instances(exe_name=None, full_path=None):
    """同会话里同一个可执行文件的进程列表（pid + 路径）。

    按**完整路径**比对，不是按 exe 文件名：开发环境下大家都是 python.exe，
    按文件名会把编辑器、别的项目、WorkBuddy 的 python 全算成「实例」，
    报出「检测到 15 个实例」这种吓人的假警。打包后也一样 —— 另一个安装
    目录里的 SJZ-Control.exe 不是同一套数据，不该算进来。
    """
    name = (exe_name or os.path.basename(sys.executable) or "").lower()
    me_path = (full_path or _exe_path(os.getpid()) or "").lower()
    out = []
    try:
        TH32CS_SNAPPROCESS = 0x02

        class PE32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_void_p),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", wintypes.WCHAR * 260)]

        _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PE32)]
        _kernel32.Process32FirstW.restype = wintypes.BOOL
        _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PE32)]
        _kernel32.Process32NextW.restype = wintypes.BOOL

        snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == wintypes.HANDLE(-1):
            return out
        try:
            pe = PE32()
            pe.dwSize = ctypes.sizeof(PE32)
            ok = _kernel32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                pid = int(pe.th32ProcessID)
                exe = _exe_path(pid)
                if me_path:
                    # 拿不到路径就跳过：宁可少数，也不要拿别的程序凑数。
                    if exe and exe.lower() == me_path:
                        out.append({"pid": pid, "exe": exe})
                elif (pe.szExeFile or "").lower() == name:
                    out.append({"pid": pid, "exe": exe})
                ok = _kernel32.Process32NextW(snap, ctypes.byref(pe))
        finally:
            _kernel32.CloseHandle(snap)
    except Exception:                        # noqa: BLE001
        pass
    return out


def instances(exe_name=None):
    """同会话里同名进程（默认看自己这个可执行文件），给「检测到 N 个实例」用。"""
    return _same_exe_instances(exe_name)


def holder_of(spec):
    """某个热键此刻的占用者自述信息（pid / exe / 启动时间）；没人占返回 None。"""
    parsed = parse(spec)
    if not parsed:
        return None
    return _read_holder(*parsed)


def _msg_occupied(spec, code, who="", holder=None):
    """「被占」文案：**必须分清是谁占的**，不能一律甩给「另一个实例」。

    183 的两个来源：别的进程真的握着名字；或者本进程上一轮没释放。
    1409 是 RegisterHotKey 的系统级占用（别的程序抢了这个键）。
    """
    if code == _ERROR_ALREADY_EXISTS:
        if who == "local":
            return (f"热键 {spec} 在本进程内重复注册（上一次的占位没释放）"
                    f"（错误码 183）——通常重启一次就好；"
                    f"若反复出现，请在设置页点「结束其它实例」")
        clue = _holder_text(holder) or (who if who else "另一个实例正在用它")
        return (f"热键 {spec} 已被占用（错误码 183）——{clue}；"
                f"占位会随该进程退出自动释放，重启一次通常就好，"
                f"也可以在设置页点「结束其它实例」")
    if code == _ERROR_HOTKEY_REGISTERED:
        return (f"热键 {spec} 已被别的程序注册（错误码 {code}）——"
                f"换一个键，或在那些程序里改掉它的热键")
    return f"热键 {spec} 注册失败（错误码 {code}）——先关掉别的实例再试"


class _HotkeyMutex:
    """跨进程占位：一个按热键命名的互斥体，只做「名字归谁」的判定。

    为什么不用 WaitForSingleObject 持有所有权：我们不需要串行化任何操作，
    只要「这个名字同一时刻只有一个实例能拿到」。`CreateMutexW` 的
    `ERROR_ALREADY_EXISTS` 判定是系统原子的 —— 两个实例同时注册，必然
    一个拿到新对象、另一个拿到「已存在」，不存在两边都成功的窗口。

    生命周期：句柄留在对象里，`release()` 关掉；进程正常退出、异常退出、
    被 TerminateProcess 强杀时，内核都会关掉它所有的句柄 —— 名字随最后一个
    句柄消失而销毁。所以**不会留下永久占位**，下一个实例立刻能注册。

    注意：我们从不持有所有权（`bInitialOwner=False`，也不 Wait），因此
    「被抛弃的互斥体（abandoned）」这个概念在这里不适用 —— 只要句柄还在，
    就一定有活着的实例在用这个键。
    """

    __slots__ = ("name", "_handle", "holder", "_map", "_view", "where")

    def __init__(self, name):
        self.name = name
        self._handle = None
        self.holder = None      # 抢占失败时读到的占用者信息
        self.where = "none"     # local / external / none
        self._map = None
        self._view = None

    def acquire(self):
        """返回 (True, 0) = 占住；否则 (False, 错误码)。

        失败时 `self.holder` / `self.where` 会填好，供上层生成可排查的文案。
        """
        ctypes.set_last_error(0)
        handle = _kernel32.CreateMutexW(None, False, self.name)
        err = ctypes.get_last_error()
        if not handle:
            return False, err                   # 创建失败（权限之类），交给上层报
        if err == _ERROR_ALREADY_EXISTS:
            # 这次只是为了探明「有人在用」，别把这个句柄留着 —— 多留一个
            # 句柄会把名字撑住，对方退出后我们反而成了新的占位者。
            _kernel32.CloseHandle(handle)
            with _LOCAL_LOCK:
                self.where = "local" if self.name in _LOCAL_HELD else "external"
            if self.where == "external":
                parsed = self._parse_name()
                if parsed:
                    self.holder = _read_holder(*parsed)
            return False, err
        self._handle = handle
        with _LOCAL_LOCK:
            _LOCAL_HELD[self.name] = os.getpid()
        self.where = "none"
        parsed = self._parse_name()
        if parsed:
            self._map, self._view = _write_holder(*parsed)
        return True, 0

    def _parse_name(self):
        """从 `Local\\dafors.hotkey.{mods}.{vk}` 反解 (mods, vk)。"""
        try:
            tail = self.name.rsplit(".", 2)[-2:]
            return int(tail[0], 16), int(tail[1], 16)
        except Exception:                    # noqa: BLE001
            return None

    def release(self):
        handle, self._handle = self._handle, None
        view, self._view = self._view, None
        mp, self._map = self._map, None
        with _LOCAL_LOCK:
            if _LOCAL_HELD.get(self.name) == os.getpid():
                _LOCAL_HELD.pop(self.name, None)
        if view:
            _kernel32.UnmapViewOfFile(view)
        if mp:
            _kernel32.CloseHandle(mp)
        if handle:
            # 没持有所有权，关句柄就等于放开名字；必须 CloseHandle，
            # 只 ReleaseMutex 不关句柄名字会一直存在到进程结束。
            _kernel32.CloseHandle(handle)


_VK = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "ESC": 0x1B, "PAUSE": 0x13, "SCROLLLOCK": 0x91, "INSERT": 0x2D,
}


def parse(spec):
    """把 "F8" / "Ctrl+F8" / "Alt+Shift+F9" 解析成 (mods, vk)。认不出返回 None。"""
    if not spec:
        return None
    parts = [p.strip().upper() for p in str(spec).replace("＋", "+").split("+") if p.strip()]
    if not parts:
        return None
    key = parts[-1]
    vk = _VK.get(key)
    if vk is None:
        return None
    mods = 0
    for p in parts[:-1]:
        if p in ("CTRL", "CONTROL"):
            mods |= _MOD_CONTROL
        elif p == "ALT":
            mods |= _MOD_ALT
        elif p == "SHIFT":
            mods |= _MOD_SHIFT
        elif p in ("WIN", "META", "SUPER"):
            mods |= _MOD_WIN
        else:
            return None
    return mods, vk


# ---------------- 低级键盘钩子（首选） ----------------
# 为什么不再靠 RegisterHotKey：游戏全屏独占键盘时，系统热键经常收不到 ——
# 注册是成功的（日志写着「F7 已就绪」），按下去却毫无反应，用户反复踩到。
# 低级钩子在输入栈更底层，游戏吃键盘也照样能看见。
_WH_KEYBOARD_LL = 13
_WM_KEYDOWN = 0x0100
_WM_SYSKEYDOWN = 0x0104


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                               wintypes.WPARAM, wintypes.LPARAM)

# **必须显式声明参数类型。** 不声明时 ctypes 按 c_int 传，64 位的
# lParam 直接溢出（实测报 "OverflowError: int too long to convert"），
# CallNextHookEx 一抛异常，钩子链就断了 —— 后面所有按键都可能卡住。
_user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                   wintypes.WPARAM, wintypes.LPARAM]
_user32.CallNextHookEx.restype = ctypes.c_ssize_t
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC,
                                      ctypes.c_void_p, wintypes.DWORD]
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_user32.UnhookWindowsHookEx.restype = wintypes.BOOL
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p,
                                wintypes.UINT, wintypes.UINT]
_user32.GetMessageW.restype = ctypes.c_int     # -1 = 出错，不能用 BOOL

# 修饰键的虚拟键码（和 _MOD_* 一一对应）
_VK_SHIFT, _VK_CONTROL, _VK_MENU = 0x10, 0x11, 0x12
_VK_LWIN, _VK_RWIN = 0x5B, 0x5C

_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = wintypes.SHORT


def _mods_now():
    """此刻按下的修饰键集合，位定义与 parse() 的返回值一致。

    低级钩子只喂给我们「哪个键被按了」，修饰键状态要自己查。用
    GetAsyncKeyState 的高位（0x8000 = 当前按下）而不是低位（自上次读取后
    按过），因为我们要的是「这一击发生时的状态」。
    """
    down = 0
    for vk, bit in ((_VK_CONTROL, _MOD_CONTROL), (_VK_MENU, _MOD_ALT),
                    (_VK_SHIFT, _MOD_SHIFT), (_VK_LWIN, _MOD_WIN),
                    (_VK_RWIN, _MOD_WIN)):
        if _user32.GetAsyncKeyState(vk) & 0x8000:
            down |= bit
    return down


def _matches(mods, vk, kb_vk):
    """这一击算不算这个热键：键码相同 **且** 修饰键完全一致。

    必须完全一致。以前只比 vkCode，`F8` 和 `Ctrl+F8` 是同一个键，两个实例
    （或同一个实例的「停止 / 开抢」）会同时响应 —— 按一下 F8 既停又开抢。
    按精确匹配后，`F8` 与 `Ctrl+F8` 是两件事，互斥体的 (mods, vk) 命名也
    才对得上 RegisterHotKey 那一路的语义。
    """
    return kb_vk == vk and _mods_now() == mods


class HookHotkey:
    """低级键盘钩子版热键。**只监听不拦截**（CallNextHookEx 原样放行）。

    和 `Hotkey`（RegisterHotKey）接口一致：start() 返回 (ok, msg)。
    组合键按**精确修饰键匹配**：`F8` 与 `Ctrl+F8` 是两个独立热键，互不
    触发，也各有各的跨实例互斥名。
    """

    def __init__(self, spec="F8", callback=None, log=None):
        self.spec = spec
        self.callback = callback
        self.log = log
        self._thread = None
        self._tid = None
        self._proc = None       # **必须保住引用**，被 GC 掉会直接崩进程
        self._hook = None
        self._ok = False
        self._msg = "未启动"
        self._last_fire = 0.0
        self._mutex = None      # 跨进程占位（命名互斥体），见 _HotkeyMutex
        self._conflict = False  # start() 失败是不是因为「别的实例占着」
        # 状态字段被三个线程读写（钩子线程、stop() 调用方、status() 读方）
        self._lock = threading.RLock()

    def _dispatch(self, fn):
        """把回调挪到工作线程执行。

        **低级钩子线程里不能做重活。** 回调超时（注册表
        LowLevelHooksTimeout，默认 300ms）系统会静默把钩子摘掉，之后按
        什么键都没反应 —— 表现就是「急停失灵」，而且完全不报错。
        所以钩子线程只负责置标志 + 起线程，回调本身在工作线程里跑。
        """
        def run():
            try:
                if self.log:
                    self.log(f"钩子收到 {self.spec}")
            except Exception:                     # noqa: BLE001
                pass
            try:
                if fn:
                    fn()
            except Exception:                     # noqa: BLE001
                pass

        t = threading.Thread(target=run, name="hotkey-fire", daemon=True)
        t.start()
        return t

    def _fire(self):
        # 按住不放时系统会连续发 KEYDOWN，去重 0.4 秒
        now = time.perf_counter()
        with self._lock:
            if now - self._last_fire < 0.4:
                return
            self._last_fire = now
        self._dispatch(self.callback)

    def start(self):
        if self._thread and self._thread.is_alive():
            return True, "已经在监听"
        # 上一轮线程若已死却没走 stop()/status()，名字可能还在本进程手里；
        # 先自己放开，否则 CreateMutexW 会撞上「自己」，误报成别的实例占用。
        self._release_mutex()
        parsed = parse(self.spec)
        if not parsed:
            return False, f"认不出热键「{self.spec}」，用 F8 / F7 这种写法"
        mods, vk = parsed

        # **先抢跨进程占位，再装钩子。** 低级钩子本身没有「独占」概念：
        # 两个实例各装一个都会成功，两边都收到按键、都去执行抢购 —— 这就是
        # 双触发。命名互斥体负责补上这一层，第二个实例在这里就失败。
        guard = _HotkeyMutex(_mutex_name(mods, vk))
        held, code = guard.acquire()
        if not held:
            self._conflict = True
            msg = _msg_occupied(self.spec, code, guard.where, guard.holder)
            with self._lock:
                self._ok = False
                self._msg = msg
            return False, msg
        self._conflict = False
        with self._lock:
            self._mutex = guard
        ready = threading.Event()

        def loop():
            self._tid = _kernel32.GetCurrentThreadId()

            def handler(n_code, w_param, l_param):
                if n_code == 0 and w_param in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                    kb = ctypes.cast(
                        l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                    # 精确匹配修饰键；只比 vkCode 会让 F8 / Ctrl+F8 双触发。
                    if _matches(mods, vk, kb.vkCode):
                        self._fire()
                return _user32.CallNextHookEx(None, n_code, w_param, l_param)

            self._proc = _HOOKPROC(handler)
            ctypes.set_last_error(0)
            hook = _user32.SetWindowsHookExW(_WH_KEYBOARD_LL, self._proc, None, 0)
            ok = bool(hook)
            with self._lock:
                self._hook = hook
                self._ok = ok
                self._msg = (f"热键 {self.spec} 已就绪（低级钩子）" if ok else
                             f"热键 {self.spec} 钩子安装失败"
                             f"（错误码 {ctypes.get_last_error()}）")
            if not ok:
                # 装不上钩子就把名字放掉：上层会退回 RegisterHotKey，
                # 那条路要用同一个名字；不放的话回退也一定失败。
                self._release_mutex()
            ready.set()
            if not ok:
                return
            msg = wintypes.MSG()
            while True:
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0:                    # WM_QUIT
                    break
                if ret == -1:                   # 出错：别静默退出
                    with self._lock:
                        self._ok = False
                        self._msg = (f"热键 {self.spec} 消息循环出错"
                                     f"（{ctypes.get_last_error()}）")
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
            with self._lock:
                hook, self._hook = self._hook, None
                self._ok = False
                if self._msg.startswith("热键") and "已就绪" in self._msg:
                    self._msg = f"热键 {self.spec} 钩子已退出"
            self._release_mutex()
            if hook:
                _user32.UnhookWindowsHookEx(hook)

        self._thread = threading.Thread(target=loop, name="hotkey-hook", daemon=True)
        self._thread.start()
        ready.wait(timeout=2.0)
        with self._lock:
            return self._ok, self._msg

    def _release_mutex(self):
        """放开跨进程占位。可重入：没有占位时什么都不做。

        钩子线程退出和 stop() 都会调它，谁先到谁放；后到的一次看到
        `_mutex` 已经是 None，直接返回 —— 不会重复 CloseHandle。
        """
        with self._lock:
            guard, self._mutex = self._mutex, None
        if guard is not None:
            guard.release()

    def stop(self):
        tid = self._tid
        if tid:
            _user32.PostThreadMessageW(tid, _WM_QUIT, 0, 0)
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
        # 线程可能没来得及退（join 超时），所以这里主动放一次名字 ——
        # 否则 stop() 之后另一个实例还是注册不上，直到本进程结束。
        self._release_mutex()
        with self._lock:
            self._tid = None
            self._ok = False
            # 句柄交给钩子线程自己清（loop 退出时会 Unhook 并把 _hook 置 None）；
            # 这里直接清掉的话，线程晚一点退出就少一次 UnhookWindowsHookEx。
            self._msg = "已停止"
        return True

    def status(self):
        """热键此刻真的还挂着吗？

        **不能只看本地 flag**：钩子被系统摘掉（回调超时、线程没了）时
        `_ok` 还是 True，UI 就会一直显示「已就绪」。这里回读系统状态：
        句柄还在、线程还活着、线程的消息队列还收得到 PostThreadMessage。
        """
        with self._lock:
            msg = self._msg
        thread = self._thread
        alive = bool(thread and thread.is_alive())
        with self._lock:
            hook = self._hook
            ok = self._ok
            tid = self._tid
        live = bool(ok and hook and alive)
        if ok and not alive:
            # 线程异常死亡（故障注入 / 回调把线程带崩）：句柄和名字都已失效，
            # 但 `_ok` 还停在 True，msg 还是「已就绪（低级钩子）」——
            # 界面会显示「未生效 · 热键 F8 已就绪」，自相矛盾。这里一起改掉。
            live = False
            with self._lock:
                self._ok = False
                self._msg = f"热键 {self.spec} 钩子线程已退出（已释放占用）"
            msg = self._msg
        if live and tid:
            # 消息队列没了 = 线程已被系统回收（GetMessage 已返回）
            if not _user32.PostThreadMessageW(tid, _WM_NULL, 0, 0):
                live = False
                with self._lock:
                    self._ok = False
                    self._msg = f"热键 {self.spec} 钩子已失效（消息队列没了）"
                msg = self._msg
        if not live and not alive:
            # 只有确认线程已经没了才放：start() 正在装钩子的那一小段里
            # `_ok` 还是 False，这时放名字会开出一个「两边都注册」的窗口。
            # 钩子已经不在监听了，也别继续攥着名字 —— 否则别的实例被这个
            # 空壳挡在外面，用户看到的却是「热键被占用」，实际没人响应。
            self._release_mutex()
        return {"spec": self.spec, "active": bool(live), "msg": msg}


class Hotkey:
    """一个全局热键。start() 返回 (ok, msg)，失败时 msg 说明原因。"""

    _next_id = 0x5A00

    def __init__(self, spec="F8", callback=None, log=None):
        self.spec = spec
        self.callback = callback
        self.log = log
        self._thread = None
        self._tid = None
        self._id = 0
        self._ok = False
        self._msg = "未启动"
        self._mutex = None      # 和 HookHotkey 共用同一套跨实例判定
        self._conflict = False
        self._lock = threading.RLock()

    def _dispatch(self, fn):
        """回调挪到工作线程，别卡住消息循环。"""
        def run():
            try:
                if fn:
                    fn()
            except Exception:                     # noqa: BLE001
                pass

        threading.Thread(target=run, name="hotkey-fire", daemon=True).start()

    def start(self):
        if self._thread and self._thread.is_alive():
            return True, "已经在监听"
        # 同上：清掉上一轮残留的名字，别把自己当成「另一个实例」。
        self._release_mutex()
        parsed = parse(self.spec)
        if not parsed:
            return False, f"认不出热键「{self.spec}」，用 F8 / Ctrl+F8 这种写法"
        mods, vk = parsed

        # 和 HookHotkey 抢同一个名字：两个实现之间也必须互斥。否则一个实例
        # 走钩子、另一个回退到 RegisterHotKey，两条路各自「成功」却都在响应。
        guard = _HotkeyMutex(_mutex_name(mods, vk))
        held, code = guard.acquire()
        if not held:
            self._conflict = True
            msg = _msg_occupied(self.spec, code, guard.where, guard.holder)
            with self._lock:
                self._ok = False
                self._msg = msg
            return False, msg
        self._conflict = False
        with self._lock:
            self._mutex = guard

        Hotkey._next_id += 1
        self._id = Hotkey._next_id
        ready = threading.Event()

        def loop():
            self._tid = _kernel32.GetCurrentThreadId()
            ctypes.set_last_error(0)
            ok = _user32.RegisterHotKey(None, self._id, mods, vk)
            with self._lock:
                self._ok = bool(ok)
                if not ok:
                    code_ = ctypes.get_last_error()
                    self._msg = _msg_occupied(self.spec, code_, "external",
                                              guard.holder)
                else:
                    self._msg = f"热键 {self.spec} 已就绪"
            if not ok:
                # 系统那边没注册上，名字也得放掉 —— 否则一次失败会把
                # 这个键一直锁死到进程结束。
                self._release_mutex()
            ready.set()
            if not ok:
                return
            msg = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == _WM_HOTKEY and msg.wParam == self._id:
                    self._dispatch(self.callback)
                elif msg.message == _WM_QUIT:
                    break
            _user32.UnregisterHotKey(None, self._id)
            with self._lock:
                self._ok = False
                if "已就绪" in self._msg:
                    self._msg = f"热键 {self.spec} 已退出"
            self._release_mutex()

        self._thread = threading.Thread(target=loop, name="hotkey-msg", daemon=True)
        self._thread.start()
        ready.wait(timeout=2.0)
        with self._lock:
            return self._ok, self._msg

    def _release_mutex(self):
        """放开跨实例占位（可重入）。消息循环退出和 stop() 谁先到谁放。"""
        with self._lock:
            guard, self._mutex = self._mutex, None
        if guard is not None:
            guard.release()

    def stop(self):
        tid = self._tid
        if tid:
            _user32.PostThreadMessageW(tid, _WM_QUIT, 0, 0)
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
        self._release_mutex()
        with self._lock:
            self._tid = None
            self._ok = False
            self._msg = "已停止"
        return True

    def status(self):
        """回读线程状态：线程死了就不可能还在监听。"""
        thread = self._thread
        alive = bool(thread and thread.is_alive())
        with self._lock:
            ok = self._ok and alive
            if self._ok and not alive:
                self._ok = False
                self._msg = f"热键 {self.spec} 线程已退出"
            msg = self._msg
        if not ok and not alive:
            # 同上：start() 进行中（线程活着但还没注册完）不能放手。
            # 线程没了还攥着名字，会让下一个实例误以为键被一个空壳占着。
            self._release_mutex()
        return {"spec": self.spec, "active": bool(ok), "msg": msg}
