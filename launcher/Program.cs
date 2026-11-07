// 入口 + 路径 + 配置 + 命令行模式。
// 两种编译产物：
//   SjzLauncher.exe      /target:winexe + asInvoker 清单（正式，日常不提权）
//   SjzLauncherTest.exe  /target:exe   + asInvoker 清单（自动化测试用，只跑 CLI）
namespace SjzLauncher
{
    using System;
    using System.Collections.Generic;
    using System.Diagnostics;
    using System.IO;
    using System.Reflection;
    using System.Runtime.InteropServices;
    using System.Text;
    using System.Windows.Forms;

    // 版本常量在 Version.g.cs（由 tools/version_sync.py 从 core/version.py 生成）

    internal static class AppPaths
    {
        public static string ExeDir;
        public static string Root;
        public static string DataDir;
        public static string LogFile;
        public static string ConfigPath;
        public static int Pid;

        public static void Init()
        {
            ExeDir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            Root = string.Equals(Path.GetFileName(ExeDir), "launcher", StringComparison.OrdinalIgnoreCase)
                ? Path.GetDirectoryName(ExeDir)
                : ExeDir;                                  // 兼容把 exe 放根目录的部署
            DataDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "SJZ-Control");
            // 便携/测试用：SJZ_DATA_DIR 可把卡密、日志、更新暂存整体挪到别处。
            string dataEnv = Environment.GetEnvironmentVariable("SJZ_DATA_DIR");
            if (!string.IsNullOrEmpty(dataEnv)) DataDir = dataEnv;
            try { Directory.CreateDirectory(DataDir); } catch (Exception) { }
            LogFile = Path.Combine(DataDir, "launcher.log");
            Pid = System.Diagnostics.Process.GetCurrentProcess().Id;
            ConfigPath = Path.Combine(ExeDir, "launcher.config.json");
            if (!File.Exists(ConfigPath))
            {
                string alt = Path.Combine(Root, "launcher.config.json");
                if (File.Exists(alt)) ConfigPath = alt;
            }
        }
    }

    internal static class Log
    {
        private static readonly object Lock = new object();
        private static Action<string> _sink;

        public static void SetSink(Action<string> sink) { _sink = sink; }

        public static void Write(string msg)
        {
            string line = DateTime.Now.ToString("HH:mm:ss") + "  " + msg;
            lock (Lock)
            {
                try
                {
                    if (File.Exists(AppPaths.LogFile)
                        && new FileInfo(AppPaths.LogFile).Length > 2 * 1024 * 1024)
                    {
                        File.Delete(AppPaths.LogFile + ".1");
                        File.Move(AppPaths.LogFile, AppPaths.LogFile + ".1");
                    }
                    File.AppendAllText(AppPaths.LogFile, line + Environment.NewLine, Encoding.UTF8);
                }
                catch (Exception) { }
            }
            Action<string> sink = _sink;
            if (sink != null) sink(line);
        }
    }

    internal sealed class LauncherConfig
    {
        public string UpdateUrl = "";
        public string Python = @"runtime\pythonw.exe";
        public string Script = "desktop.py";
        public string DriverDll = @"core\assets\interception.dll";
        public string GameWindowClass = "UnrealWindow";
        public string[] GameProcesses = new string[0];
        public int MaxRestarts = 3;
        public int CtrlPort = 8777;
        // version.json 是否强制要求签名。false 时也接受明文
        // {version,url,sha256,notes}（内测/自建下载站用，仍然校验 sha256）。
        public bool UpdateRequireSig = true;
        public string[] ProtectedPaths = { "launcher", "config.json", "data",
                                          "templates", ".venv", ".venv-vl",
                                          "launcher.config.json" };

        public string DriverDllPath()
        {
            string p = Path.IsPathRooted(DriverDll) ? DriverDll : Path.Combine(AppPaths.Root, DriverDll);
            if (File.Exists(p)) return p;
            // frozen onedir 里随包资源在 _internal\ 下
            string frozen = Path.Combine(AppPaths.Root, "_internal", DriverDll);
            return File.Exists(frozen) ? frozen : p;
        }

        public static LauncherConfig Load()
        {
            var cfg = new LauncherConfig();
            try
            {
                if (!File.Exists(AppPaths.ConfigPath)) return cfg;
                JsonNode root = Json.Parse(File.ReadAllText(AppPaths.ConfigPath, Encoding.UTF8));
                if (root.Kind != JsonKind.Object) return cfg;

                cfg.UpdateUrl = root.GetString("update_url") ?? cfg.UpdateUrl;
                JsonNode urs = root.Get("update_require_sig");
                if (urs != null && urs.Kind == JsonKind.Bool) cfg.UpdateRequireSig = urs.BoolValue;
                cfg.Python = root.GetString("python") ?? cfg.Python;
                cfg.Script = root.GetString("script") ?? cfg.Script;
                cfg.DriverDll = root.GetString("driver_dll") ?? cfg.DriverDll;
                cfg.GameWindowClass = root.GetString("game_window_class") ?? cfg.GameWindowClass;
                long n;
                if (root.TryLong("max_restarts", out n)) cfg.MaxRestarts = (int)Math.Max(0, Math.Min(10, n));
                if (root.TryLong("ctrl_port", out n)) cfg.CtrlPort = (int)Math.Max(1024, Math.Min(65535, n));

                JsonNode gp = root.Get("game_processes");
                if (gp != null && gp.Kind == JsonKind.Array)
                {
                    var list = new List<string>();
                    foreach (JsonNode x in gp.Items)
                        if (x.Kind == JsonKind.String && x.Text.Length > 0) list.Add(x.Text);
                    cfg.GameProcesses = list.ToArray();
                }
                JsonNode pp = root.Get("protected_paths");
                if (pp != null && pp.Kind == JsonKind.Array)
                {
                    var list = new List<string>();
                    foreach (JsonNode x in pp.Items)
                        if (x.Kind == JsonKind.String) list.Add(x.Text);
                    if (list.Count > 0) cfg.ProtectedPaths = list.ToArray();
                }
            }
            catch (Exception e)
            {
                Log.Write("!! launcher.config.json 读取失败，用默认值：" + e.Message);
            }
            return cfg;
        }
    }

    internal sealed class Cli
    {
        public string Mode = "gui";
        public string FilePath;
        public string SpawnExe;
        public string[] SpawnArgs = new string[0];
        public int Seconds = 0;
        public int RestartMax = 0;
        public int BackoffMs = 300;
        public string OutPath;
        public bool KeepChildren;
        public int Port = 0;
        public string Token = "";
        public bool IgnoreFatalCheck;
        public int KillPid = 0;

        public static Cli Parse(string[] args)
        {
            var cli = new Cli();
            var rest = new List<string>();
            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i];
                switch (a)
                {
                    case "--fp": cli.Mode = "fp"; break;
                    case "--preflight": cli.Mode = "preflight"; break;
                    case "--selftest": cli.Mode = "selftest"; break;
                    case "--headless": cli.Mode = "headless"; break;
                    case "--perf": cli.Mode = "perf"; break;
                    case "--update-only": cli.Mode = "update"; break;
                    case "--rollback": cli.Mode = "rollback"; break;
                    case "--verify":
                        cli.Mode = "verify";
                        if (i + 1 < args.Length && !args[i + 1].StartsWith("--"))
                            cli.FilePath = args[++i];
                        break;
                    case "--spawn": cli.Mode = "spawn"; break;
                    case "--mutex-probe": cli.Mode = "mutex"; break;
                    case "--ctrl": cli.Mode = "ctrl"; break;
                    case "--ctrl-stop": cli.Mode = "ctrl-stop"; break;
                    case "--stop": cli.Mode = "stop"; break;
                    case "--kill-pid":
                        cli.Mode = "kill";
                        if (i + 1 < args.Length) cli.KillPid = ToInt(args[++i]);
                        break;
                    case "--port":
                        if (i + 1 < args.Length) cli.Port = ToInt(args[++i]);
                        break;
                    case "--token":
                        if (i + 1 < args.Length) cli.Token = args[++i];
                        break;
                    case "--version": cli.Mode = "version"; break;
                    case "--keep-children": cli.KeepChildren = true; break;
                    case "--ignore-preflight-fail": cli.IgnoreFatalCheck = true; break;
                    case "--license":
                        if (i + 1 < args.Length) cli.FilePath = args[++i];
                        break;
                    case "--out":
                        if (i + 1 < args.Length) cli.OutPath = args[++i];
                        break;
                    case "--seconds":
                        if (i + 1 < args.Length) cli.Seconds = ToInt(args[++i]);
                        break;
                    case "--restart-max":
                        if (i + 1 < args.Length) cli.RestartMax = ToInt(args[++i]);
                        break;
                    case "--restart-backoff":
                        if (i + 1 < args.Length) cli.BackoffMs = ToInt(args[++i]);
                        break;
                    case "--spawn-exe":
                        if (i + 1 < args.Length) cli.SpawnExe = args[++i];
                        break;
                    case "--":
                        // 之后的都当子进程参数
                        for (int k = i + 1; k < args.Length; k++) rest.Add(args[k]);
                        i = args.Length;
                        break;
                    default:
                        rest.Add(a);
                        break;
                }
            }
            cli.SpawnArgs = rest.ToArray();
            if (cli.Mode == "verify" && cli.FilePath == null)
                cli.FilePath = LicenseCheck.DefaultPath();
            return cli;
        }

        private static int ToInt(string s)
        {
            int v;
            return int.TryParse(s, out v) ? v : 0;
        }
    }

    internal static class Program
    {
        [DllImport("kernel32.dll")]
        private static extern bool AttachConsole(int pid);

        private const int AttachParentProcess = -1;

        [STAThread]
        public static int Main(string[] args)
        {
            AppPaths.Init();
            Cli cli = Cli.Parse(args);

            if (cli.Mode != "gui") AttachConsole(AttachParentProcess);

            // 先把自己放进 Job，之后拉起的子进程才会继承 Job：
            // 启动器被强杀时 KILL_ON_JOB_CLOSE 会把整棵树带走。
            bool inJob = Native.AdoptSelfIntoJob();
            if (cli.Mode != "gui" && cli.Mode != "fp" && cli.Mode != "verify")
                Log.Write(inJob ? "job: ok" : "job: 绑定失败（孤儿兜底降级）");

            switch (cli.Mode)
            {
                case "version":
                    Emit("{\"version\":\"" + AppVersion.Current + "\"}", cli);
                    return 0;
                case "fp":
                    return CmdFingerprint(cli);
                case "verify":
                    return CmdVerify(cli);
                case "preflight":
                    return CmdPreflight(cli);
                case "selftest":
                    return CmdSelfTest(cli);
                case "update":
                    return CmdUpdate(cli);
                case "rollback":
                    return CmdRollback(cli);
                case "spawn":
                    return SpawnTest.Run(cli.SpawnExe, cli.SpawnArgs, cli.Seconds,
                                         cli.RestartMax, cli.BackoffMs, cli.KeepChildren);
                case "headless":
                    return CmdHeadless(cli);
                case "perf":
                    return CmdPerf(cli);
                case "mutex":
                    return CmdMutex(cli);
                case "ctrl":
                    return CmdCtrl(cli, false);
                case "ctrl-stop":
                    return CmdCtrl(cli, true);
                case "stop":
                    return CmdStop(cli);
                case "kill":
                    return CmdKillPid(cli);
            }

#if SJZ_TEST_BUILD
            Console.Error.WriteLine("SjzLauncherTest.exe 只跑命令行模式，GUI 请用 SjzLauncher.exe");
            return 2;
#else
            // 全局互斥体：第二次启动把已存在的窗口拉前台，然后自己退出。
            if (!Native.AcquireSingleInstance(MutexName))
            {
                Native.ActivateExisting(LauncherForm.WindowTitle, 12);
                return 0;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            try
            {
                Application.Run(new LauncherForm());
            }
            finally
            {
                Native.ReleaseSingleInstance();
            }
            return 0;
#endif
        }

        public const string MutexName = @"Global\SjzLauncher.Console.v1";

        private static void Emit(string json, Cli cli)
        {
            Console.WriteLine(json);
            Console.Out.Flush();
            if (!string.IsNullOrEmpty(cli.OutPath))
            {
                try { File.WriteAllText(cli.OutPath, json, new UTF8Encoding(false)); }
                catch (Exception) { }
            }
        }

        private static int CmdFingerprint(Cli cli)
        {
            LauncherConfig cfg = LauncherConfig.Load();
            FingerprintResult fp = Fingerprint.Collect();
            Emit(fp.ToJson(), cli);
            return 0;
        }

        private static int CmdVerify(Cli cli)
        {
            FingerprintResult fp = Fingerprint.Collect();
            LicenseResult res = LicenseCheck.Load(cli.FilePath, fp);
            Emit(res.ToJson(), cli);
            return res.Allowed ? 0 : 1;
        }

        private static int CmdPreflight(Cli cli)
        {
            LauncherConfig cfg = LauncherConfig.Load();
            List<CheckItem> items = Preflight.Run(cfg);
            var sb = new StringBuilder("{\"items\":[");
            for (int i = 0; i < items.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append(items[i].ToJson());
            }
            sb.Append("],\"fatal\":").Append(Preflight.AnyFatal(items) ? "true" : "false").Append('}');
            Emit(sb.ToString(), cli);
            return Preflight.AnyFatal(items) ? 1 : 0;
        }

        private static int CmdSelfTest(Cli cli)
        {
            // JSON 规范化 KAT：和 Python 的 canonical_bytes 输出逐字节比对
            JsonNode n = Json.Parse("{\"b\":2,\"a\":\"中文 \\u0001\",\"c\":[1,true,null]}");
            string canon = Json.Canonical(n);
            bool keyOrder = canon.StartsWith("{\"a\":");
            bool pubOk;
            try { pubOk = Crypto.EmbeddedPublicKey().Length == 64; }
            catch (Exception) { pubOk = false; }
            FingerprintResult fp = Fingerprint.Collect();
            string json = "{\"ok\":" + (keyOrder && pubOk ? "true" : "false")
                        + ",\"canonical\":\"" + canon.Replace("\\", "\\\\").Replace("\"", "\\\"")
                        + "\",\"pub_ok\":" + (pubOk ? "true" : "false")
                        + ",\"fp_hash\":\"" + fp.Hash + "\"}";
            Emit(json, cli);
            return keyOrder && pubOk ? 0 : 1;
        }

        private static int CmdUpdate(Cli cli)
        {
            LauncherConfig cfg = LauncherConfig.Load();
            UpdateResult r = Updater.CheckAndApply(cfg, true, s => Log.Write(s));
            Emit(r.ToJson(), cli);
            return r.Ok || r.Status == "disabled" ? 0 : 1;
        }

        /// <summary>回滚到上一版（update_backup 目录仍在时）。</summary>
        private static int CmdRollback(Cli cli)
        {
            LauncherConfig cfg = LauncherConfig.Load();
            UpdateResult r = Updater.Rollback(cfg, s => Log.Write(s));
            Emit(r.ToJson(), cli);
            return r.Ok ? 0 : 1;
        }

        /// <summary>单实例闸探针：能不能拿到 Global 互斥体（pytest 用）。</summary>
        private static int CmdMutex(Cli cli)
        {
            bool acquired = Native.AcquireSingleInstance(MutexName);
            Emit("{\"acquired\":" + (acquired ? "true" : "false") + "}", cli);
            Native.ReleaseSingleInstance();
            return acquired ? 0 : 1;
        }

        /// <summary>控制面客户端探针：--ctrl（查状态）/ --ctrl-stop（要求停止）。</summary>
        private static int CmdCtrl(Cli cli, bool stop)
        {
            if (cli.Port <= 0)
            {
                Emit("{\"ok\":false,\"reason\":\"需要 --port\"}", cli);
                return 2;
            }
            if (stop)
            {
                bool sent = ControlPlane.PostStop(cli.Port, cli.Token, 2000);
                Emit("{\"ok\":" + (sent ? "true" : "false") + "}", cli);
                return sent ? 0 : 1;
            }
            JsonNode st = ControlPlane.GetStatus(cli.Port, cli.Token, 2000);
            if (st == null)
            {
                Emit("{\"ok\":false,\"reason\":\"无应答或被拒\"}", cli);
                return 1;
            }
            long pid;
            bool hasPid = st.TryLong("pid", out pid);
            Emit("{\"ok\":true,\"has_pid\":" + (hasPid ? "true" : "false")
                 + ",\"pid\":" + pid + ",\"status\":" + Json.Canonical(st) + "}", cli);
            return 0;
        }

        /// <summary>
        /// --stop：同权限收尾。结束控制台实例、清 config.json.lock、清实例登记。
        /// 提权实例杀不掉（同权限原则），但日常实例（清单已改 asInvoker）能收掉。
        /// </summary>
        private static int CmdStop(Cli cli)
        {
            int pid = 0;
            string owner = Path.Combine(AppPaths.DataDir, "instance.json");
            try
            {
                if (File.Exists(owner))
                {
                    JsonNode root = Json.Parse(File.ReadAllText(owner, Encoding.UTF8));
                    long n;
                    if (root != null && root.TryLong("pid", out n)) pid = (int)n;
                }
            }
            catch (Exception) { }

            bool stopped = false;
            if (pid > 0)
            {
                try
                {
                    Process p = Process.GetProcessById(pid);
                    p.Kill();
                    stopped = p.WaitForExit(3000);
                    Log.Write("--stop：结束控制台 pid=" + pid + " ok=" + stopped);
                }
                catch (Exception e)
                {
                    Log.Write("--stop：pid=" + pid + " 结束失败：" + e.Message);
                }
            }
            else
            {
                // 没有登记（旧版本留下的进程）：按进程名 + 安装根目录筛一遍
                try
                {
                    string root = Path.GetFullPath(AppPaths.Root);
                    foreach (Process p in Process.GetProcessesByName("SJZ-Control"))
                    {
                        try
                        {
                            string exe = Path.GetFullPath(p.MainModule.FileName);
                            if (exe.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                            {
                                p.Kill();
                                stopped = true;
                                Log.Write("--stop：按路径命中 pid=" + p.Id + " " + exe);
                            }
                        }
                        catch (Exception) { }
                    }
                }
                catch (Exception) { }
            }

            foreach (string rel in new[] { "config.json.lock",
                                           Path.Combine("data", "config.json.lock") })
            {
                try
                {
                    string p = Path.Combine(AppPaths.Root, rel);
                    if (File.Exists(p))
                    {
                        File.Delete(p);
                        Log.Write("--stop：已清 " + rel);
                    }
                }
                catch (Exception) { }
            }
            try { File.Delete(owner); } catch (Exception) { }

            Emit("{\"ok\":" + (stopped ? "true" : "false")
                 + ",\"pid\":" + pid + "}", cli);
            return (stopped || pid <= 0) ? 0 : 1;
        }

        /// <summary>
        /// --kill-pid：极小的「提权 killer」。只做一件事——把指定 pid 及其子树收掉。
        /// 界面在权限不足（错误码 5/6）时会用 runas 拉起这个模式，启动器本身不提权。
        /// 同样遵循「窗口 → 进程」两级：先 WM_CLOSE 等 2s，退不掉再 TerminateTree。
        /// </summary>
        private static int CmdKillPid(Cli cli)
        {
            int pid = cli.KillPid;
            var sw = Stopwatch.StartNew();
            if (pid <= 0)
            {
                Emit("{\"ok\":false,\"reason\":\"需要 --kill-pid N\"}", cli);
                return 2;
            }
            if (!Native.ProcessAlive(pid))
            {
                Emit("{\"ok\":true,\"pid\":" + pid + ",\"reason\":\"进程已不存在\",\"elapsed_ms\":"
                     + sw.ElapsedMilliseconds + "}", cli);
                return 0;
            }

            int windows = Native.PostCloseToPid(pid);
            if (windows > 0 && Native.WaitExit(pid, 2000))
            {
                ClearInstanceIfMatches(pid);
                Emit("{\"ok\":true,\"pid\":" + pid + ",\"reason\":\"关闭窗口后已退出\",\"elapsed_ms\":"
                     + sw.ElapsedMilliseconds + "}", cli);
                return 0;
            }

            int killed;
            int err = Native.TerminateTree(pid, out killed);
            Native.WaitExit(pid, 800);
            bool alive = Native.ProcessAlive(pid);
            if (!alive) ClearInstanceIfMatches(pid);
            Emit("{\"ok\":" + (alive ? "false" : "true")
                 + ",\"pid\":" + pid
                 + ",\"killed\":" + killed
                 + ",\"error\":" + err
                 + ",\"reason\":\"" + (alive ? "终止失败" : "已强制结束") + "\""
                 + ",\"elapsed_ms\":" + sw.ElapsedMilliseconds + "}", cli);
            return alive ? 1 : 0;
        }

        private static void ClearInstanceIfMatches(int pid)
        {
            try
            {
                string owner = Path.Combine(AppPaths.DataDir, "instance.json");
                if (!File.Exists(owner)) return;
                JsonNode root = Json.Parse(File.ReadAllText(owner, Encoding.UTF8));
                long n;
                if (root != null && root.TryLong("pid", out n) && (int)n == pid)
                    File.Delete(owner);
            }
            catch (Exception) { }
        }

        /// <summary>无窗口跑完整流程（自动化测试与排障用）。</summary>
        private static int CmdHeadless(Cli cli)
        {
            LauncherConfig cfg = LauncherConfig.Load();
            string token = Crypto.NewToken();
            var rt = new LauncherRuntime(cfg, token);
            int code = rt.RunHeadless(cli.Seconds, cli.IgnoreFatalCheck, cli.FilePath);
            return code;
        }

        /// <summary>
        /// 性能探针：把 GUI 按钮里同步执行的那几步单独计时，量出「按一下卡多久」。
        /// 输出 JSON：start_block_ms / tick_sum_ms / tick_worst_ms / ready_ms /
        ///           stop_block_ms / exit_ms / still_alive
        /// </summary>
        private static int CmdPerf(Cli cli)
        {
            LauncherConfig cfg = LauncherConfig.Load();
            string token = Crypto.NewToken();
            var rt = new LauncherRuntime(cfg, token);
            var sb = new StringBuilder("{\"perf\":true");

            Stopwatch sw = new Stopwatch();
            sw.Start();
            bool prep = rt.Prepare(false, s => Log.Write(s));
            sw.Stop();
            sb.Append(",\"prepare_ms\":").Append(sw.ElapsedMilliseconds);
            if (!prep)
            {
                sb.Append(",\"ok\":false,\"stage\":\"prepare\"}");
                Emit(sb.ToString(), cli);
                return 3;
            }

            string err;
            sw.Reset(); sw.Start();
            bool started = rt.StartConsole(out err);
            sw.Stop();
            sb.Append(",\"start_block_ms\":").Append(sw.ElapsedMilliseconds)
              .Append(",\"start_ok\":").Append(started ? "true" : "false");
            if (!started)
            {
                string eq = (err ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"");
                sb.Append(",\"error\":\"").Append(eq).Append("\",\"ok\":false}");
                Emit(sb.ToString(), cli);
                return 5;
            }

            // 模拟 GUI 的 1Hz 定时器：改前这一步是跑在 UI 线程上的，卡多久就是用户感觉到的卡多久
            int ticks = cli.Seconds > 0 ? cli.Seconds : 12;
            long tickSum = 0, tickWorst = 0, readyAt = -1;
            var arr = new StringBuilder("[");
            int baseTick = Environment.TickCount;
            for (int i = 0; i < ticks; i++)
            {
                sw.Reset(); sw.Start(); rt.RefreshStatus(); sw.Stop();
                long ms = sw.ElapsedMilliseconds;
                tickSum += ms;
                if (ms > tickWorst) tickWorst = ms;
                if (readyAt < 0 && rt.CtrlPort > 0) readyAt = Environment.TickCount - baseTick;
                if (i > 0) arr.Append(',');
                arr.Append("{\"i\":").Append(i).Append(",\"ms\":").Append(ms)
                   .Append(",\"port\":").Append(rt.CtrlPort).Append('}');
                int spent = Environment.TickCount - baseTick - i * 1000;
                if (1000 - spent > 0) System.Threading.Thread.Sleep(1000 - spent);
            }
            arr.Append(']');
            sb.Append(",\"ticks\":").Append(arr)
              .Append(",\"tick_sum_ms\":").Append(tickSum)
              .Append(",\"tick_worst_ms\":").Append(tickWorst)
              .Append(",\"ready_ms\":").Append(readyAt);

            sw.Reset(); sw.Start();
            rt.Shutdown(8000);
            sw.Stop();
            sb.Append(",\"stop_block_ms\":").Append(sw.ElapsedMilliseconds);

            int pid = rt.Host.Pid;
            long tStop = Environment.TickCount;
            while (pid > 0 && Native.ProcessAlive(pid) && Environment.TickCount - tStop < 12000)
                System.Threading.Thread.Sleep(50);
            bool alive = pid > 0 && Native.ProcessAlive(pid);
            sb.Append(",\"exit_ms\":").Append(Environment.TickCount - tStop)
              .Append(",\"still_alive\":").Append(alive ? "true" : "false")
              .Append(",\"pid\":").Append(pid)
              .Append(",\"ok\":true}");
            Emit(sb.ToString(), cli);
            return alive ? 6 : 0;
        }
    }
}
