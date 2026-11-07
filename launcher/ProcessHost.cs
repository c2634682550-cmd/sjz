// 子进程宿主：拉起 pythonw、守护、按策略重启、退出时收干净。
// 所有子进程都塞进同一个 Job 对象（KILL_ON_JOB_CLOSE），所以即便启动器
// 被任务管理器强杀，控制台和它拉起的 Edge/webview 也不会变成孤儿。
namespace SjzLauncher
{
    using System;
    using System.Collections.Generic;
    using System.Diagnostics;
    using System.IO;
    using System.Threading;

    internal sealed class PythonHost
    {
        /// <summary>停止结果（界面必须照着它回写，不能静默保持「运行中」）。</summary>
        public sealed class StopOutcome
        {
            public bool Ok;
            public bool Forced;         // 是否走到强制终止
            public bool NeedAdmin;      // 权限不足，需要以管理员身份停止
            public int ErrorCode;       // 0 = 无错误；非 0 是 Win32 错误码
            public string Reason = "";
            public long ElapsedMs;
            public int Pid;
        }

        public event Action<string> OnLog;
        public event Action<string, int> OnExit;      // (kind, code)
        public event Action<string> OnState;

        private readonly LauncherConfig _cfg;
        private readonly string _token;
        private volatile bool _stopping;
        private Process _proc;
        private Thread _watcher;
        private int _restarts;
        private DateTime _startedAt = DateTime.MinValue;

        public int Pid { get { return _proc == null ? 0 : _proc.Id; } }
        public int CtrlPort { get; private set; }

        public PythonHost(LauncherConfig cfg, string token)
        {
            _cfg = cfg;
            _token = token;
        }

        public bool Running
        {
            get { return _proc != null && Native.ProcessAlive(_proc.Id); }
        }

        private void Log(string msg)
        {
            Action<string> h = OnLog;
            if (h != null) h(msg);
        }

        private string PythonPath()
        {
            string p = _cfg.Python;
            if (!Path.IsPathRooted(p)) p = Path.Combine(AppPaths.Root, p);
            if (File.Exists(p)) return p;
            string fallback = Path.Combine(AppPaths.Root, @"runtime\pythonw.exe");
            return File.Exists(fallback) ? fallback : null;
        }

        // PyInstaller onedir 形态里没有 runtime\pythonw.exe，主程序 SJZ-Control.exe
        // 与 _internal 同级。两种形态都要能拉起。
        private string FrozenExePath()
        {
            string exe = Path.Combine(AppPaths.Root, "SJZ-Control.exe");
            if (File.Exists(exe)) return exe;
            // 启动器被塞进子目录的异常布局，主程序仍在上层
            string up = Path.GetFullPath(Path.Combine(AppPaths.Root, "..", "SJZ-Control.exe"));
            return File.Exists(up) ? up : null;
        }

        private string ResolveTarget(out bool frozen, out string error)
        {
            frozen = false;
            error = null;
            string python = PythonPath();
            if (python != null && File.Exists(Path.Combine(AppPaths.Root, _cfg.Script)))
                return python;                       // 旧路径：仓内 runtime + desktop.py
            string exe = FrozenExePath();
            if (exe != null)
            {
                frozen = true;                       // frozen：直接拉 onedir 主程序
                return exe;
            }
            error = python == null
                ? @"找不到 runtime\pythonw.exe，也没有 SJZ-Control.exe（安装包不完整）"
                : "找不到 " + _cfg.Script + "（安装包不完整）";
            return null;
        }

        public bool Start(out string error)
        {
            error = null;
            _stopping = false;
            _restarts = 0;
            if (!Native.EnsureJob()) Log("!! 建 Job 对象失败，孤儿兜底不可用");
            bool ok = StartCore(out error);
            if (ok)
            {
                _watcher = new Thread(WatchLoop);
                _watcher.IsBackground = true;
                _watcher.Name = "python-watch";
                _watcher.Start();
            }
            return ok;
        }

        private bool StartCore(out string error)
        {
            error = null;
            bool frozen;
            string target = ResolveTarget(out frozen, out error);
            if (target == null) return false;

            int selfPid = Process.GetCurrentProcess().Id;
            string ctrlArgs = string.Format(
                "--launcher-pid {0} --ctrl-token {1} --port {2} --host 127.0.0.1 --elevated",
                selfPid, _token, _cfg.CtrlPort);
            // frozen 的主程序自己就是入口，不用再带桌面脚本名
            string args = frozen ? ctrlArgs : _cfg.Script + " " + ctrlArgs;

            var psi = new ProcessStartInfo(target, args);
            psi.WorkingDirectory = AppPaths.Root;    // 项目根，不是 launcher\
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;               // pythonw，本来就没控制台
            psi.WindowStyle = ProcessWindowStyle.Hidden;

            // 线程预算双保险：frozen 包里 desktop.py 的预算代码要等
            // PyInstaller bootloader 起来才执行，这里先在子进程环境把
            // OMP/ORT 压住，import 阶段就不会按 16 核开线程池。
            int threads = Math.Max(1, Math.Min(4, Environment.ProcessorCount / 2));
            string envThreads = Environment.GetEnvironmentVariable("SJZ_THREADS");
            if (!string.IsNullOrEmpty(envThreads))
            {
                int n;
                if (int.TryParse(envThreads, out n) && n > 0) threads = n;
            }
            foreach (string key in new[] { "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                                           "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                                           "VECLIB_MAXIMUM_THREADS", "ORT_NUM_THREADS",
                                           "OMP_THREAD_LIMIT" })
                psi.EnvironmentVariables[key] = threads.ToString();

            try
            {
                _proc = Process.Start(psi);
                if (_proc == null)
                {
                    error = "CreateProcess 返回空";
                    return false;
                }
                Native.AssignToJob(_proc.Handle);
                // venv 的 pythonw.exe 可能只是个壳：真解释器是它的子进程。
                // 以 Job 里那个新 pid 为准，否则守护/重启/控制面发现全会对不上。
                // frozen 的 onedir 主程序自己就是 bootloader，没有这层壳。
                if (!frozen)
                {
                    int real = Native.FindChildPid(_proc.Id, 1500);
                    if (real <= 0) real = Native.FindNewChild(_proc.Id, 200);
                    int shell = _proc.Id;
                    if (real > 0 && real != shell)
                    {
                        try
                        {
                            _proc = Process.GetProcessById(real);
                            Log(string.Format("pythonw 是壳进程，改跟真实解释器 pid={0}（壳 {1}）",
                                              real, shell));
                        }
                        catch (Exception) { }
                    }
                }
                _startedAt = DateTime.UtcNow;
                CtrlPort = 0;
                Log(string.Format("已拉起控制台 pid={0}（{1}；{2}）", _proc.Id,
                                  frozen ? "frozen" : "runtime",
                                  string.Join(" ", new[] { Path.GetFileName(target),
                                               "--launcher-pid", selfPid.ToString(),
                                               "--port", _cfg.CtrlPort.ToString() })));
                Action<string> st = OnState;
                if (st != null) st("running");
                return true;
            }
            catch (Exception e)
            {
                error = "拉起控制台失败：" + e.Message;
                return false;
            }
        }

        private void WatchLoop()
        {
            while (true)
            {
                Thread.Sleep(500);
                Process p = _proc;
                if (p == null) return;
                bool exited;
                try { exited = p.HasExited; }
                catch (Exception) { exited = true; }
                if (!exited) continue;
                if (_stopping) return;

                int code = -1;
                try { code = p.ExitCode; } catch (Exception) { }
                double uptime = (DateTime.UtcNow - _startedAt).TotalSeconds;
                try { p.Dispose(); } catch (Exception) { }
                _proc = null;
                CtrlPort = 0;
                Log(string.Format("控制台已退出 code={0}，运行 {1:0}s", code, uptime));

                Action<string, int> eh = OnExit;
                if (eh != null) eh("exit", code);

                if (code == 0)
                {
                    Log("控制台正常退出（用户关闭窗口），不做重启");
                    Action<string> st0 = OnState;
                    if (st0 != null) st0("stopped");
                    return;
                }
                if (uptime > 600) _restarts = 0;          // 稳定跑过 10 分钟，计数清零
                if (_restarts >= _cfg.MaxRestarts)
                {
                    Log(string.Format("!! 已连续异常退出 {0} 次，达到上限，停止重启", _restarts));
                    Action<string> st1 = OnState;
                    if (st1 != null) st1("failed");
                    return;
                }
                _restarts++;
                int delay = Math.Min(1000 << (_restarts - 1), 8000);
                Log(string.Format("第 {0}/{1} 次重启，退避 {2}ms", _restarts, _cfg.MaxRestarts, delay));
                Thread.Sleep(delay);
                if (_stopping) return;
                string err;
                if (!StartCore(out err)) Log("!! 重启失败：" + err);
            }
        }

        /// <summary>
        /// 停止：① 请控制面自己退（1.2s）→ ② 给窗口发 WM_CLOSE 再等 2s → ③ 强制终止。
        /// 每一步都核对进程是否真的没了；权限不足时返回 NeedAdmin（错误码 5），
        /// 由界面提示「以管理员身份停止」，不再静默显示「运行中」。
        /// </summary>
        public StopOutcome StopEx(int graceMs)
        {
            var r = new StopOutcome();
            var sw = Stopwatch.StartNew();
            _stopping = true;
            Process p = _proc;
            if (p == null)
            {
                r.Ok = true;
                r.Reason = "没有在运行";
                return r;
            }
            r.Pid = p.Id;
            if (!Native.ProcessAlive(p.Id))
            {
                r.Ok = true;
                r.Reason = "进程已退出";
                _proc = null;
                return r;
            }

            // ① 控制面（能保住收尾动作的唯一路径）
            if (_token != null && _token.Length > 0)
            {
                if (CtrlPort == 0)
                {
                    JsonNode st;
                    CtrlPort = ControlPlane.Discover(p.Id, _token, _cfg.CtrlPort, 20, out st);
                }
                if (CtrlPort > 0)
                {
                    bool sent = ControlPlane.PostStop(CtrlPort, _token, 1200);
                    Log(sent ? "已请求控制台停止" : "请求控制台停止失败（控制面无应答）");
                }
            }
            if (graceMs > 0 && Native.WaitExit(p.Id, Math.Min(graceMs, 3000)))
            {
                r.Ok = true;
                r.Reason = "已优雅退出";
                Native.ClearInstanceIfMatches(p.Id);
                _proc = null;
                r.ElapsedMs = sw.ElapsedMilliseconds;
                return r;
            }

            // ② 窗口还在 → 先关窗口，再给 2 秒（覆盖「点了 × 但还活着」以外的常见情况）
            int windows = Native.PostCloseToPid(p.Id);
            if (windows > 0)
            {
                Log("已给控制台窗口发关闭请求（" + windows + " 个窗口）");
                if (Native.WaitExit(p.Id, 2000))
                {
                    r.Ok = true;
                    r.Reason = "关闭窗口后已退出";
                    Native.ClearInstanceIfMatches(p.Id);
                    _proc = null;
                    r.ElapsedMs = sw.ElapsedMilliseconds;
                    return r;
                }
            }
            else
            {
                Log("控制台没有窗口（窗口已销毁但进程还在），直接终止进程");
            }

            // ③ 强制终止整棵树
            int killed;
            int err = Native.TerminateTree(p.Id, out killed);
            Native.WaitExit(p.Id, 800);
            r.Forced = true;
            if (Native.ProcessAlive(p.Id))
            {
                r.Ok = false;
                r.ErrorCode = err != 0 ? err : 5;
                r.NeedAdmin = r.ErrorCode == 5 || r.ErrorCode == 6;
                r.Reason = r.NeedAdmin
                    ? "权限不足（错误码 " + r.ErrorCode + "），需要以管理员身份停止"
                    : "终止失败（错误码 " + r.ErrorCode + "）";
            }
            else
            {
                r.Ok = true;
                r.Reason = "已强制结束（" + killed + " 个进程）";
                Native.ClearInstanceIfMatches(p.Id);
                _proc = null;
            }
            r.ElapsedMs = sw.ElapsedMilliseconds;
            return r;
        }

        /// <summary>
        /// 停止登记在 instance.json 里的「遗留控制台」（启动器重启后不知道它的 token，
        /// 只能走窗口 → 终止 → 权限 三级），返回结果。
        /// </summary>
        public static StopOutcome StopForeignPid(int pid)
        {
            var r = new StopOutcome { Pid = pid };
            var sw = Stopwatch.StartNew();
            if (pid <= 0 || !Native.ProcessAlive(pid))
            {
                r.Ok = true;
                r.Reason = "进程已不存在";
                if (pid > 0) Native.ClearInstanceIfMatches(pid);
                return r;
            }
            int windows = Native.PostCloseToPid(pid);
            if (windows > 0 && Native.WaitExit(pid, 2000))
            {
                r.Ok = true;
                r.Reason = "关闭窗口后已退出";
                Native.ClearInstanceIfMatches(pid);
                r.ElapsedMs = sw.ElapsedMilliseconds;
                return r;
            }
            int killed;
            int err = Native.TerminateTree(pid, out killed);
            Native.WaitExit(pid, 800);
            r.Forced = true;
            if (Native.ProcessAlive(pid))
            {
                r.Ok = false;
                r.ErrorCode = err != 0 ? err : 5;
                r.NeedAdmin = r.ErrorCode == 5 || r.ErrorCode == 6;
                r.Reason = r.NeedAdmin
                    ? "权限不足（错误码 " + r.ErrorCode + "），需要以管理员身份停止"
                    : "终止失败（错误码 " + r.ErrorCode + "）";
            }
            else
            {
                r.Ok = true;
                r.Reason = "已强制结束";
                Native.ClearInstanceIfMatches(pid);
            }
            r.ElapsedMs = sw.ElapsedMilliseconds;
            return r;
        }

        /// <summary>兼容旧调用：返回是否走到了强杀。</summary>
        public bool StopAndWait(int graceMs)
        {
            StopOutcome r = StopEx(graceMs);
            return r.Forced;
        }

        public void RefreshStatus(out JsonNode status)
        {
            status = null;
            Process p = _proc;
            if (p == null || _token == null) return;
            if (CtrlPort == 0)
                CtrlPort = ControlPlane.Discover(p.Id, _token, _cfg.CtrlPort, 20, out status);
            else
                status = ControlPlane.StatusFast(CtrlPort, _token);
        }
    }

    /// <summary>
    /// 自测用：把任意子进程塞进 Job 跑，用来验证「退出后被感知 / 重启 / 无孤儿」。
    /// 只在命令行模式下用，不碰 GUI。
    /// </summary>
    internal static class SpawnTest
    {
        public static int Run(string exe, string[] childArgs, int seconds,
                               int maxRestarts, int backoffMs, bool keepChildren)
        {
            if (!Native.EnsureJob())
            {
                Console.WriteLine("{\"event\":\"error\",\"msg\":\"job\"}");
                return 2;
            }
            int spawns = 0;
            var pids = new List<int>();
            Process proc = null;
            DateTime end = DateTime.Now.AddSeconds(seconds > 0 ? seconds : 3);
            int restarts = 0;

            while (true)
            {
                if (proc == null)
                {
                    try
                    {
                        var psi = new ProcessStartInfo(exe, JoinArgs(childArgs));
                        psi.UseShellExecute = false;
                        psi.CreateNoWindow = true;
                        psi.WorkingDirectory = AppPaths.Root;
                        proc = Process.Start(psi);
                        Native.AssignToJob(proc.Handle);
                        spawns++;
                        pids.Add(proc.Id);
                        Console.WriteLine("{\"event\":\"spawn\",\"pid\":" + proc.Id + ",\"n\":" + spawns + "}");
                        Console.Out.Flush();
                    }
                    catch (Exception e)
                    {
                        Console.WriteLine("{\"event\":\"spawn_failed\",\"msg\":" + Quote(e.Message) + "}");
                        return 2;
                    }
                }

                if (proc.HasExited)
                {
                    int code = 0;
                    try { code = proc.ExitCode; } catch (Exception) { }
                    Console.WriteLine("{\"event\":\"exit\",\"pid\":" + proc.Id + ",\"code\":" + code + "}");
                    Console.Out.Flush();
                    proc.Dispose();
                    proc = null;
                    if (restarts >= maxRestarts) break;
                    restarts++;
                    Thread.Sleep(backoffMs);
                    continue;
                }
                if (DateTime.Now > end) break;
                Thread.Sleep(100);
            }

            if (proc != null && !proc.HasExited && !keepChildren)
            {
                Console.WriteLine("{\"event\":\"kill_tree\",\"pid\":" + proc.Id + "}");
                Native.TerminateAllChildren();
                Thread.Sleep(400);
            }
            Console.WriteLine("{\"event\":\"done\",\"spawns\":" + spawns +
                              ",\"pids\":[" + string.Join(",", pids.ConvertAll(x => x.ToString()).ToArray()) + "]}");
            Console.Out.Flush();
            return 0;
        }

        private static string JoinArgs(string[] args)
        {
            if (args == null || args.Length == 0) return "";
            var parts = new List<string>();
            foreach (string a in args)
                parts.Add(a.IndexOf(' ') >= 0 ? "\"" + a + "\"" : a);
            return string.Join(" ", parts.ToArray());
        }

        private static string Quote(string s)
        {
            return "\"" + (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
        }
    }
}
