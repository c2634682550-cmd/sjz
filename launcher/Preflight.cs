// 环境自检：.NET 版本 / VC++ 运行库 / 驱动与签名 / 游戏是否在跑。
// 只读，不改任何系统状态。
namespace SjzLauncher
{
    using System;
    using System.Collections.Generic;
    using System.Diagnostics;
    using System.Globalization;
    using System.IO;
    using Microsoft.Win32;

    internal enum CheckLevel { Ok, Warn, Fail }

    internal sealed class CheckItem
    {
        public string Key;
        public string Title;
        public CheckLevel Level = CheckLevel.Ok;
        public string Detail = "";

        public string LevelName()
        {
            return Level == CheckLevel.Fail ? "fail" : Level == CheckLevel.Warn ? "warn" : "ok";
        }

        public string ToJson()
        {
            var n = JsonNode.NewObject();
            AddString(n, "key", Key);
            AddString(n, "title", Title);
            AddString(n, "level", LevelName());
            AddString(n, "detail", Detail);
            return Json.Canonical(n);
        }

        private static void AddString(JsonNode n, string k, string v)
        {
            var s = new JsonNode();
            s.Kind = JsonKind.String;
            s.Text = v ?? "";
            n.Members[k] = s;
        }
    }

    internal static class Preflight
    {
        public static List<CheckItem> Run(LauncherConfig cfg)
        {
            var items = new List<CheckItem>();
            items.Add(DotNet());
            items.Add(VcRuntime());
            items.Add(Driver(cfg));
            items.Add(Game(cfg));
            return items;
        }

        public static bool AnyFatal(List<CheckItem> items)
        {
            foreach (CheckItem i in items) if (i.Level == CheckLevel.Fail) return true;
            return false;
        }

        // ---------------- .NET ----------------

        private static CheckItem DotNet()
        {
            var item = new CheckItem { Key = "dotnet", Title = ".NET Framework" };
            long release = 0;
            try
            {
                using (RegistryKey k = Registry.LocalMachine.OpenSubKey(
                           @"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full", false))
                {
                    if (k != null)
                    {
                        object v = k.GetValue("Release", null);
                        if (v != null) release = Convert.ToInt64(v, CultureInfo.InvariantCulture);
                    }
                }
            }
            catch (Exception) { }

            string ver = release >= 533320 ? "4.8.1+"
                       : release >= 528040 ? "4.8"
                       : release >= 461808 ? "4.7.2"
                       : release >= 393295 ? "4.6+"
                       : release > 0 ? "4.x(Release=" + release + ")"
                       : "未知";
            item.Detail = string.Format("Release={0}（{1}），CLR {2}",
                                        release, ver, Environment.Version);
            if (release > 0 && release < 528040)
            {
                item.Level = CheckLevel.Fail;
                item.Detail += "；需要 4.8，请先安装 .NET Framework 4.8";
            }
            else if (release == 0)
            {
                item.Level = CheckLevel.Warn;
                item.Detail += "；注册表里没读到，按实际运行版本继续";
            }
            return item;
        }

        // ---------------- VC++ ----------------

        private static CheckItem VcRuntime()
        {
            var item = new CheckItem { Key = "vcrt", Title = "VC++ 运行库" };
            var names = new[] { "vcruntime140.dll", "msvcp140.dll", "ucrtbase.dll" };
            var found = new List<string>();
            var missing = new List<string>();
            foreach (string n in names)
            {
                string p = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.System), n);
                if (File.Exists(p)) found.Add(n);
                else missing.Add(n);
            }
            // python 运行时自带一份，系统没有也不算致命；frozen 形态在 _internal\ 里
            string pyDir = Path.Combine(AppPaths.Root, @"runtime");
            bool pyLocal = File.Exists(Path.Combine(pyDir, "vcruntime140.dll"))
                        || File.Exists(Path.Combine(AppPaths.Root, @"_internal\vcruntime140.dll"));
            if (missing.Count == 0)
            {
                item.Detail = "系统目录齐全：" + string.Join(", ", found.ToArray());
            }
            else if (pyLocal)
            {
                item.Level = CheckLevel.Warn;
                item.Detail = "系统缺少 " + string.Join(", ", missing.ToArray())
                            + "，但包内自带，可继续";
            }
            else
            {
                item.Level = CheckLevel.Warn;
                item.Detail = "系统缺少 " + string.Join(", ", missing.ToArray())
                            + "（可能需要安装 VC++ 2015-2022 x64 运行库）";
            }
            return item;
        }

        // ---------------- 驱动 ----------------

        private static CheckItem Driver(LauncherConfig cfg)
        {
            var item = new CheckItem { Key = "driver", Title = "输入驱动" };
            string sysPath = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.System),
                @"drivers\interception.sys");
            string userDll = cfg.DriverDllPath();
            var parts = new List<string>();

            if (File.Exists(sysPath))
            {
                int hr = Native.VerifyTrustStatus(sysPath);
                string sig = hr == 0 ? "签名有效" : "未签名/签名无效(0x" + ((uint)hr).ToString("x8") + ")";
                parts.Add("内核 interception.sys 存在，" + sig);
                if (hr != 0) item.Level = CheckLevel.Warn;
            }
            else
            {
                parts.Add("内核 interception.sys 不存在（驱动没装）");
                item.Level = CheckLevel.Warn;
            }

            if (File.Exists(userDll))
            {
                int hr = Native.VerifyTrustStatus(userDll);
                parts.Add("interception.dll " + (hr == 0 ? "签名有效" : "未签名"));
                if (hr != 0 && item.Level == CheckLevel.Ok) item.Level = CheckLevel.Warn;
            }
            else
            {
                parts.Add("缺少 " + userDll);
                item.Level = CheckLevel.Fail;        // 没有它注入直接不可用
            }
            item.Detail = string.Join("；", parts.ToArray());
            return item;
        }

        // ---------------- 游戏 ----------------

        private static CheckItem Game(LauncherConfig cfg)
        {
            var item = new CheckItem { Key = "game", Title = "游戏进程" };
            var pids = new List<int>();
            bool byWindow = false;
            try { byWindow = Native.HasWindowOfClass(cfg.GameWindowClass, out pids); }
            catch (Exception) { }

            var names = new List<string>();
            foreach (string exe in cfg.GameProcesses)
            {
                try
                {
                    foreach (Process p in Process.GetProcessesByName(exe))
                    {
                        names.Add(p.ProcessName + "(" + p.Id + ")");
                    }
                }
                catch (Exception) { }
            }

            if (byWindow)
            {
                item.Detail = "检测到 " + cfg.GameWindowClass + " 窗口，pid=" +
                              string.Join(",", pids.ConvertAll(x => x.ToString()).ToArray());
            }
            else if (names.Count > 0)
            {
                item.Detail = "检测到进程 " + string.Join(", ", names.ToArray());
            }
            else
            {
                item.Level = CheckLevel.Warn;        // 没开游戏也能先起控制台
                item.Detail = "没检测到游戏窗口（" + cfg.GameWindowClass + "）；可以先开控制台再进游戏";
            }
            return item;
        }
    }
}
