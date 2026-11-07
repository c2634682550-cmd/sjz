// WinForms 主窗口：状态 + 自检 + 日志 + 开关控制台。
// 窗口标题同时是单实例「拉前台」的查找键，别随手改。
namespace SjzLauncher
{
    using System;
    using System.Collections.Generic;
    using System.Diagnostics;
    using System.Drawing;
    using System.IO;
    using System.Threading;
    using System.Windows.Forms;

    internal sealed class LauncherForm : Form
    {
        public const string WindowTitle = "SJZ 启动器";

        private readonly LauncherConfig _cfg;
        private LauncherRuntime _rt;
        private readonly Label _licenseLabel;
        private readonly Label _machineLabel;
        private readonly Label _consoleLabel;
        private readonly ListView _checks;
        private readonly RichTextBox _log;
        private readonly Button _btnStart;
        private readonly Button _btnStop;
        private readonly Button _btnUpdate;
        private readonly System.Windows.Forms.Timer _timer;
        private bool _closing;
        private bool _busy;                       // 启动/停止进行中（按钮互斥）
        private volatile bool _refreshing;        // 后台刷新单飞，避免叠线程
        private string _fault;                    // 停止失败原因，必须一直显示到下次操作
        private string _lastStopText;             // 「已停止」要停 2 秒，别被 1Hz 刷新立刻覆盖
        private DateTime _lastStopAt = DateTime.MinValue;

        public LauncherForm()
        {
            _cfg = LauncherConfig.Load();
            Text = WindowTitle;
            ClientSize = new Size(620, 660);
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            Font = new Font("Microsoft YaHei UI", 9f);
            BackColor = Color.FromArgb(246, 247, 249);

            var root = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 1,
                RowCount = 6,
                Padding = new Padding(12),
            };
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 148));
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            Controls.Add(root);

            // --- 卡密状态 ---
            _licenseLabel = new Label
            {
                Text = "卡密：正在检查…",
                AutoSize = false,
                Height = 26,
                Dock = DockStyle.Fill,
                Font = new Font(Font, FontStyle.Bold),
            };
            root.Controls.Add(_licenseLabel, 0, 0);

            var machineRow = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true };
            _machineLabel = new Label { Text = "机器码：—", AutoSize = true, Padding = new Padding(0, 6, 8, 0) };
            var btnCopy = new Button { Text = "复制机器码", AutoSize = true, Height = 26 };
            btnCopy.Click += (s, e) =>
            {
                if (_rt != null && _rt.License != null)
                    Clipboard.SetText(_rt.License.MachineCode);
            };
            machineRow.Controls.Add(_machineLabel);
            machineRow.Controls.Add(btnCopy);
            root.Controls.Add(machineRow, 0, 1);

            // --- 自检 ---
            _checks = new ListView
            {
                Dock = DockStyle.Fill,
                View = View.Details,
                FullRowSelect = true,
                GridLines = true,
            };
            _checks.Columns.Add("项目", 120);
            _checks.Columns.Add("结果", 52);
            _checks.Columns.Add("详情", 400);
            root.Controls.Add(_checks, 0, 2);

            // --- 日志 ---
            _log = new RichTextBox
            {
                Dock = DockStyle.Fill,
                ReadOnly = true,
                BackColor = Color.White,
                BorderStyle = BorderStyle.FixedSingle,
                Font = new Font("Consolas", 9f),
                HideSelection = false,
            };
            root.Controls.Add(_log, 0, 3);

            // --- 按钮 ---
            var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, FlowDirection = FlowDirection.LeftToRight };
            _btnStart = new Button { Text = "启动控制台", Width = 104, Height = 30 };
            _btnStop = new Button { Text = "停止控制台", Width = 104, Height = 30, Enabled = false };
            _btnUpdate = new Button { Text = "检查更新", Width = 90, Height = 30 };
            var btnLog = new Button { Text = "打开日志", Width = 90, Height = 30 };
            var btnQuit = new Button { Text = "退出", Width = 72, Height = 30 };
            buttons.Controls.Add(_btnStart);
            buttons.Controls.Add(_btnStop);
            buttons.Controls.Add(_btnUpdate);
            buttons.Controls.Add(btnLog);
            buttons.Controls.Add(btnQuit);
            root.Controls.Add(buttons, 0, 4);

            _consoleLabel = new Label { Text = "控制台：未启动", AutoSize = true, Padding = new Padding(2, 6, 0, 0) };
            root.Controls.Add(_consoleLabel, 0, 5);

            _btnStart.Click += (s, e) => StartConsole();
            _btnStop.Click += (s, e) => StopConsole(false);
            _btnUpdate.Click += (s, e) => RunUpdateAsync();
            btnLog.Click += (s, e) =>
            {
                try { Process.Start("notepad.exe", AppPaths.LogFile); }
                catch (Exception) { }
            };
            btnQuit.Click += (s, e) => Close();

            FormClosing += OnClosing;
            Log.SetSink(OnLogLine);

            _timer = new System.Windows.Forms.Timer { Interval = 1000 };
            _timer.Tick += (s, e) => RefreshConsoleStateAsync();

            Shown += (s, e) =>
            {
                _timer.Start();
                ThreadPool.QueueUserWorkItem(_ => PrepareAsync());
            };
        }

        // ---------------- 日志 ----------------

        private void OnLogLine(string line)
        {
            if (_closing || IsDisposed) return;
            try
            {
                BeginInvoke((Action)(() =>
                {
                    if (_log.IsDisposed) return;
                    _log.AppendText(line + Environment.NewLine);
                    if (_log.TextLength > 200000) _log.Clear();
                    _log.SelectionStart = _log.TextLength;
                    _log.ScrollToCaret();
                }));
            }
            catch (Exception) { }
        }

        // ---------------- 启动流程 ----------------

        private void PrepareAsync()
        {
            _rt = new LauncherRuntime(_cfg, Crypto.NewToken());
            Log.Write("启动器版本 " + AppVersion.Current + "，token " + Crypto.MaskToken(_rt.Token));
            bool ok = _rt.Prepare(true, Log.Write);
            _rt.ProbeLegacyInstance();

            BeginInvoke((Action)(() =>
            {
                ApplyResult();
                _btnUpdate.Enabled = true;
                if (!ok) return;
                // 已经有遗留控制台在跑就别再拉一个，先把状态摆到界面上
                if (_rt.LegacyPid > 0)
                {
                    _btnStart.Enabled = false;
                    _btnStop.Enabled = true;
                    SetConsoleLabel("控制台：运行中 pid=" + _rt.LegacyPid
                                    + "（上一次启动器留下的，可停止）", Color.FromArgb(160, 110, 0));
                    return;
                }
                StartConsole();
                if (_rt.Update != null && _rt.Update.RestartNeeded) AskRestart();
            }));
        }

        private void ApplyResult()
        {
            if (_rt == null) return;
            LicenseResult lic = _rt.License;
            if (lic != null)
            {
                _licenseLabel.Text = "卡密：" + lic.Reason;
                _licenseLabel.ForeColor = lic.Allowed
                    ? Color.FromArgb(22, 130, 70)
                    : Color.FromArgb(190, 40, 40);
                _machineLabel.Text = "机器码：" + lic.MachineCode
                    + (lic.LicId.Length > 0 ? "    许可：" + lic.LicId : "");
            }
            _checks.BeginUpdate();
            _checks.Items.Clear();
            foreach (CheckItem c in _rt.Checks)
            {
                var item = new ListViewItem(new[] { c.Title, LevelText(c.Level), c.Detail });
                item.ForeColor = c.Level == CheckLevel.Fail ? Color.FromArgb(190, 40, 40)
                               : c.Level == CheckLevel.Warn ? Color.FromArgb(160, 110, 0)
                               : Color.FromArgb(22, 130, 70);
                _checks.Items.Add(item);
            }
            _checks.EndUpdate();
        }

        private static string LevelText(CheckLevel l)
        {
            return l == CheckLevel.Fail ? "失败" : l == CheckLevel.Warn ? "注意" : "通过";
        }

        // ---- 界面：所有重活都在后台线程，UI 线程只改文字和按钮 ----

        private void SetConsoleLabel(string text, Color color)
        {
            if (IsDisposed) return;
            _consoleLabel.Text = text;
            _consoleLabel.ForeColor = color;
        }

        /// <summary>
        /// 启动控制台：按钮立刻返回（界面 <50ms 可交互），拉起/探活全在后台。
        /// 原来这一整套（含 1.5s 的子进程定位）是同步跑在 UI 线程上的。
        /// </summary>
        private void StartConsole()
        {
            if (_rt == null || _busy || _closing) return;
            if (_rt.License != null && !_rt.License.Allowed)
            {
                MessageBox.Show(this, _rt.License.Reason + "\n\n机器码：" + _rt.License.MachineCode,
                                "卡密不可用", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }

            _busy = true;
            _btnStart.Enabled = false;
            _btnStop.Enabled = false;
            _fault = null;
            _lastStopText = null;
            SetConsoleLabel("控制台：正在启动…", Color.DimGray);

            ThreadPool.QueueUserWorkItem(_ =>
            {
                string err = null;
                bool ok = false;
                try { ok = _rt.StartConsole(out err); }
                catch (Exception e) { err = e.Message; }
                if (_closing || IsDisposed) return;
                BeginInvoke((Action)(() =>
                {
                    _busy = false;
                    if (!ok)
                    {
                        _btnStart.Enabled = true;
                        _btnStop.Enabled = false;
                        SetConsoleLabel("控制台：启动失败", Color.Firebrick);
                        Log.Write("!! 启动失败：" + (err ?? "未知原因"));
                        MessageBox.Show(this, err ?? "未知原因", "启动失败",
                                        MessageBoxButtons.OK, MessageBoxIcon.Error);
                        return;
                    }
                    _btnStop.Enabled = true;
                    _btnStart.Enabled = false;
                    SetConsoleLabel("控制台：启动中 pid=" + _rt.Host.Pid + "（等待就绪）",
                                    Color.FromArgb(160, 110, 0));
                    ThreadPool.QueueUserWorkItem(__ =>
                    {
                        _rt.VerifyNewVersion(15000, Log.Write);
                        if (!_closing && !IsDisposed) BeginInvoke((Action)(() => RefreshConsoleStateAsync()));
                    });
                }));
            });
        }

        /// <summary>
        /// 停止控制台：1 秒内返回界面，后台按「控制面 → 窗口 → 强制 → 权限」收尾，
        /// 结果（成功 / 强制 / 权限不足）必须回写到界面上。
        /// </summary>
        private void StopConsole(bool fromClosing)
        {
            if (_rt == null || _busy) return;
            _busy = true;
            _btnStop.Enabled = false;
            _btnStart.Enabled = false;
            _fault = null;
            _lastStopText = null;
            SetConsoleLabel("控制台：正在停止…", Color.DimGray);

            ThreadPool.QueueUserWorkItem(_ =>
            {
                PythonHost.StopOutcome r;
                try { r = _rt.ShutdownEx(fromClosing ? 4000 : 6000); }
                catch (Exception e)
                {
                    r = new PythonHost.StopOutcome();
                    r.Reason = e.Message;
                    r.ErrorCode = 1;
                }
                if (_closing || IsDisposed) return;
                BeginInvoke((Action)(() => ApplyStopOutcome(r)));
            });
        }

        private void ApplyStopOutcome(PythonHost.StopOutcome r)
        {
            _busy = false;
            Log.Write("停止结果：" + (r.Ok ? "成功" : "失败") + " — " + r.Reason
                      + "（耗时 " + r.ElapsedMs + "ms）");
            if (r.Ok)
            {
                _fault = null;
                _lastStopText = "控制台：已停止" + (r.Forced ? "（强制结束）" : "");
                _lastStopAt = DateTime.Now;
                _btnStart.Enabled = true;
                _btnStop.Enabled = false;
                SetConsoleLabel(_lastStopText, Color.DimGray);
                RefreshConsoleStateAsync();
                return;
            }

            // 失败必须让用户看见真实原因，并给一条能用的路
            _fault = "控制台：停止失败 — " + r.Reason;
            _btnStart.Enabled = false;
            _btnStop.Enabled = true;
            SetConsoleLabel(_fault, Color.Firebrick);
            if (r.NeedAdmin)
            {
                if (MessageBox.Show(this, r.Reason + "\n\n是否以管理员身份停止？（会弹一次 UAC）",
                                    "需要管理员权限", MessageBoxButtons.YesNo,
                                    MessageBoxIcon.Warning) == DialogResult.Yes)
                    StopAsAdmin(r.Pid);
            }
            else
            {
                MessageBox.Show(this, r.Reason, "停止失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        /// <summary>只把「极小的 killer」提权，启动器本身不提权。</summary>
        private void StopAsAdmin(int pid)
        {
            if (pid <= 0) return;
            try
            {
                var psi = new ProcessStartInfo(Application.ExecutablePath, "--kill-pid " + pid);
                psi.UseShellExecute = true;
                psi.Verb = "runas";
                Process.Start(psi);
                Log.Write("已请求以管理员身份终止 pid=" + pid);
                SetConsoleLabel("控制台：等待管理员确认…", Color.DimGray);
            }
            catch (Exception e)
            {
                Log.Write("提权终止被取消或失败：" + e.Message);
                MessageBox.Show(this, "没能以管理员身份停止：\n" + e.Message
                                    + "\n\n可以手动在「任务管理器」里结束 pid=" + pid,
                                "停止失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        /// <summary>
        /// 状态刷新：读状态（可能是一次本地 HTTP）放到后台，UI 只做赋值。
        /// 原来 1Hz 的定时器是在 UI 线程里直接发 HTTP，控制面一卡界面就跟着卡。
        /// </summary>
        private void RefreshConsoleStateAsync()
        {
            if (_rt == null || _closing || _refreshing) return;
            _refreshing = true;
            ThreadPool.QueueUserWorkItem(_ =>
            {
                try { _rt.RefreshStatus(); }
                catch (Exception) { }
                finally { _refreshing = false; }
                if (_closing || IsDisposed) return;
                try { BeginInvoke((Action)(ApplyConsoleState)); }
                catch (Exception) { }
            });
        }

        private void ApplyConsoleState()
        {
            if (_rt == null || _closing || IsDisposed) return;
            if (_busy) return;                       // 启动/停止进行中，别抢着翻状态
            bool legacy = _rt.LegacyPid > 0 && Native.ProcessAlive(_rt.LegacyPid);
            bool running = _rt.Host.Running || legacy;
            _btnStart.Enabled = !running;
            _btnStop.Enabled = running;
            if (!running)
            {
                _fault = null;
                _rt.ClearLegacy();
                // 停止刚成功时先显示「已停止」2 秒，再退回「未启动」
                if (_lastStopText != null &&
                    (DateTime.Now - _lastStopAt).TotalSeconds < 2.0)
                {
                    SetConsoleLabel(_lastStopText, Color.DimGray);
                    return;
                }
                _lastStopText = null;
                SetConsoleLabel("控制台：未启动", Color.DimGray);
                return;
            }
            // 停止失败的提示要压过每秒刷新，否则「权限不足」1 秒就被「运行中」盖掉
            if (_fault != null)
            {
                _btnStart.Enabled = false;
                _btnStop.Enabled = true;
                SetConsoleLabel(_fault, Color.Firebrick);
                return;
            }
            if (legacy)
            {
                SetConsoleLabel("控制台：运行中 pid=" + _rt.LegacyPid + "（上一次启动器留下的）",
                                Color.FromArgb(160, 110, 0));
                return;
            }
            string extra = "";
            JsonNode st = _rt.LastStatus;
            if (st != null)
            {
                string lic = null;
                JsonNode l = st.Get("license");
                if (l != null && l.GetString("status") != null) lic = l.GetString("status");
                long up = 0;
                st.TryLong("uptime_s", out up);
                extra = string.Format("，端口 {0}，已运行 {1}s，卡密 {2}",
                                      _rt.CtrlPort, up, lic ?? "?");
            }
            SetConsoleLabel("控制台：运行中 pid=" + _rt.Host.Pid + extra,
                            Color.FromArgb(22, 130, 70));
        }

        private void RunUpdateAsync()
        {
            if (_rt == null || _busy) return;
            _btnUpdate.Enabled = false;
            _btnStart.Enabled = false;
            _btnStop.Enabled = false;
            SetConsoleLabel("控制台：准备更新…", Color.DimGray);

            // 替换正在运行的 exe/dll 一定失败，所以先停控制台再更新；
            // 更新完如果本来就是运行状态，用新文件重新拉起来。
            // 停止放到后台线程里，UI 不再被挡住。
            ThreadPool.QueueUserWorkItem(_ =>
            {
                bool wasRunning = _rt.Host.Running || _rt.LegacyPid > 0;
                if (wasRunning)
                {
                    try { _rt.ShutdownEx(4000); } catch (Exception) { }
                }
                bool ok = _rt.License != null && _rt.License.Allowed;
                UpdateResult r = Updater.CheckAndApply(_cfg, ok, Log.Write);
                if (_closing || IsDisposed) return;
                BeginInvoke((Action)(() =>
                {
                    _btnUpdate.Enabled = true;
                    if (wasRunning && !_rt.Host.Running)
                    {
                        _btnStart.Enabled = true;
                        _btnStop.Enabled = false;
                    }
                    MessageBox.Show(this, r.Status + "：" + r.Reason
                                        + (r.Notes.Length > 0 ? "\n\n更新说明：\n" + r.Notes : ""),
                                    r.Ok ? "更新" : "更新未应用", MessageBoxButtons.OK,
                                    r.Ok ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
                    if (r.RestartNeeded && r.Ok && wasRunning)
                    {
                        // 控制台已经在前面停掉了，直接用新文件拉起来即可，
                        // 不必重启启动器本身（launcher\ 与配置都在保护名单里）。
                        StartConsole();
                    }
                    else if (r.RestartNeeded) AskRestart();
                    else if (wasRunning && !_rt.Host.Running) StartConsole();
                }));
            });
        }

        private void AskRestart()
        {
            if (MessageBox.Show(this, "更新已就位，现在重启启动器？", "重启",
                                MessageBoxButtons.YesNo, MessageBoxIcon.Question)
                == DialogResult.Yes)
            {
                try { Process.Start(Application.ExecutablePath); } catch (Exception) { }
                _closing = true;
                Close();
            }
        }

        // ---------------- 退出 ----------------

        private void OnClosing(object sender, FormClosingEventArgs e)
        {
            if (_closing) return;
            bool running = _rt != null && _rt.Host.Running;
            if (running)
            {
                if (MessageBox.Show(this, "控制台还在运行，退出会把一起关掉。继续？", "退出确认",
                                    MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
                {
                    e.Cancel = true;
                    return;
                }
            }
            _closing = true;
            _timer.Stop();
            Log.SetSink(null);

            // 窗口立刻消失，收尾全放后台：最多等 1.2s 优雅退出，之后 Job 的
            // KILL_ON_JOB_CLOSE 兜底把整棵树带走。退出不再卡在 UI 线程上。
            e.Cancel = true;
            Hide();
            ThreadPool.QueueUserWorkItem(_ =>
            {
                try { if (_rt != null) _rt.ShutdownEx(1200); } catch (Exception) { }
                Native.TerminateAllChildren();
                try { BeginInvoke((Action)(() => Close())); } catch (Exception) { }
            });
        }
    }
}
