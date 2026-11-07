// 机器指纹（六特征）。必须与 core/license.py 完全同口径：
//   bios_uuid / machine_guid / board_serial / cpu_id / volume_serial / mac
//   vec[k] = sha256("sjz-fp:{k}:{norm(v)")[:32]
//   hash   = sha256(join("|", sorted "k=v"))
// 任何一处口径不同，启动器就会把合法卡密判成 fp_mismatch。
namespace SjzLauncher
{
    using System;
    using System.Collections.Generic;
    using System.Globalization;
    using System.Runtime.InteropServices;
    using System.Text;
    using Microsoft.Win32;

    internal sealed class FingerprintResult
    {
        public string Hash;
        public Dictionary<string, string> Vec = new Dictionary<string, string>(StringComparer.Ordinal);
        public Dictionary<string, int> Weights = new Dictionary<string, int>(StringComparer.Ordinal);
        public List<string> ComponentsPresent = new List<string>();

        /// <summary>机器码：指纹哈希前 32 位，每 4 位分组，便于口头报给客服。</summary>
        public string MachineCode()
        {
            string raw = (Hash ?? "").Substring(0, Math.Min(32, (Hash ?? "").Length)).ToUpperInvariant();
            var parts = new List<string>();
            for (int i = 0; i + 4 <= raw.Length; i += 4) parts.Add(raw.Substring(i, 4));
            return string.Join("-", parts.ToArray());
        }

        public string ToJson()
        {
            var sb = new StringBuilder();
            sb.Append("{\"hash\":\"").Append(Hash).Append("\",\"components_present\":[");
            for (int i = 0; i < ComponentsPresent.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append('"').Append(ComponentsPresent[i]).Append('"');
            }
            sb.Append("],\"vec\":{");
            var keys = new List<string>(Vec.Keys);
            keys.Sort(StringComparer.Ordinal);
            for (int i = 0; i < keys.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append('"').Append(keys[i]).Append("\":\"").Append(Vec[keys[i]]).Append('"');
            }
            sb.Append("},\"machine_code\":\"").Append(MachineCode()).Append("\"}");
            return sb.ToString();
        }
    }

    internal static class Fingerprint
    {
        public static readonly Dictionary<string, int> Weights =
            new Dictionary<string, int>(StringComparer.Ordinal)
            {
                { "bios_uuid", 30 },
                { "machine_guid", 25 },
                { "board_serial", 15 },
                { "cpu_id", 15 },
                { "volume_serial", 10 },
                { "mac", 5 },
            };

        // ---------------- P/Invoke ----------------

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint GetSystemFirmwareTable(uint provider, uint tableId,
                                                          byte[] buffer, uint bufferSize);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern bool GetVolumeInformationW(string root, StringBuilder volumeName,
                                                         uint volumeNameSize, out uint serial,
                                                         out uint maxComponent,
                                                         out uint fileSystemFlags,
                                                         StringBuilder fileSystemName,
                                                         uint fileSystemNameSize);

        [DllImport("rpcrt4.dll")]
        private static extern int UuidCreateSequential(out Guid uuid);

        // ---------------- 归一化 ----------------

        /// <summary>和 Python 的 " ".join(str(v).strip().lower().split()) 同效。</summary>
        public static string Norm(string value)
        {
            if (value == null) return "";
            var sb = new StringBuilder(value.Length);
            bool pendingSpace = false;
            bool seen = false;
            foreach (char c in value.ToLowerInvariant())
            {
                if (char.IsWhiteSpace(c)) { if (seen) pendingSpace = true; continue; }
                if (pendingSpace) { sb.Append(' '); pendingSpace = false; }
                sb.Append(c);
                seen = true;
            }
            return sb.ToString();
        }

        // ---------------- SMBIOS ----------------

        private static byte[] SmbiosTable()
        {
            const uint Rsmb = 0x52534D42;      // 'RSMB'
            try
            {
                uint size = GetSystemFirmwareTable(Rsmb, 0, null, 0);
                if (size < 64) return null;
                byte[] buf = new byte[size];
                uint got = GetSystemFirmwareTable(Rsmb, 0, buf, size);
                if (got < 64) return null;
                int length = BitConverter.ToInt32(buf, 4);     // LE
                if (length <= 0 || 8 + length > (int)got) length = (int)got - 8;
                byte[] table = new byte[length];
                Array.Copy(buf, 8, table, 0, length);
                return table;
            }
            catch (Exception) { return null; }
        }

        private sealed class SmbiosStructure
        {
            public byte Type;
            public byte[] Formatted;
            public List<string> Strings = new List<string>();
        }

        private static IEnumerable<SmbiosStructure> SmbiosStructures(byte[] table)
        {
            if (table == null) yield break;
            int off = 0, n = table.Length;
            while (off + 4 <= n)
            {
                int stype = table[off];
                int slen = table[off + 1];
                if (slen < 4 || off + slen > n) break;
                byte[] fmt = new byte[slen];
                Array.Copy(table, off, fmt, 0, slen);
                var st = new SmbiosStructure { Type = (byte)stype, Formatted = fmt };

                int p = off + slen;
                int next;
                if (p < n && table[p] == 0x00)
                {
                    next = p + 2;                                   // 双 NUL：没有字符串
                }
                else
                {
                    while (p < n)
                    {
                        int end = p;
                        while (end < n && table[end] != 0x00) end++;
                        byte[] raw = new byte[end - p];
                        Array.Copy(table, p, raw, 0, raw.Length);
                        st.Strings.Add(Encoding.GetEncoding(28591).GetString(raw));  // latin-1
                        p = end + 1;
                        if (p < n && table[p] == 0x00) { p++; break; }
                    }
                    next = p;
                }
                yield return st;
                if (next <= off) break;
                off = next;
            }
        }

        private static void SmbiosIds(Dictionary<string, string> outMap)
        {
            foreach (SmbiosStructure st in SmbiosStructures(SmbiosTable()))
            {
                if (st.Type == 1 && st.Formatted.Length >= 0x18)
                {
                    byte[] raw = new byte[16];
                    Array.Copy(st.Formatted, 0x08, raw, 0, 16);
                    bool written = false;
                    foreach (byte b in raw) { if (b != 0x00 && b != 0xFF) { written = true; break; } }
                    if (written)
                    {
                        var sb = new StringBuilder(32);
                        foreach (byte b in raw) sb.Append(b.ToString("x2"));
                        outMap["bios_uuid"] = sb.ToString();
                    }
                }
                else if (st.Type == 2 && st.Formatted.Length >= 0x08)
                {
                    int idx = st.Formatted[0x07];
                    if (idx > 0 && idx <= st.Strings.Count)
                    {
                        string v = st.Strings[idx - 1].Trim();
                        string low = v.ToLowerInvariant();
                        if (v.Length > 0 && low != "to be filled by o.e.m."
                            && low != "default string" && low != "none" && low != "empty")
                            outMap["board_serial"] = v;
                    }
                }
            }
        }

        // ---------------- 注册表 / 卷 / MAC ----------------

        private static string RegString(string path, string name)
        {
            try
            {
                using (RegistryKey k = Registry.LocalMachine.OpenSubKey(path, false))
                {
                    if (k == null) return null;
                    object v = k.GetValue(name, null);
                    return v == null ? null : Convert.ToString(v, CultureInfo.InvariantCulture);
                }
            }
            catch (Exception) { return null; }
        }

        private static string MachineGuid()
        {
            return RegString(@"SOFTWARE\Microsoft\Cryptography", "MachineGuid");
        }

        private static string CpuId()
        {
            const string Base = @"HARDWARE\DESCRIPTION\System\CentralProcessor\0";
            string name = RegString(Base, "ProcessorNameString");
            string ident = RegString(Base, "Identifier");
            if ((name != null && name.Length > 0) || (ident != null && ident.Length > 0))
                return (name ?? "") + "|" + (ident ?? "");
            return null;
        }

        private static string VolumeSerial()
        {
            try
            {
                string root = (Environment.GetEnvironmentVariable("SystemDrive") ?? "C:") + "\\";
                uint serial, maxComponent, flags;
                if (!GetVolumeInformationW(root, null, 0, out serial, out maxComponent,
                                           out flags, null, 0))
                    return null;
                return string.Format(CultureInfo.InvariantCulture, "{0:x8}", serial);
            }
            catch (Exception) { return null; }
        }

        private static string Mac()
        {
            try
            {
                Guid uuid;
                if (UuidCreateSequential(out uuid) != 0) return null;
                byte[] raw = uuid.ToByteArray();
                long node = 0;
                for (int i = 10; i < 16; i++) node = (node << 8) | raw[i];
                if (((node >> 40) & 1) != 0) return null;     // 多播位 = 随机/无网卡
                return node.ToString("x12", CultureInfo.InvariantCulture);
            }
            catch (Exception) { return null; }
        }

        public static Dictionary<string, string> CollectComponents()
        {
            var map = new Dictionary<string, string>(StringComparer.Ordinal);
            try { SmbiosIds(map); } catch (Exception) { }
            AddIfPresent(map, "machine_guid", MachineGuid());
            AddIfPresent(map, "cpu_id", CpuId());
            AddIfPresent(map, "volume_serial", VolumeSerial());
            AddIfPresent(map, "mac", Mac());
            return map;
        }

        private static void AddIfPresent(Dictionary<string, string> map, string key, string value)
        {
            if (!string.IsNullOrEmpty(value)) map[key] = value;
        }

        public static FingerprintResult Collect()
        {
            var comps = CollectComponents();
            var res = new FingerprintResult();
            var keys = new List<string>();
            foreach (string key in Weights.Keys)
            {
                if (!comps.ContainsKey(key) || string.IsNullOrEmpty(comps[key])) continue;
                string text = "sjz-fp:" + key + ":" + Norm(comps[key]);
                string digest = Crypto.Hex(Crypto.Sha256Utf8(text)).Substring(0, 32);
                res.Vec[key] = digest;
                res.Weights[key] = Weights[key];
                keys.Add(key);
            }
            keys.Sort(StringComparer.Ordinal);
            res.ComponentsPresent = keys;

            var joined = new StringBuilder();
            for (int i = 0; i < keys.Count; i++)
            {
                if (i > 0) joined.Append('|');
                joined.Append(keys[i]).Append('=').Append(res.Vec[keys[i]]);
            }
            res.Hash = Crypto.Hex(Crypto.Sha256Utf8(joined.ToString()));
            return res;
        }
    }
}
