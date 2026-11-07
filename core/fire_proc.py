"""主进程侧的卡点进程包装（分支 B 结论落地）。

为什么必须独立进程：主进程里跑自旋时，GIL 会让它在满载下 p50 从 0.3ms
劣化到 5.8ms、p95 到 37.5ms；同一 target 交给独立进程（
`tools/fire_worker.py`）仍然是 p50 0.0003ms / p95 0.0006ms。根因是 GIL，
不是算力 —— 把自旋拉长到 40ms 无效。

IPC 传 `target`（`time.perf_counter()` 秒；QPC 全系统同轴，跨进程有效）。
发送抖动不影响精度：target 是绝对时刻，早发晚发都一样，只要在 target 前
送到就行。

状态语义（2026-11-07 修竞态后）：
- `dead` 只有一个来源：子进程**真的退出了**（或管道确认写坏）。带退出码
  和可读原因，和「暂停 / 停止 / 用户中止」三个控制态严格分开 —— 控制态
  是用户意图，不会被当成故障，也不触发重建。
- 探活不是「起进程时看一眼」：`fire()` 每次发送前都按状态 + `poll()` 判断，
  `ensure_ready()` 在抢购准备阶段做「检查 → 必要时重建」。
- 父进程探到存活到真正写进管道之间仍有死亡窗口：写失败时做**一次**明确
  的重建重试；若距 target 已不足 `min_rebuild_lead_s`，关键路径不重建，
  直接标记 dead 交给调用方降级（不引入新的阻塞等待）。
- 等结果带超时 → 不会死等；`close()` 幂等。
"""

import os
import queue
import subprocess
import sys
import threading
import time
from enum import Enum

_HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(os.path.dirname(_HERE), "tools", "fire_worker.py")


class FireState(str, Enum):
    """卡点进程状态。

    故意不是布尔：`dead` 是故障终态，`paused` / `stopped` / `aborted` 是
    三种**控制终态/暂态**，语义不同，恢复策略也不同。
    """

    STARTING = "starting"     # 刚拉起，还没被使用过
    READY = "ready"           # 进程活着，允许发送
    PAUSED = "paused"         # 用户暂停：进程还在，但不许发送
    STOPPED = "stopped"       # 用户停止任务：进程已收，不重建
    ABORTED = "aborted"       # 用户中止本轮：进程已收，不重建
    DEAD = "dead"             # 故障：子进程意外退出 / 管道写坏


# 控制态：不是故障，绝不触发重建。
_CONTROL_STATES = (FireState.PAUSED, FireState.STOPPED, FireState.ABORTED)


def _exit_text(code):
    """把 returncode 变成可读原因。"""
    if code is None:
        return "卡点进程状态未知（拿不到退出码）"
    if code == 0:
        return "卡点进程正常退出（exit=0）"
    if code < 0:
        return f"卡点进程被信号终止（signal={-code}）"
    return f"卡点进程异常退出（exit={code}）"


class FireProcess:
    def __init__(self, exe=None, worker=None, start_timeout=3.0,
                 min_rebuild_lead_s=0.05):
        self.state = FireState.STARTING
        self.error = ""
        self.exit_code = None
        self.dead_since = None
        self.restarts = 0
        self.fire_retries = 0          # 写失败 → 重建重试 的实际次数
        self.last_rebuild_s = 0.0      # 最近一次重建耗时
        self._exe = exe
        self._worker = worker
        self._start_timeout = float(start_timeout)
        self._min_rebuild_lead_s = float(min_rebuild_lead_s)
        self._q = queue.Queue()
        self._lock = threading.RLock()
        self._closed = False
        self.p = None
        self._spawn()

    # ---------------------------------------------------------------- 状态
    @property
    def dead(self):
        """故障终态。控制态（暂停/停止/中止）不算 dead。"""
        return self.state is FireState.DEAD

    def usable(self):
        """能不能马上发送：READY 且进程确实活着。"""
        if self._closed or self.state is not FireState.READY:
            return False
        return self.alive()

    def alive(self):
        """进程还在不在（只看 poll，不等结果）。

        注意 `alive()` 返回 True 不代表 fire 能成功 —— 管道可能已经坏了。
        """
        if self._closed or self.state in (FireState.DEAD, FireState.STOPPED,
                                          FireState.ABORTED):
            return False
        p = self.p
        if p is None:
            return False
        try:
            return p.poll() is None
        except Exception:                   # noqa: BLE001
            return False

    def status(self):
        """给排障/台账用的一行状态快照。"""
        return {
            "state": self.state.value,
            "exit_code": self.exit_code,
            "error": self.error,
            "restarts": self.restarts,
            "fire_retries": self.fire_retries,
            "alive": self.alive(),
        }

    # ------------------------------------------------------------ 生命周期
    def _popen(self):
        """起子进程的工厂。单独抽出来是为了测试能在这里注入「发送瞬间死亡」。"""
        return subprocess.Popen(
            [self._exe or sys.executable, self._worker or WORKER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1)

    def _spawn(self):
        """起一个 worker。失败只置 dead，不抛。"""
        self._q = queue.Queue()
        self.state = FireState.STARTING
        self.error = ""
        self.exit_code = None
        self.dead_since = None
        try:
            self.p = self._popen()
        except Exception as e:              # noqa: BLE001
            self.p = None
            self._mark_dead(None, f"启动卡点进程失败: {e}")
            return
        p = self.p
        threading.Thread(target=self._pump, args=(p, self._q),
                         daemon=True).start()
        # 退出看门狗：子进程什么时候死都能立刻标 dead，不依赖「起进程时看
        # 一眼」那一次探活。这正是隔离跑绿、全量回归红的那条竞态。
        threading.Thread(target=self._watch, args=(p,), daemon=True).start()
        if not self._probe():
            return
        self.state = FireState.READY

    def _watch(self, p):
        """子进程一退出就把 dead 置位，退出码与原因可读。"""
        try:
            code = p.wait()
        except Exception:                   # noqa: BLE001
            return
        with self._lock:
            if self._closed or p is not self.p:
                return                  # 已被 restart/close 接管的旧进程
            if self.state in (FireState.STOPPED, FireState.ABORTED):
                return                  # 用户主动收掉的，不是故障
            self._mark_dead(code, _exit_text(code))

    def _mark_dead(self, code, reason):
        """把故障终态置位。不抛异常，供看门狗/发送路径共用。"""
        with self._lock:
            self.state = FireState.DEAD
            if code is not None:
                self.exit_code = code
            self.error = reason
            if self.dead_since is None:
                self.dead_since = time.perf_counter()

    def _probe(self):
        """按状态判断能不能用：已退出就同步标 dead。返回是否还能发。"""
        if self.state is FireState.DEAD:
            return False
        p = self.p
        if p is None:
            self._mark_dead(None, "没有可用的卡点进程")
            return False
        try:
            code = p.poll()
        except Exception as e:              # noqa: BLE001
            self._mark_dead(None, f"探活卡点进程失败: {e}")
            return False
        if code is not None:
            self._mark_dead(code, _exit_text(code))
            return False
        return True

    def _terminate(self, timeout=None):
        p = getattr(self, "p", None)
        if p is None:
            return
        try:
            p.stdin.close()
        except Exception:                   # noqa: BLE001
            pass
        try:
            p.terminate()
            p.wait(timeout=self._start_timeout if timeout is None else timeout)
        except Exception:                   # noqa: BLE001
            try:
                p.kill()
            except Exception:               # noqa: BLE001
                pass

    def _rebuild_locked(self, attempts=1, timeout=0.5):
        """调用方必须已持有 `_lock`。返回是否起了个能用的。"""
        t0 = time.perf_counter()
        ok = False
        for i in range(max(1, attempts)):
            self._terminate(timeout=timeout)
            self._spawn()
            if self.state is FireState.READY and self.alive():
                self.restarts += 1
                ok = True
                break
            time.sleep(0.02 * (i + 1))
        self.last_rebuild_s = time.perf_counter() - t0
        return ok

    def restart(self, attempts=2):
        """卡点进程挂了就地重开。返回是否起了个能用的。

        只处理故障态；用户暂停/停止/中止的状态不会被这里悄悄恢复。
        """
        if self._closed:
            return False
        with self._lock:
            if self.state in _CONTROL_STATES:
                return False
            return self._rebuild_locked(attempts=attempts)

    def ensure_ready(self, rebuild=True, attempts=1):
        """抢购准备阶段调用：检查状态，必要时重建。

        这是唯一允许阻塞重建的入口；卡点前的关键路径只做非阻塞状态判断。
        """
        if self._closed or self.state in _CONTROL_STATES:
            return False
        with self._lock:
            if self.state is FireState.READY and self._probe():
                return True
            if not rebuild:
                return False
            return self._rebuild_locked(attempts=attempts)

    # ------------------------------------------------------- 控制态（非故障）
    def pause(self, reason="用户暂停"):
        """暂停：进程留着，但 `fire()` 一律返回 False，且不标 dead。"""
        with self._lock:
            if self._closed or self.state is FireState.DEAD:
                return False
            self.state = FireState.PAUSED
            self.error = reason
            return True

    def resume(self):
        """从暂停恢复；进程已经没了就返回 False（由调用方决定是否重建）。"""
        with self._lock:
            if self.state is not FireState.PAUSED:
                return self.state is FireState.READY
            if not self._probe():
                return False
            self.state = FireState.READY
            self.error = ""
            return True

    def stop(self, reason="用户停止"):
        """停止任务：收掉进程，语义是用户意图，不是故障。"""
        with self._lock:
            if self._closed:
                return
            self.state = FireState.STOPPED
            self.error = reason
            self._terminate(timeout=0.5)

    def abort(self, reason="用户中止本轮"):
        """中止本轮：同样收进程，但和「停止整个任务」区分开记状态。"""
        with self._lock:
            if self._closed:
                return
            self.state = FireState.ABORTED
            self.error = reason
            self._terminate(timeout=0.5)

    def close(self):
        if self._closed:
            return
        with self._lock:
            self._closed = True
            if self.state is not FireState.DEAD:
                self.state = FireState.STOPPED
            self._terminate()

    # ------------------------------------------------------------------ IPC
    def _pump(self, p, q):
        """读 worker 的 stdout。队列按进程实例传入，重启不会串到新队列。"""
        try:
            for line in p.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    q.put(float(line))
                except ValueError:
                    q.put(None)
        except Exception:                   # noqa: BLE001
            pass
        finally:
            q.put(None)

    def _write(self, target):
        self.p.stdin.write(f"{float(target):.9f}\n")
        self.p.stdin.flush()

    def _lead(self, target):
        try:
            return float(target) - time.perf_counter()
        except (TypeError, ValueError):
            return 0.0

    def fire(self, target):
        """把绝对触发时刻交给卡点进程。返回 False 表示这一发不可用。

        正常路径只有「状态判断 + 一次 poll + 写管道」，不睡眠、不等握手。
        唯一的阻塞是写失败后的一次重建重试，且只在距 target 还够
        `min_rebuild_lead_s` 时才做 —— 卡点前的关键路径不会因此多等。
        """
        if self._closed:
            return False
        with self._lock:
            if self.state in _CONTROL_STATES:
                return False
            if not self._probe():
                return False
            if self.state is not FireState.READY:
                return False
            try:
                self._write(target)
                return True
            except Exception as e:          # noqa: BLE001
                lead = self._lead(target)
                if lead <= self._min_rebuild_lead_s:
                    self._mark_dead(
                        None,
                        f"写入卡点进程失败: {e}（距 target 仅 {lead * 1000:.0f}ms，"
                        "关键路径不重建，直接降级）")
                    return False
                self._mark_dead(
                    None, f"写入卡点进程失败: {e}（距 target {lead * 1000:.0f}ms，"
                          "尝试一次重建重试）")
                if not self._rebuild_locked(attempts=1):
                    return False
                self.fire_retries += 1
                try:
                    self._write(target)
                    self.error = (f"发送失败后已重建重试一次并成功"
                                  f"（{self.last_rebuild_s * 1000:.0f}ms）")
                    return True
                except Exception as e2:     # noqa: BLE001
                    self._mark_dead(None, f"重建后写入仍然失败: {e2}")
                    return False

    def wait(self, timeout=1.0):
        """等这一发的实际触发时刻。超时/进程死 → None，调用方必须降级。"""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            p = self.p
            code = None
            if p is not None:
                try:
                    code = p.poll()
                except Exception:           # noqa: BLE001
                    code = None
            if code is not None:
                self._mark_dead(code, f"等卡点结果超时，且子进程已退出（exit={code}）")
            else:
                self._mark_dead(None, "等卡点结果超时")
            return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
