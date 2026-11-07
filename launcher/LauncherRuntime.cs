// 启动流程编排：许可 → 自检 → 更新 → 拉起控制台 → 守护。
// GUI 和 --headless 共用这一套，保证「自动化测的就是用户跑的」。
namespace SjzLauncher
{
    using System;
    using System.Collections.Generic;
    using System.IO;
    using System.Text;
    using System.Threading;

    internal sealed class LauncherRuntime
    {
        public readonly LauncherConfig Config;
        public readonly string Token;
        public readonly PythonHost Host;

        public LicenseResult License;
        public List<CheckItem> Checks = new List<CheckItem>();
        public UpdateResult Update;

        public int CtrlPort;
        public JsonNode LastStatus;

        public LauncherRuntime(LauncherConfig cfg, string token)
        {
            Config = cfg;
            Token = token;
            Host = new PythonHost(cfg, token);
            // 更新替换文件前先停控制台（frozen 下主 exe 与 _internal 都被占着）
            Updater.StopConsole = delegate
            {
                if (Host.Running) Host.StopAndWait(8000);
            };
        }

        /// <summary>许可 + 自检 + （可选）更新。返回 false = 不该继续起控制台。</summary>
        public bool Prepare(bool doUpdate, Action<string> log, bool ignoreFatalChecks = false,
                            string licensePath = null)
        {
            FingerprintResult fp = Fingerprint.Collect();
            License = LicenseCheck.Load(licensePath ?? LicenseCheck.DefaultPath(), fp);
            log(string.Format("卡密：{0} — {1}", License.StatusName(), License.Reason));
            log("机器码：" + License.MachineCode);

            Checks = Preflight.Run(Config);
            foreach (CheckItem c in Checks)
                log(string.Format("自检 {0}：[{1}] {2}", c.Title, c.LevelName(), c.Detail));

            if (!License.Allowed) return false;
            if (Preflight.AnyFatal(Checks) && !ignoreFatalChecks)
            {
                log("!! 自检有致命项，停止启动");
                return false;
            }

            if (doUpdate && !string.IsNullOrEmpty(Config.UpdateUrl))
            {
                Update = Updater.CheckAndApply(Config, License.Allowed, log);
                log(string.Format("更新：{0} — {1}", Update.Status, Update.Reason));
                if (Update.Notes.Length > 0) log("更新说明：" + Update.Notes);
            }
            else
            {
                Update = new UpdateResult { Status = "disabled", Reason = "跳过更新检查" };
            }
            return true;
        }

        public bool StartConsole(out string error)
        {
            bool ok = Host.Start(out error);
            if (ok) CtrlPort = 0;
            return ok;
        }

        /// <summary>轮询控制面直到拿到 pid 匹配的 status（最多 waitMs）。</summary>
        public bool WaitForControlPlane(int waitMs)
        {
            DateTime end = DateTime.Now.AddMilliseconds(waitMs);
            while (DateTime.Now < end)
            {
                int pid = Host.Pid;
                if (pid > 0)
                {
                    JsonNode st;
                    int port = ControlPlane.Discover(pid, Token, Config.CtrlPort, 20, out st);
                    if (port > 0)
                    {
                        CtrlPort = port;
                        LastStatus = st;
                        return true;
                    }
                }
                Thread.Sleep(400);
            }
            return false;
        }

        /// <summary>
        /// 新版本首次启动的确认/回退（硬要求：更新失败不能把现有安装弄坏）。
        /// 控制面在 waitMs 内没应答 → 视为新版本起不来 → 自动回滚并重启一次。
        /// </summary>
        public bool VerifyNewVersion(int waitMs, Action<string> log)
        {
            bool pending = Updater.HasUnconfirmedUpdate();
            bool ready = WaitForControlPlane(waitMs);
            if (!pending) return ready;
            if (ready)
            {
                Updater.ConfirmRunning();
                log("新版本已确认可运行");
                return true;
            }

            log("新版本启动后控制面无应答，开始回滚上一版");
            UpdateResult rb = Updater.Rollback(Config, log);
            if (!rb.Ok) return false;

            Update = rb;
            Host.StopAndWait(3000);
            string err;
            if (Host.Start(out err) && WaitForControlPlane(waitMs))
            {
                log("回滚后启动成功：" + rb.Version);
                return true;
            }
            log("回滚后仍起不来：" + err);
            return false;
        }

        public void RefreshStatus()
        {
            Host.RefreshStatus(out LastStatus);
            CtrlPort = Host.CtrlPort;
        }

        public bool RequestStop()
        {
            if (CtrlPort <= 0 || Token == null) return false;
            return ControlPlane.PostStop(CtrlPort, Token, 2000);
        }

        public void Shutdown(int graceMs)
        {
            Host.StopAndWait(graceMs);
        }

        /// <summary>
        /// 停止并带回真实结果：成功 / 强制 / 权限不足（需要管理员）。
        /// 界面照着它回写，不允许再「点了没反应、状态照旧运行中」。
        /// </summary>
        public PythonHost.StopOutcome ShutdownEx(int graceMs)
        {
            if (LegacyPid > 0)
            {
                PythonHost.StopOutcome fr = PythonHost.StopForeignPid(LegacyPid);
                if (fr.Ok) LegacyPid = 0;
                return fr;
            }
            return Host.StopEx(graceMs);
        }

        /// <summary>
        /// 上一次启动器退出时登记过的「遗留控制台」pid（instance.json）。
        /// 存活才可信；已死就清掉登记，别让界面继续显示「运行中」。
        /// </summary>
        public int LegacyPid { get; private set; }

        /// <summary>读实例登记：活着就接管显示，死了就清掉假状态。</summary>
        public void ClearLegacy()
        {
            LegacyPid = 0;
        }

        public void ProbeLegacyInstance()
        {
            LegacyPid = 0;
            string path = System.IO.Path.Combine(AppPaths.DataDir, "instance.json");
            int pid = 0;
            try
            {
                if (!System.IO.File.Exists(path)) return;
                JsonNode root = Json.Parse(System.IO.File.ReadAllText(path,
                    System.Text.Encoding.UTF8));
                long n;
                if (root != null && root.TryLong("pid", out n)) pid = (int)n;
            }
            catch (Exception) { }
            if (pid <= 0) return;
            if (!Native.ProcessAlive(pid))
            {
                try { System.IO.File.Delete(path); } catch (Exception) { }
                Log.Write("已清理遗留的「运行中」登记（pid=" + pid + " 早已不存在）");
                return;
            }
            LegacyPid = pid;
            Log.Write("检测到遗留控制台 pid=" + pid + "（上一次启动器退出时留下的），可直接停止");
        }

        public string StatusJson()
        {
            JsonNode st = LastStatus;
            string pid = Host.Running ? Host.Pid.ToString() : "0";
            string inner = st == null ? "null" : Json.Canonical(st);
            return "{\"launcher_pid\":" + System.Diagnostics.Process.GetCurrentProcess().Id
                 + ",\"python_pid\":" + pid
                 + ",\"ctrl_port\":" + (CtrlPort > 0 ? CtrlPort.ToString() : "0")
                 + ",\"status\":" + inner + "}";
        }

        /// <summary>无窗口全流程；返回进程退出码语义：0 正常，3 许可，4 自检，5 拉起失败。</summary>
        public int RunHeadless(int seconds, bool ignoreFatalChecks, string licensePath = null)
        {
            // 无窗口模式就是自动化入口：让被拉起的控制台继承这个约定，
            // 别在 CI/沙箱里建 WebView 窗口（建窗会占住主线程，HTTP 不应答）。
            Environment.SetEnvironmentVariable("SJZ_NO_WINDOW", "1");
            Action<string> log = s => Dispatch("log", "{\"msg\":" + Q(s) + "}");
            if (!Prepare(true, log, ignoreFatalChecks, licensePath))
            {
                Dispatch("license", License.ToJson());
                Dispatch("done", "{\"ok\":false,\"stage\":\"license\"}");
                return License.Allowed ? 4 : 3;
            }
            Dispatch("license", License.ToJson());

            var sb = new StringBuilder("{\"items\":[");
            for (int i = 0; i < Checks.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append(Checks[i].ToJson());
            }
            sb.Append("]}");
            Dispatch("preflight", sb.ToString());
            Dispatch("update", Update.ToJson());

            string err;
            if (!StartConsole(out err))
            {
                Dispatch("error", "{\"msg\":" + Q(err) + "}");
                Dispatch("done", "{\"ok\":false,\"stage\":\"spawn\"}");
                return 5;
            }
            Dispatch("spawn", "{\"pid\":" + Host.Pid + "}");

            bool ready = VerifyNewVersion(15000, log);
            Dispatch("ready", "{\"ok\":" + (ready ? "true" : "false")
                               + ",\"port\":" + CtrlPort + ",\"status\":" + StatusJson() + "}");
            if (!ready) Dispatch("warn", "{\"msg\":\"控制面没应答（可能没传 token 或被拒）\"}");

            int code;
            if (seconds > 0)
            {
                DateTime end = DateTime.Now.AddSeconds(seconds);
                while (DateTime.Now < end && Host.Running) Thread.Sleep(500);
            }
            else
            {
                while (Host.Running) Thread.Sleep(500);
            }

            bool killed = Host.StopAndWait(8000);
            Dispatch("stopped", "{\"forced\":" + (killed ? "true" : "false") + "}");
            Dispatch("done", "{\"ok\":true,\"python_pid\":" + (Host.Pid > 0 ? Host.Pid.ToString() : "0") + "}");
            code = 0;
            return code;
        }

        private static void Dispatch(string evt, string body)
        {
            if (body.Length > 1 && body[0] == '{')
                Console.WriteLine("{\"event\":\"" + evt + "\"," + body.Substring(1));
            else
                Console.WriteLine("{\"event\":\"" + evt + "\"," + body + "}");
            Console.Out.Flush();
        }

        private static string Q(string s)
        {
            return "\"" + (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"")
                .Replace("\r", "\\r").Replace("\n", "\\n") + "\"";
        }
    }
}
