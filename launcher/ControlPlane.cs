// 启动器 ⇄ 控制台 的本地控制面客户端。
// 只走 127.0.0.1，token 放在 X-SJZ-Token 头里；token 由启动器随机生成，
// 只通过命令行交给 python 子进程，不写文件、不进日志。
namespace SjzLauncher
{
    using System;
    using System.Collections.Generic;
    using System.IO;
    using System.Net;
    using System.Text;

    internal static class ControlPlane
    {
        public static string Url(int port, string path)
        {
            return "http://127.0.0.1:" + port + path;
        }

        private static HttpWebRequest NewRequest(int port, string path, string token, int timeoutMs)
        {
            var req = (HttpWebRequest)WebRequest.Create(Url(port, path));
            req.Method = "GET";
            req.Proxy = null;                 // 别经过系统代理（本地回环没这个必要）
            req.KeepAlive = false;
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            req.Headers["X-SJZ-Token"] = token;
            req.Accept = "application/json";
            return req;
        }

        private static JsonNode ReadJson(HttpWebResponse resp)
        {
            using (resp)
            using (var s = resp.GetResponseStream())
            using (var r = new StreamReader(s, Encoding.UTF8))
            {
                string text = r.ReadToEnd();
                return text.Length == 0 ? null : Json.Parse(text);
            }
        }

        public static JsonNode GetStatus(int port, string token, int timeoutMs)
        {
            try
            {
                HttpWebRequest req = NewRequest(port, "/api/ctrl/status", token, timeoutMs);
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                {
                    if (resp.StatusCode != HttpStatusCode.OK) return null;
                    return ReadJson(resp);
                }
            }
            catch (Exception)
            {
                return null;                  // 没起 / 端口不对 / 被拒 —— 都当「查不到」
            }
        }

        public static bool PostStop(int port, string token, int timeoutMs)
        {
            try
            {
                HttpWebRequest req = NewRequest(port, "/api/ctrl/stop", token, timeoutMs);
                req.Method = "POST";
                req.ContentType = "application/json";
                byte[] body = Encoding.UTF8.GetBytes("{}");
                req.ContentLength = body.Length;
                using (var s = req.GetRequestStream()) s.Write(body, 0, body.Length);
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                {
                    return resp.StatusCode == HttpStatusCode.OK;
                }
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>
        /// 端口上到底有没有进程在听：只做 TCP 握手，probeMs 上限。
        /// 实测这台机器（装有安全软件）对「没人监听的 127.0.0.1 端口」，HTTP 请求
        /// 不会立刻被拒，而是一路等到超时（824ms/端口）；串行扫 20 个端口于是变成
        /// 16 秒 —— 这段代码原来跑在 UI 线程上，就是「一点启动整机卡十几秒」的根因。
        /// 先并行做 TCP 探测，只对真正在听的端口再发 HTTP。
        /// </summary>
        private static bool TcpListening(int port, int probeMs)
        {
            var c = new System.Net.Sockets.TcpClient();
            try
            {
                IAsyncResult ar = c.BeginConnect(System.Net.IPAddress.Loopback, port, null, null);
                if (!ar.AsyncWaitHandle.WaitOne(probeMs)) return false;
                c.EndConnect(ar);
                return true;
            }
            catch (Exception) { return false; }
            finally { try { c.Close(); } catch (Exception) { } }
        }

        private const int ProbeMs = 200;        // 单个端口的 TCP 探测上限
        private const int StatusMs = 400;       // 本地回环的状态查询上限（原来 1500ms 太长）

        /// <summary>
        /// 找控制台端口：desktop.py 的端口可能被占后退一格（8777~8796）。
        /// 并行 TCP 预探（≤ProbeMs）→ 只对在听的端口做 HTTP 校验。
        /// 最坏总耗时 ≈ ProbeMs + StatusMs，不再随端口数线性变长。
        /// </summary>
        public static int Discover(int pythonPid, string token, int basePort, int span, out JsonNode status)
        {
            status = null;
            if (token == null || token.Length == 0) return 0;
            if (span <= 0) span = 1;

            var listening = new List<int>();
            object gate = new object();
            int pending = span;
            var done = new System.Threading.ManualResetEvent(false);
            for (int i = 0; i < span; i++)
            {
                int port = basePort + i;
                System.Threading.Thread th = new System.Threading.Thread(
                    new System.Threading.ThreadStart(delegate()
                    {
                        try
                        {
                            if (TcpListening(port, ProbeMs))
                                lock (gate) listening.Add(port);
                        }
                        catch (Exception) { }
                        finally
                        {
                            if (System.Threading.Interlocked.Decrement(ref pending) == 0)
                                done.Set();
                        }
                    }));
                th.IsBackground = true;
                th.Name = "ctrl-probe-" + port;
                th.Start();
            }
            done.WaitOne(ProbeMs + 120);

            List<int> ports;
            lock (gate) ports = new List<int>(listening);
            ports.Sort();

            foreach (int port in ports)
            {
                JsonNode st = GetStatus(port, token, StatusMs);
                if (st == null) continue;
                long pid;
                if (!st.TryLong("pid", out pid) || pid != pythonPid) continue;
                status = st;
                return port;
            }
            return 0;
        }

        /// <summary>已知道端口时的状态查询（用较短的超时，避免拖 UI）。</summary>
        public static JsonNode StatusFast(int port, string token)
        {
            return port <= 0 ? null : GetStatus(port, token, StatusMs);
        }
    }
}
