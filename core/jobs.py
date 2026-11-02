"""长任务执行器：把「点鼠标那种活」挪到后台跑。

为什么要它：定位一件砖要抢前台、滚列表、跑 OCR，一次几秒到几十秒。
以前它直接在 HTTP handler 里同步跑 —— 浏览器转圈、没有进度、超时了
也不知道它在干什么，而它正在动用户的鼠标。

同一时刻只跑一个任务：鼠标只有一个，两个任务并行会互相点，还会抢前台。
忙的时候直接拒绝（而不是排队）—— 排队意味着用户点了之后过很久才
突然开始动鼠标，那比报错更吓人。
"""
import threading
import time
import traceback
import uuid
from collections import deque

_lock = threading.Lock()
_current = None                 # 正在跑的那个（含已结束的最后一个）
_jobs = {}                      # id -> job
_MAX_JOBS = 20


class Busy(Exception):
    """已经有一个动作在跑。"""


def _snapshot(job):
    return {"id": job["id"], "name": job["name"], "state": job["state"],
            "result": job["result"], "error": job["error"],
            "elapsed": round((job["finished"] or time.time()) - job["started"], 2),
            "log": list(job["log"])}


def submit(fn, name=""):
    """提交一个动作，返回 job id。

    fn(log) 里可以调 log("...") 写进度；返回值会原样放进 result。
    忙的时候抛 Busy。
    """
    global _current
    with _lock:
        if _current is not None and _current["state"] == "running":
            raise Busy(_current["name"] or "上一个动作")
        job = {"id": uuid.uuid4().hex[:8], "name": name, "state": "running",
               "result": None, "error": None, "started": time.time(),
               "finished": None, "log": deque(maxlen=60)}
        _current = job
        _jobs[job["id"]] = job
        # 只留最近若干个，避免长期开着越攒越多
        while len(_jobs) > _MAX_JOBS:
            for old in list(_jobs):
                if old != job["id"] and _jobs[old]["state"] != "running":
                    _jobs.pop(old, None)
                    break
            else:
                break
    threading.Thread(target=_run, args=(job, fn), daemon=True,
                     name=f"job-{job['id']}").start()
    return job["id"]


def _run(job, fn):
    def log(msg):
        job["log"].append(str(msg))

    try:
        job["result"] = fn(log)
        job["state"] = "done"
    except Exception as e:                    # noqa: BLE001
        job["error"] = f"{type(e).__name__}: {e}"
        job["state"] = "failed"
        for line in traceback.format_exc().splitlines()[-4:]:
            job["log"].append(line)
    finally:
        job["finished"] = time.time()


def status(job_id):
    """查一个任务的状态。不认识就返回 None。"""
    with _lock:
        job = _jobs.get(job_id)
        return _snapshot(job) if job else None


def busy():
    """现在有没有动作在跑。"""
    with _lock:
        return bool(_current is not None and _current["state"] == "running")


def current():
    """当前（或最后一个）任务的状态，给界面显示用。"""
    with _lock:
        return _snapshot(_current) if _current else None
