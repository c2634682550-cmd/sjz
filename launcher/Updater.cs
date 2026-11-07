// 更新链路：拉 version.json → 验签 → 下载 → SHA-256 → 解压到临时目录
// → 原子替换 → 重启。顺序是硬约束：**验签不过，一个文件都不许落地**。
namespace SjzLauncher
{
    using System;
    using System.Collections.Generic;
    using System.IO;
    using System.IO.Compression;
    using System.Net;
    using System.Text;

    internal sealed class UpdateResult
    {
        public string Status = "disabled";    // disabled/ok/uptodate/rejected/failed/updated/rolledback
        public bool Ok;
        public bool RestartNeeded;
        public string Version = "";
        public string Reason = "";
        public string Sha256 = "";
        public string Notes = "";             // version.json 的更新说明

        public string ToJson()
        {
            return "{\"status\":\"" + Status + "\",\"ok\":" + (Ok ? "true" : "false")
                 + ",\"restart\":" + (RestartNeeded ? "true" : "false")
                 + ",\"version\":\"" + Escape(Version) + "\""
                 + ",\"sha256\":\"" + Escape(Sha256) + "\""
                 + ",\"notes\":\"" + Escape(Notes) + "\""
                 + ",\"reason\":\"" + Escape(Reason) + "\"}";
        }

        private static string Escape(string s)
        {
            return (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "").Replace("\n", " ");
        }
    }

    internal static class Updater
    {
        // 由 LauncherRuntime 注入：替换文件前先把控制台停干净。
        // frozen onedir 下 SJZ-Control.exe 与 _internal 里的 dll/pyd 都被占着，
        // 不停就换不动；旧 runtime 形态同样受益（webview/Edge 也不会挡路）。
        public static Action StopConsole;

        /// <summary>进程外更新（--update-only）时没有 Host 句柄：按 exe 路径把本目录的控制台停掉。</summary>
        private static void StopLocalConsole(LauncherConfig cfg)
        {
            string exe = Path.GetFullPath(Path.Combine(AppPaths.Root, "SJZ-Control.exe"));
            foreach (System.Diagnostics.Process p in System.Diagnostics.Process.GetProcessesByName("SJZ-Control"))
            {
                try
                {
                    if (!string.Equals(Path.GetFullPath(p.MainModule.FileName), exe,
                                       StringComparison.OrdinalIgnoreCase)) continue;
                    p.Kill();
                    p.WaitForExit(6000);
                }
                catch (Exception) { }
            }
            System.Threading.Thread.Sleep(600);
        }
        public static UpdateResult CheckAndApply(LauncherConfig cfg, bool licenseOk,
                                                 Action<string> log)
        {
            var res = new UpdateResult();
            if (string.IsNullOrEmpty(cfg.UpdateUrl))
            {
                res.Reason = "未配置更新源（launcher.config.json 的 update_url 为空）";
                return res;
            }
            if (!licenseOk)
            {
                // 顺序约束写死在代码里，而不是靠调用方记得先验许可。
                res.Status = "rejected";
                res.Reason = "许可未通过，按策略不进入更新流程";
                return res;
            }

            JsonNode ver = null;
            string text;
            try
            {
                text = FetchText(cfg.UpdateUrl, 8000);
                ver = Json.Parse(text);
            }
            catch (Exception e)
            {
                res.Status = "failed";
                res.Reason = "拉取 version.json 失败：" + e.Message;
                return res;
            }

            JsonNode payload = ver.Get("payload");
            JsonNode sig = ver.Get("sig");
            JsonNode meta;
            if (payload != null && payload.Kind == JsonKind.Object)
            {
                // 签名格式：签名覆盖 payload，验签不过一个文件都不许落地
                if (sig == null || sig.Kind != JsonKind.String)
                {
                    res.Status = "rejected";
                    res.Reason = "version.json 结构不对（缺 payload/sig）";
                    return res;
                }
                byte[] sigBytes;
                try { sigBytes = Crypto.FromBase64Strict(sig.Text); }
                catch (Exception) { res.Status = "rejected"; res.Reason = "version.json 签名不是合法 base64"; return res; }
                if (!Crypto.VerifyP256(Encoding.UTF8.GetBytes(Json.Canonical(payload)), sigBytes))
                {
                    res.Status = "rejected";
                    res.Reason = "version.json 验签失败（可能被中间人改过）";
                    return res;
                }
                meta = payload;
            }
            else if (!cfg.UpdateRequireSig)
            {
                // 明文格式（自建下载站/内测）：{version,url,sha256,notes}，仍校验 sha256
                meta = ver;
            }
            else
            {
                res.Status = "rejected";
                res.Reason = "version.json 缺 payload/sig，且未允许明文更新"
                        + "（launcher.config.json 的 update_require_sig=false 可放开）";
                return res;
            }

            res.Version = meta.GetString("version") ?? "";
            string url = meta.GetString("url") ?? "";
            string wantHash = (meta.GetString("sha256") ?? "").ToLowerInvariant();
            res.Notes = meta.GetString("notes") ?? "";
            if (res.Version.Length == 0 || url.Length == 0
                || (wantHash.Length != 64 && wantHash.Length != 32))
            {
                res.Status = "rejected";
                res.Reason = "version.json 字段不全（version/url/sha256）";
                return res;
            }
            if (CompareVersion(res.Version, AppVersion.Current) <= 0)
            {
                res.Status = "uptodate";
                res.Ok = true;
                res.Reason = "已是最新版本 " + AppVersion.Current;
                return res;
            }

            log("发现新版本 " + res.Version + "，开始下载");
            // 下载缓存放数据目录（可由 SJZ_DATA_DIR 整体搬走），并带 pid，
            // 避免两个实例/两轮测试撞同一个固定文件名。
            string zipPath = Path.Combine(AppPaths.DataDir,
                "update_" + Sanitize(res.Version) + "_" + AppPaths.Pid + ".zip");
            string partPath = zipPath + ".part";
            try
            {
                // 先落到 .part，下完再改名：下载中断只留半个 .part，
                // 不会被后续步骤当成完整包用（旧版直接写目标名的隐患）。
                TryDelete(partPath);
                Download(url, partPath);
                TryDelete(zipPath);
                File.Move(partPath, zipPath);
            }
            catch (Exception e)
            {
                TryDelete(partPath);
                TryDelete(zipPath);
                res.Status = "failed";
                res.Reason = "下载失败：" + e.Message;
                return res;
            }

            string got = Crypto.Hex(Crypto.Sha256(File.ReadAllBytes(zipPath)));
            res.Sha256 = got;
            // 允许两种声明：完整 64 位，或 128 位截断前缀（老 version.json 习惯）。
            bool hashOk = wantHash.Length == 64
                ? got == wantHash
                : got.Substring(0, 32) == wantHash;
            if (!hashOk)
            {
                res.Status = "rejected";
                res.Reason = string.Format("SHA-256 不符：期望 {0}，实际 {1}", wantHash, got);
                TryDelete(zipPath);
                return res;
            }

            string stage = Path.Combine(AppPaths.DataDir, "update_stage_" + Sanitize(res.Version));
            try
            {
                if (Directory.Exists(stage)) Directory.Delete(stage, true);
                Directory.CreateDirectory(stage);
                ExtractSafe(zipPath, stage);
                log("解压完成，准备原子替换");
                Action stop = StopConsole;
                if (stop == null) stop = delegate { StopLocalConsole(cfg); };
                log("替换前停控制台");
                stop();
                ApplyAtomic(stage, cfg);
                // 记一笔「已换上新版本但还没确认能跑」：下一次启动如果新版本
                // 起不来，就自动退回 update_backup（见 Rollback / 运行时钩子）。
                WriteState(res.Version, AppVersion.Current);
                res.Status = "updated";
                res.Ok = true;
                res.RestartNeeded = true;
                res.Reason = "已更新到 " + res.Version + "，需要重启启动器";
            }
            catch (Exception e)
            {
                res.Status = "failed";
                res.Reason = "更新落地失败：" + e.Message;
                return res;
            }
            finally
            {
                TryDelete(zipPath);
            }
            return res;
        }

        // ---------------- 更新状态 / 回滚 ----------------

        private static string StatePath()
        {
            return Path.Combine(AppPaths.DataDir, "update_state.json");
        }

        private static string BackupDir()
        {
            return Path.Combine(AppPaths.DataDir, "update_backup");
        }

        private static void WriteState(string version, string prev)
        {
            var sb = new StringBuilder();
            sb.Append("{\"version\":\"").Append(version ?? "")
              .Append("\",\"prev_version\":\"").Append(prev ?? "")
              .Append("\",\"backup\":\"").Append(BackupDir().Replace("\\", "\\\\"))
              .Append("\",\"confirmed\":false,\"boot_attempts\":0}");
            try { File.WriteAllText(StatePath(), sb.ToString(), Encoding.UTF8); }
            catch (Exception) { }
        }

        private static JsonNode ReadState()
        {
            try
            {
                if (!File.Exists(StatePath())) return null;
                return Json.Parse(File.ReadAllText(StatePath(), Encoding.UTF8));
            }
            catch (Exception) { return null; }
        }

        /// <summary>是否存在「已更新但尚未确认可运行」的版本。</summary>
        public static bool HasUnconfirmedUpdate()
        {
            JsonNode st = ReadState();
            if (st == null) return false;
            JsonNode c = st.Get("confirmed");
            return c == null || (c.Kind == JsonKind.Bool && !c.BoolValue);
        }

        /// <summary>新版本成功拉起控制台后调用：标记这次更新已确认，不再自动回退。</summary>
        public static void ConfirmRunning()
        {
            JsonNode st = ReadState();
            if (st == null) return;
            try
            {
                string v = st.GetString("version") ?? "";
                string p = st.GetString("prev_version") ?? "";
                File.WriteAllText(StatePath(),
                    "{\"version\":\"" + v + "\",\"prev_version\":\"" + p
                    + "\",\"confirmed\":true,\"boot_attempts\":0}",
                    Encoding.UTF8);
            }
            catch (Exception) { }
        }

        /// <summary>把 update_backup 里的上一版搬回根目录。失败时不动现有文件。</summary>
        public static UpdateResult Rollback(LauncherConfig cfg, Action<string> log)
        {
            var res = new UpdateResult();
            string backup = BackupDir();
            if (!Directory.Exists(backup) || Directory.GetFileSystemEntries(backup).Length == 0)
            {
                res.Status = "failed";
                res.Reason = "没有可回滚的上一版（update_backup 不存在或为空）";
                return res;
            }
            JsonNode st = ReadState();
            res.Version = st != null ? (st.GetString("prev_version") ?? "") : "";
            string err = null;
            int restored = 0;
            foreach (string name in Directory.GetFileSystemEntries(backup))
            {
                string leaf = Path.GetFileName(name);
                if (IsProtected(leaf, cfg)) continue;     // 用户数据/许可永不覆盖
                string target = Path.Combine(AppPaths.Root, leaf);
                try
                {
                    RemoveAny(target);
                    MoveAny(name, target);
                    restored++;
                }
                catch (Exception e)
                {
                    err = leaf + "：" + e.Message;
                    break;
                }
            }
            if (err != null)
            {
                res.Status = "failed";
                res.Reason = "回滚中断（" + err + "），已恢复 " + restored + " 项";
                return res;
            }
            try { if (Directory.Exists(backup)) Directory.Delete(backup, true); } catch (Exception) { }
            try { File.Delete(StatePath()); } catch (Exception) { }
            res.Status = "rolledback";
            res.Ok = true;
            res.RestartNeeded = true;
            res.Reason = "已回滚到上一版" + (res.Version.Length > 0 ? " " + res.Version : "")
                      + "（共恢复 " + restored + " 项）";
            log(res.Reason);
            return res;
        }

        // ---------------- 取文件 ----------------

        private static string FetchText(string url, int timeoutMs)
        {
            if (url.StartsWith("file://", StringComparison.OrdinalIgnoreCase))
                return File.ReadAllText(LocalPath(url), Encoding.UTF8);
            if (!url.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
                && !url.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
                return File.ReadAllText(LocalPath(url), Encoding.UTF8);   // 相对/绝对本地路径

            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Proxy = null;
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            using (var resp = (HttpWebResponse)req.GetResponse())
            using (var s = resp.GetResponseStream())
            using (var r = new StreamReader(s, Encoding.UTF8))
            {
                if (resp.StatusCode != HttpStatusCode.OK)
                    throw new IOException("HTTP " + (int)resp.StatusCode);
                return r.ReadToEnd();
            }
        }

        private static string LocalPath(string url)
        {
            if (url.StartsWith("file://", StringComparison.OrdinalIgnoreCase))
                return new Uri(url).LocalPath;
            return Path.IsPathRooted(url) ? url : Path.Combine(AppPaths.Root, url);
        }

        private static void Download(string url, string dest)
        {
            if (url.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
                || url.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
            {
                var req = (HttpWebRequest)WebRequest.Create(url);
                req.Proxy = null;
                req.Timeout = 30000;
                req.ReadWriteTimeout = 30000;
                using (var resp = (HttpWebResponse)req.GetResponse())
                using (var s = resp.GetResponseStream())
                using (var f = new FileStream(dest, FileMode.Create, FileAccess.Write))
                {
                    if (resp.StatusCode != HttpStatusCode.OK)
                        throw new IOException("HTTP " + (int)resp.StatusCode);
                    s.CopyTo(f);
                }
                return;
            }
            File.Copy(LocalPath(url), dest, true);
        }

        // ---------------- 解压（防路径穿越） ----------------

        public static void ExtractSafe(string zipPath, string destDir)
        {
            string root = Path.GetFullPath(destDir);
            string rootPrefix = root.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            using (ZipArchive zip = ZipFile.OpenRead(zipPath))
            {
                foreach (ZipArchiveEntry entry in zip.Entries)
                {
                    string name = entry.FullName.Replace('/', Path.DirectorySeparatorChar);
                    if (name.Length == 0) continue;
                    if (Path.IsPathRooted(name) || name.StartsWith("\\", StringComparison.Ordinal)
                        || name.IndexOf(':') >= 0 || name.IndexOf('\0') >= 0)
                        throw new IOException("压缩包条目非法（绝对路径/盘符/ADS）：" + entry.FullName);

                    string full = Path.GetFullPath(Path.Combine(root, name));
                    if (!full.StartsWith(rootPrefix, StringComparison.OrdinalIgnoreCase)
                        && !string.Equals(full, root, StringComparison.OrdinalIgnoreCase))
                        throw new IOException("压缩包条目越界（路径穿越）：" + entry.FullName);

                    if (entry.FullName.EndsWith("/") || entry.FullName.EndsWith("\\"))
                    {
                        Directory.CreateDirectory(full);
                        continue;
                    }
                    Directory.CreateDirectory(Path.GetDirectoryName(full));
                    using (var src = entry.Open())
                    using (var dst = new FileStream(full, FileMode.Create, FileAccess.Write))
                    {
                        src.CopyTo(dst);
                    }
                }
            }
        }

        // ---------------- 原子替换 ----------------

        private static void ApplyAtomic(string stage, LauncherConfig cfg)
        {
            string root = AppPaths.Root;
            string backup = BackupDir();
            // 先在 .tmp 里攒备份：新一次更新失败时，上一版备份（update_backup）
            // 仍然原样保留，不会因为这次失败而丢掉唯一的退路。
            string tmpBackup = backup + ".tmp";
            RemoveAny(tmpBackup);
            Directory.CreateDirectory(tmpBackup);

            var moved = new List<string>();     // 已经换上去的条目（顶层名）
            var backed = new List<string>();
            try
            {
                foreach (string name in Directory.GetFileSystemEntries(stage))
                {
                    string leaf = Path.GetFileName(name);
                    if (IsProtected(leaf, cfg))
                    {
                        continue;               // launcher/配置/数据/密钥都不许被覆盖
                    }
                    string target = Path.Combine(root, leaf);
                    if (File.Exists(target) || Directory.Exists(target))
                    {
                        string bak = Path.Combine(tmpBackup, leaf);
                        MoveAny(target, bak);
                        backed.Add(leaf);
                    }
                    MoveAny(name, target);
                    moved.Add(leaf);
                }
                // 全部换好才轮换备份目录
                RemoveAny(backup);
                Directory.Move(tmpBackup, backup);
            }
            catch (Exception)
            {
                // 回滚：把换上去的删掉，再从临时备份搬回来
                foreach (string leaf in moved)
                {
                    try { RemoveAny(Path.Combine(root, leaf)); } catch (Exception) { }
                }
                reverse(backed, root, tmpBackup);
                try { RemoveAny(tmpBackup); } catch (Exception) { }
                throw;
            }
        }

        private static void reverse(List<string> names, string root, string backup)
        {
            for (int i = names.Count - 1; i >= 0; i--)
            {
                string bak = Path.Combine(backup, names[i]);
                string target = Path.Combine(root, names[i]);
                try
                {
                    if (Directory.Exists(bak) || File.Exists(bak))
                    {
                        RemoveAny(target);
                        MoveAny(bak, target);
                    }
                }
                catch (Exception) { }
            }
        }

        private static bool IsProtected(string leaf, LauncherConfig cfg)
        {
            foreach (string p in cfg.ProtectedPaths)
                if (string.Equals(p, leaf, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }

        private static void MoveAny(string from, string to)
        {
            if (Directory.Exists(from)) Directory.Move(from, to);
            else File.Move(from, to);
        }

        private static void RemoveAny(string path)
        {
            if (Directory.Exists(path)) Directory.Delete(path, true);
            else if (File.Exists(path)) File.Delete(path);
        }

        private static void TryDelete(string path)
        {
            try { if (File.Exists(path)) File.Delete(path); } catch (Exception) { }
        }

        private static string Sanitize(string s)
        {
            var sb = new StringBuilder();
            foreach (char c in s)
                sb.Append(char.IsLetterOrDigit(c) || c == '.' || c == '-' ? c : '_');
            return sb.ToString();
        }

        /// <summary>返回 -1/0/1；只解析数字段，够用。</summary>
        public static int CompareVersion(string a, string b)
        {
            string[] pa = a.Split('.');
            string[] pb = b.Split('.');
            int n = Math.Max(pa.Length, pb.Length);
            for (int i = 0; i < n; i++)
            {
                int x = i < pa.Length ? ParseInt(pa[i]) : 0;
                int y = i < pb.Length ? ParseInt(pb[i]) : 0;
                if (x != y) return x < y ? -1 : 1;
            }
            return 0;
        }

        private static int ParseInt(string s)
        {
            int v = 0;
            for (int i = 0; i < s.Length; i++)
            {
                if (s[i] < '0' || s[i] > '9') break;
                v = v * 10 + (s[i] - '0');
            }
            return v;
        }
    }
}
