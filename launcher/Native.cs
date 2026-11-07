// Win32 封装：全局互斥体、Job 对象（防孤儿）、窗口找前台、驱动签名验证。
namespace SjzLauncher
{
    using System;
    using System.Collections.Generic;
    using System.IO;
    using System.Runtime.InteropServices;
    using System.Text;

    internal static class Native
    {
        // ---------------- 互斥体 ----------------

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern IntPtr CreateMutexW(IntPtr attrs, bool initialOwner, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        private const int ErrorAlreadyExists = 183;

        private static IntPtr _mutex = IntPtr.Zero;

        /// <summary>全局单实例闸。true = 我是第一个；false = 已经有一个在跑。</summary>
        public static bool AcquireSingleInstance(string name)
        {
            if (_mutex != IntPtr.Zero) return true;
            IntPtr h = CreateMutexW(IntPtr.Zero, true, name);
            if (h == IntPtr.Zero) return false;
            _mutex = h;
            return Marshal.GetLastWin32Error() != ErrorAlreadyExists;
        }

        public static void ReleaseSingleInstance()
        {
            if (_mutex == IntPtr.Zero) return;
            try { CloseHandle(_mutex); } catch (Exception) { }
            _mutex = IntPtr.Zero;
        }

        // ---------------- 窗口 ----------------

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr FindWindowW(string cls, string title);

        [DllImport("user32.dll")]
        private static extern bool ShowWindow(IntPtr hWnd, int cmd);

        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr hWnd);

        [DllImport("user32.dll")]
        private static extern bool IsIconic(IntPtr hWnd);

        public const int SwRestore = 9;

        /// <summary>把已存在的启动器窗口拉到前台（第二次双击）。</summary>
        public static bool ActivateExisting(string title, int retries)
        {
            for (int i = 0; i < retries; i++)
            {
                IntPtr h = FindWindowW(null, title);
                if (h != IntPtr.Zero)
                {
                    if (IsIconic(h)) ShowWindow(h, SwRestore);
                    ShowWindow(h, SwRestore);
                    SetForegroundWindow(h);
                    return true;
                }
                System.Threading.Thread.Sleep(150);
            }
            return false;
        }

        // ---------------- 进程存活 ----------------

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint access, bool inherit, int pid);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr handle, uint ms);

        private const uint Synchronize = 0x00100000;
        private const uint WaitTimeout = 0x00000102;

        /// <summary>
        /// 停止成功后把 instance.json 的「运行中」登记删掉（pid 必须对得上），
        /// 免得下次打开启动器还把已经死掉的 pid 当成在跑。
        /// </summary>
        public static void ClearInstanceIfMatches(int pid)
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

        /// <summary>pid 还活着吗？打不开（权限不足，错误码 5）按「活着」——提权进程本来
        /// 就杀不掉也问不到，误判成「已退出」正是旧版静默吞掉失败的根源。
        /// 只有 pid 真的不存在（错误码 87）才返回 false。
        /// </summary>
        public static bool ProcessAlive(int pid)
        {
            if (pid <= 0) return false;
            IntPtr h = OpenProcess(Synchronize, false, pid);
            if (h == IntPtr.Zero)
                return Marshal.GetLastWin32Error() == 5;
            try { return WaitForSingleObject(h, 0) == WaitTimeout; }
            finally { CloseHandle(h); }
        }

        // ---------------- Job 对象 ----------------

        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters
        {
            public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
            public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct BasicLimitInformation
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedLimitInformation
        {
            public BasicLimitInformation BasicLimitInformation;
            public IoCounters IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern IntPtr CreateJobObjectW(IntPtr attrs, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(IntPtr job, int infoClass,
                                                           IntPtr info, uint length);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

        private const int JobObjectExtendedLimitInformation = 9;
        private const uint JobObjectLimitKillOnJobClose = 0x2000;

        private static IntPtr _job = IntPtr.Zero;

        /// <summary>
        /// 建一个 KILL_ON_JOB_CLOSE 的 Job。启动器无论怎么死（包括任务管理器
        /// 结束），句柄一关内核就把整棵子进程树带走 —— 这是「无孤儿」的兜底。
        /// </summary>
        public static bool EnsureJob()
        {
            if (_job != IntPtr.Zero) return true;
            IntPtr job = CreateJobObjectW(IntPtr.Zero, null);
            if (job == IntPtr.Zero) return false;

            var info = new ExtendedLimitInformation();
            info.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
            int size = Marshal.SizeOf(typeof(ExtendedLimitInformation));
            IntPtr p = Marshal.AllocHGlobal(size);
            try
            {
                Marshal.StructureToPtr(info, p, false);
                if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, p, (uint)size))
                {
                    CloseHandle(job);
                    return false;
                }
            }
            finally { Marshal.FreeHGlobal(p); }
            _job = job;
            return true;
        }

        public static bool AssignToJob(IntPtr processHandle)
        {
            return _job != IntPtr.Zero && processHandle != IntPtr.Zero
                && AssignProcessToJobObject(_job, processHandle);
        }

        public static void TerminateAllChildren()
        {
            KillDescendants(System.Diagnostics.Process.GetCurrentProcess().Id);
            if (_job == IntPtr.Zero) return;
            int self = System.Diagnostics.Process.GetCurrentProcess().Id;
            foreach (int pid in JobProcessIds())
            {
                if (pid == self || !ProcessAlive(pid)) continue;
                IntPtr h = OpenProcess(ProcessTerminate, false, pid);
                if (h == IntPtr.Zero) continue;
                try { TerminateProcess(h, 1); }
                finally { CloseHandle(h); }
            }
        }

        private const uint ProcessTerminate = 0x0001;

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateProcess(IntPtr process, uint code);

        // ------------------------------------------------ 停止：窗口 → 进程 → 权限
        // 停止失败的三层原因（现场实测 2026-11-07）：
        //   ① 提权实例：非提权启动器 OpenProcess 拿到拒绝访问，代码把失败吞了；
        //   ② 无窗口僵尸：窗口销毁了但进程还活着，任何「给窗口发 WM_CLOSE」都无效；
        //   ③ 失败无反馈：界面照旧显示「运行中」。
        // 所以这里按「窗口 → 进程 → 权限」三级，每一步都能报告真实结果。

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool PostMessageW(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

        private const uint WmClose = 0x0010;

        /// <summary>
        /// 给该 pid 的所有顶层窗口发 WM_CLOSE（覆盖隐藏窗口，僵尸实例没有窗口时返回 0）。
        /// </summary>
        public static int PostCloseToPid(int pid)
        {
            int sent = 0;
            EnumWindows((hWnd, l) =>
            {
                uint owner;
                GetWindowThreadProcessId(hWnd, out owner);
                if ((int)owner == pid && PostMessageW(hWnd, WmClose, IntPtr.Zero, IntPtr.Zero))
                    sent++;
                return true;
            }, IntPtr.Zero);
            return sent;
        }

        /// <summary>终止单个进程：0 = 成功；否则返回 Win32 错误码（5 = 拒绝访问）。</summary>
        public static int TerminatePid(int pid)
        {
            IntPtr h = OpenProcess(ProcessTerminate, false, pid);
            if (h == IntPtr.Zero) return Marshal.GetLastWin32Error();
            try
            {
                if (TerminateProcess(h, 1)) return 0;
                return Marshal.GetLastWin32Error();
            }
            finally { CloseHandle(h); }
        }

        /// <summary>等 pid 退出；返回是否已退出。</summary>
        public static bool WaitExit(int pid, int ms)
        {
            DateTime end = DateTime.Now.AddMilliseconds(ms);
            while (DateTime.Now < end)
            {
                if (!ProcessAlive(pid)) return true;
                System.Threading.Thread.Sleep(50);
            }
            return !ProcessAlive(pid);
        }

        /// <summary>按父子关系收集 rootPid 的整棵子树（不含 root 自己）。</summary>
        public static int[] DescendantsOf(int rootPid)
        {
            Dictionary<int, int> parents = ParentMap();
            List<int> targets = new List<int>();
            List<int> frontier = new List<int>();
            frontier.Add(rootPid);
            while (frontier.Count > 0)
            {
                List<int> next = new List<int>();
                foreach (KeyValuePair<int, int> kv in parents)
                    if (frontier.Contains(kv.Value) && kv.Key != rootPid
                        && !targets.Contains(kv.Key))
                    {
                        targets.Add(kv.Key);
                        next.Add(kv.Key);
                    }
                frontier = next;
            }
            return targets.ToArray();
        }

        /// <summary>
        /// 终止整棵树（先子后父）。返回首个错误码（0 = 全部成功），killed = 成功终止数。
        /// </summary>
        public static int TerminateTree(int rootPid, out int killed)
        {
            killed = 0;
            int firstErr = 0;
            foreach (int pid in DescendantsOf(rootPid))
            {
                int err = TerminatePid(pid);
                if (err == 0) killed++;
                else if (firstErr == 0) firstErr = err;
            }
            int self = TerminatePid(rootPid);
            if (self == 0) killed++;
            else if (firstErr == 0) firstErr = self;
            return firstErr;
        }

        // ------------------------------------------------ 进程树（不依赖 Job 继承）
        // venv 的 pythonw.exe 会 breakaway 出 Job 再 exec 真解释器，
        // 只靠 Job 会漏掉真身；用 Toolhelp 快照按父子关系自己算树。

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct ProcessEntry32W
        {
            public uint Size;
            public uint Usage;
            public uint ProcessId;
            public IntPtr DefaultHeap;
            public uint ModuleId;
            public uint Threads;
            public uint ParentProcessId;
            public int PriClassBase;
            public uint Flags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
            public string ExeFile;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint processId);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool Process32FirstW(IntPtr snapshot, ref ProcessEntry32W entry);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool Process32NextW(IntPtr snapshot, ref ProcessEntry32W entry);

        private const uint Th32csSnapProcess = 0x00000002;

        private static Dictionary<int, int> ParentMap()
        {
            Dictionary<int, int> map = new Dictionary<int, int>();
            IntPtr snap = CreateToolhelp32Snapshot(Th32csSnapProcess, 0);
            if (snap == (IntPtr)(-1)) return map;
            try
            {
                ProcessEntry32W e = new ProcessEntry32W();
                e.Size = (uint)Marshal.SizeOf(typeof(ProcessEntry32W));
                if (Process32FirstW(snap, ref e))
                {
                    do { map[(int)e.ProcessId] = (int)e.ParentProcessId; }
                    while (Process32NextW(snap, ref e));
                }
            }
            finally { CloseHandle(snap); }
            return map;
        }

        /// <summary>找 parentPid 的活子进程（壳进程再 exec 一层时用）。</summary>
        public static int FindChildPid(int parentPid, int timeoutMs)
        {
            DateTime end = DateTime.Now.AddMilliseconds(timeoutMs);
            while (DateTime.Now < end)
            {
                foreach (KeyValuePair<int, int> kv in ParentMap())
                    if (kv.Value == parentPid && ProcessAlive(kv.Key)) return kv.Key;
                System.Threading.Thread.Sleep(100);
            }
            return 0;
        }

        /// <summary>按父子关系终止整棵树（不杀 root 自己）。</summary>
        public static int KillDescendants(int rootPid)
        {
            Dictionary<int, int> parents = ParentMap();
            List<int> targets = new List<int>();
            List<int> frontier = new List<int>();
            frontier.Add(rootPid);
            while (frontier.Count > 0)
            {
                List<int> next = new List<int>();
                foreach (KeyValuePair<int, int> kv in parents)
                    if (frontier.Contains(kv.Value) && kv.Key != rootPid
                        && !targets.Contains(kv.Key))
                    {
                        targets.Add(kv.Key);
                        next.Add(kv.Key);
                    }
                frontier = next;
            }
            int killed = 0;
            foreach (int pid in targets)
            {
                IntPtr h = OpenProcess(ProcessTerminate, false, pid);
                if (h == IntPtr.Zero) continue;
                try { if (TerminateProcess(h, 1)) killed++; }
                finally { CloseHandle(h); }
            }
            return killed;
        }

        /// <summary>
        /// 把启动器自己先塞进 Job，再拉子进程 —— 子进程才会继承 Job。
        /// 先拉子进程后 Assign 会漏掉「壳进程已经再 exec 一层」的真身。
        /// </summary>
        public static bool AdoptSelfIntoJob()
        {
            if (!EnsureJob()) return false;
            System.Diagnostics.Process self = System.Diagnostics.Process.GetCurrentProcess();
            return AssignToJob(self.Handle);
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool QueryInformationJobObject(IntPtr job, int infoClass,
                                                             byte[] info, uint length,
                                                             out uint retLen);

        private const int JobObjectBasicProcessIdList = 3;

        /// <summary>Job 里当前所有 pid（用于识别「包装进程」背后的真身）。</summary>
        public static int[] JobProcessIds()
        {
            if (_job == IntPtr.Zero) return new int[0];
            byte[] buf = new byte[8 + 4 * 4096];
            uint ret;
            if (!QueryInformationJobObject(_job, JobObjectBasicProcessIdList, buf,
                                           (uint)buf.Length, out ret))
                return new int[0];
            int count = BitConverter.ToInt32(buf, 4);
            if (count <= 0 || count > 4096) return new int[0];
            int[] pids = new int[count];
            for (int i = 0; i < count; i++)
                pids[i] = BitConverter.ToInt32(buf, 8 + 4 * i);
            return pids;
        }

        /// <summary>
        /// 等 Job 里出现一个不是 excludePid 的活进程并返回它。
        /// 有些 pythonw.exe（virtualenv/uv 的壳）会再 exec 一层真解释器，
        /// Process.Start 拿到的是壳的 pid，直接拿它当控制台 pid 会全盘错位。
        /// </summary>
        public static int FindNewChild(int excludePid, int timeoutMs)
        {
            int self = System.Diagnostics.Process.GetCurrentProcess().Id;
            DateTime end = DateTime.Now.AddMilliseconds(timeoutMs);
            while (DateTime.Now < end)
            {
                foreach (int p in JobProcessIds())
                    if (p != excludePid && p != self && ProcessAlive(p)) return p;
                System.Threading.Thread.Sleep(100);
            }
            return 0;
        }

        // ---------------- 游戏窗口（Unreal） ----------------

        private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern bool EnumWindows(EnumWindowsProc proc, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern bool IsWindowVisible(IntPtr hWnd);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetClassNameW(IntPtr hWnd, StringBuilder sb, int max);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);

        /// <summary>有没有可见的 Unreal 窗口（游戏在跑）。</summary>
        public static bool HasWindowOfClass(string className, out List<int> pids)
        {
            var found = new List<int>();
            EnumWindows((hWnd, l) =>
            {
                if (!IsWindowVisible(hWnd)) return true;
                var sb = new StringBuilder(256);
                GetClassNameW(hWnd, sb, sb.Capacity);
                if (string.Equals(sb.ToString(), className, StringComparison.OrdinalIgnoreCase))
                {
                    uint pid;
                    GetWindowThreadProcessId(hWnd, out pid);
                    if (pid != 0) found.Add((int)pid);
                }
                return true;
            }, IntPtr.Zero);
            pids = found;
            return found.Count > 0;
        }

        // ---------------- 驱动签名 ----------------

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct WintrustFileInfo
        {
            public uint cbStruct;
            public IntPtr pcwszFilePath;
            public IntPtr hFile;
            public IntPtr pgKnownSubject;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct WintrustData
        {
            public uint cbStruct;
            public IntPtr pPolicyCallbackData;
            public IntPtr pSIPClientData;
            public uint dwUIChoice;
            public uint fdwRevocationChecks;
            public uint dwUnionChoice;
            public IntPtr pFile;
            public uint dwStateAction;
            public IntPtr hWVTStateData;
            public IntPtr pwszURLReference;
            public uint dwProvFlags;
            public uint dwUIContext;
            public IntPtr pSignatureSettings;
        }

        [DllImport("wintrust.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr WinVerifyTrust(IntPtr hwnd, Guid actionId, IntPtr data);

        // WINTRUST_ACTION_GENERIC_VERIFY_V2
        private static readonly Guid GenericVerifyV2 =
            new Guid("00AAC56B-CD44-11D0-8CC2-00C04FC295EE");

        private const uint WtdUiNone = 2;
        private const uint WtdRevokeNone = 0;
        private const uint WtdChoiceFile = 1;
        private const uint WtdStateActionVerify = 1;
        private const uint WtdStateActionClose = 2;

        /// <summary>验证 .sys/.exe 的 Authenticode 签名，返回 0 = 可信。</summary>
        public static int VerifyTrustStatus(string path)
        {
            IntPtr pFile = IntPtr.Zero, pData = IntPtr.Zero, pPath = IntPtr.Zero;
            WintrustData data = new WintrustData();
            WintrustFileInfo fi = new WintrustFileInfo();
            try
            {
                pPath = Marshal.StringToHGlobalUni(path);
                fi.cbStruct = (uint)Marshal.SizeOf(typeof(WintrustFileInfo));
                fi.pcwszFilePath = pPath;
                pFile = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(WintrustFileInfo)));
                Marshal.StructureToPtr(fi, pFile, false);

                data.cbStruct = (uint)Marshal.SizeOf(typeof(WintrustData));
                data.dwUIChoice = WtdUiNone;
                data.fdwRevocationChecks = WtdRevokeNone;
                data.dwUnionChoice = WtdChoiceFile;
                data.pFile = pFile;
                data.dwStateAction = WtdStateActionVerify;

                pData = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(WintrustData)));
                Marshal.StructureToPtr(data, pData, false);

                IntPtr result = WinVerifyTrust(IntPtr.Zero, GenericVerifyV2, pData);
                int hr = (int)(long)result;

                // 关闭状态句柄，否则 wintrust 会一直占着文件
                data.dwStateAction = WtdStateActionClose;
                Marshal.StructureToPtr(data, pData, false);
                WinVerifyTrust(IntPtr.Zero, GenericVerifyV2, pData);
                return hr;
            }
            catch (Exception)
            {
                return unchecked((int)0x80000000);     // 未知 = 不可信
            }
            finally
            {
                if (pPath != IntPtr.Zero) Marshal.FreeHGlobal(pPath);
                if (pFile != IntPtr.Zero) Marshal.FreeHGlobal(pFile);
                if (pData != IntPtr.Zero) Marshal.FreeHGlobal(pData);
            }
        }
    }
}
