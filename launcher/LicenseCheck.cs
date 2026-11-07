// 卡密本地校验：验签 → 指纹 → 过期。判定只读本地文件，不联网。
// 服务端（core/license.py evaluate）还有离线账本/租期/吊销；启动器只做
// 「没有合法卡密就别把驱动拉起来」这一层，口径故意与 Python 保持一致：
//   六特征权重 30/25/15/15/10/5，阈值 0.85，许可至少绑定 4 个组件，
//   bios_uuid / machine_guid 至少命中一个。
namespace SjzLauncher
{
    using System;
    using System.Collections.Generic;
    using System.Globalization;
    using System.IO;
    using System.Text;

    internal enum LicenseState
    {
        None, SigInvalid, FpMismatch, Expired, OkGrace, Ok
    }

    internal static class LicenseStateNames
    {
        public static string Of(LicenseState s)
        {
            switch (s)
            {
                case LicenseState.Ok: return "ok";
                case LicenseState.OkGrace: return "ok_grace";
                case LicenseState.Expired: return "expired";
                case LicenseState.FpMismatch: return "fp_mismatch";
                case LicenseState.SigInvalid: return "sig_invalid";
                default: return "none";
            }
        }
    }

    internal sealed class LicenseResult
    {
        public LicenseState State = LicenseState.None;
        public bool Allowed;
        public string Reason = "";
        public string LicId = "";
        public string Tier = "";
        public bool Trial;
        public double? Exp;
        public double? RemainingS;
        public double FpScore;
        public List<string> FpHits = new List<string>();
        public string MachineCode = "";
        public string Detail = "";          // 诊断用，不进 UI 主文案

        public string StatusName() { return LicenseStateNames.Of(State); }

        public string ToJson()
        {
            var sb = new StringBuilder();
            sb.Append("{\"status\":\"").Append(StatusName()).Append("\",")
              .Append("\"ok\":").Append(Allowed ? "true" : "false").Append(',')
              .Append("\"reason\":").Append(Quote(Reason)).Append(',')
              .Append("\"lic_id\":").Append(Quote(LicId)).Append(',')
              .Append("\"tier\":").Append(Quote(Tier)).Append(',')
              .Append("\"trial\":").Append(Trial ? "true" : "false").Append(',')
              .Append("\"fp_score\":").Append(FpScore.ToString("0.####", CultureInfo.InvariantCulture)).Append(',')
              .Append("\"fp_hits\":[");
            for (int i = 0; i < FpHits.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append('"').Append(FpHits[i]).Append('"');
            }
            sb.Append("],\"machine_code\":").Append(Quote(MachineCode)).Append('}');
            return sb.ToString();
        }

        private static string Quote(string s)
        {
            var n = new JsonNode();
            n.Kind = JsonKind.String;
            n.Text = s ?? "";
            return Json.Canonical(n);
        }
    }

    internal static class LicenseCheck
    {
        public const double FpMinRatio = 0.85;
        public const int FpMinComponents = 4;

        public static readonly string[] FpAnchorKeys = { "bios_uuid", "machine_guid" };

        public static string DefaultPath()
        {
            return Path.Combine(AppPaths.DataDir, "license.json");
        }

        public static LicenseResult Load(string path, FingerprintResult fp)
        {
            var res = new LicenseResult();
            res.MachineCode = fp.MachineCode();

            string text;
            try
            {
                if (!File.Exists(path))
                {
                    res.State = LicenseState.None;
                    res.Reason = "没有找到卡密文件，请先导入卡密";
                    return res;
                }
                text = ReadTextStrict(path);
            }
            catch (Exception e)
            {
                res.State = LicenseState.SigInvalid;
                res.Reason = "卡密文件读不出来：" + e.Message;
                return res;
            }

            JsonNode blob;
            try { blob = Json.Parse(text); }
            catch (Exception e)
            {
                res.State = LicenseState.SigInvalid;
                res.Reason = "卡密文件不是合法 JSON：" + e.Message;
                return res;
            }

            // ---------- 1. 验签 ----------
            JsonNode payload = blob.Get("payload");
            if (payload == null || payload.Kind != JsonKind.Object)
            {
                res.State = LicenseState.SigInvalid;
                res.Reason = "卡密缺少 payload";
                return res;
            }
            JsonNode sigNode = blob.Get("sig");
            if (sigNode == null || sigNode.Kind != JsonKind.String)
            {
                res.State = LicenseState.SigInvalid;
                res.Reason = "卡密缺少签名";
                return res;
            }

            long v;
            if (!payload.TryLong("v", out v) || v != 1)
            {
                res.State = LicenseState.SigInvalid;
                res.Reason = "卡密版本不支持";
                return res;
            }

            byte[] sig;
            try { sig = Crypto.FromBase64Strict(sigNode.Text); }
            catch (Exception)
            {
                res.State = LicenseState.SigInvalid;
                res.Reason = "签名不是合法 base64";
                return res;
            }
            if (sig.Length != 64)
            {
                res.State = LicenseState.SigInvalid;
                res.Reason = string.Format("签名长度应为 64 字节（r||s），实际 {0}", sig.Length);
                return res;
            }

            string canonical = Json.Canonical(payload);
            if (!Crypto.VerifyP256(Encoding.UTF8.GetBytes(canonical), sig))
            {
                res.State = LicenseState.SigInvalid;
                res.Reason = "签名校验失败：卡密被改过，或不是发给本版本的";
                return res;
            }

            // ---------- 2. schema（验签后再校验，失败一律 fail closed） ----------
            string schemaError = ValidatePayload(payload);
            if (schemaError != null)
            {
                res.State = LicenseState.SigInvalid;
                res.Reason = "卡密字段非法：" + schemaError;
                return res;
            }

            res.LicId = payload.GetString("lic_id") ?? "";
            res.Tier = payload.GetString("tier") ?? "";
            JsonNode trial = payload.Get("trial");
            res.Trial = trial != null && trial.Kind == JsonKind.Bool && trial.BoolValue;
            JsonNode exp = payload.Get("exp");
            if (exp != null && exp.Kind == JsonKind.Number)
                res.Exp = exp.AsDouble(0.0);
            else if (exp != null)
                res.Exp = null;

            // ---------- 3. 指纹 ----------
            var stored = new Dictionary<string, string>(StringComparer.Ordinal);
            JsonNode vec = payload.Get("fp_vec");
            if (vec != null && vec.Kind == JsonKind.Object)
            {
                foreach (var kv in vec.Members)
                    if (kv.Value.Kind == JsonKind.String) stored[kv.Key] = kv.Value.Text;
            }
            double ratio;
            List<string> hits;
            MatchScore(stored, fp.Vec, payload.Get("fp_weights"), out ratio, out hits);
            res.FpScore = ratio;
            res.FpHits = hits;

            if (!FpPasses(stored, hits, ratio))
            {
                res.State = LicenseState.FpMismatch;
                string extra = stored.Count < FpMinComponents
                    ? string.Format("（卡密只绑定了 {0} 个组件，低于下限 {1}）", stored.Count, FpMinComponents)
                    : "";
                res.Reason = string.Format("这台机器与卡密绑定信息不符：匹配 {0:0}%{1}，机器码 {2}",
                                           ratio * 100.0, extra, res.MachineCode);
                return res;
            }

            // ---------- 4. 过期 ----------
            double now = (double)DateTime.UtcNow.ToFileTimeUtc() / 10000000.0 - 11644473600.0;
            double graceH = payload.GetDouble("grace_h", 72.0);
            if (graceH < 0 || graceH > 8760) graceH = 0;      // 非法值不放宽
            double graceS = graceH * 3600.0;
            if (res.Exp.HasValue)
            {
                double expT = res.Exp.Value;
                if (now > expT + graceS)
                {
                    res.State = LicenseState.Expired;
                    res.Reason = string.Format("卡密已过期 {0:0.0} 天（到期 {1}，宽限 {2}h），请续费",
                                               (now - expT - graceS) / 86400.0,
                                               FormatTime(expT), (int)graceH);
                    return res;
                }
                if (now > expT)
                {
                    res.State = LicenseState.OkGrace;
                    res.Allowed = true;
                    res.RemainingS = expT + graceS - now;
                    res.Reason = string.Format("已过到期日，仍在 {0} 小时宽限内（剩 {1:0.0} 小时），请尽快续费",
                                               (int)graceH, res.RemainingS.Value / 3600.0);
                    return res;
                }
                res.RemainingS = expT - now;
            }

            res.State = LicenseState.Ok;
            res.Allowed = true;
            res.Reason = res.Exp.HasValue
                ? string.Format("卡密有效，剩余 {0:0.0} 天", (res.RemainingS ?? 0) / 86400.0)
                : "卡密有效（长期）";
            return res;
        }

        private static string ReadTextStrict(string path)
        {
            byte[] raw = File.ReadAllBytes(path);
            if (raw.Length == 0) throw new FormatException("文件是空的");
            string text = new UTF8Encoding(false, true).GetString(raw);
            return text.TrimStart('\uFEFF');
        }

        private static string ValidatePayload(JsonNode payload)
        {
            if (payload.Members.ContainsKey("lic_ids") || payload.Members.ContainsKey("fp_ids"))
            {
                long gen;
                if (!payload.TryLong("gen", out gen) || gen < 0)
                    return "吊销列表 gen 非法";
                return null;                                  // 吊销列表不是卡密
            }
            string licId = payload.GetString("lic_id");
            if (string.IsNullOrEmpty(licId) || licId.Length > 128) return "lic_id 非法";

            JsonNode exp = payload.Get("exp");
            if (exp != null)
            {
                if (exp.Kind != JsonKind.Number) return "exp 非法";
                double e = exp.AsDouble(-1);
                if (e < 0 || e > 1e13) return "exp 非法";
            }
            foreach (var name in new[] { "grace_h", "lease_h", "offline_grace_h" })
            {
                JsonNode n = payload.Get(name);
                if (n == null) continue;
                if (n.Kind != JsonKind.Number) return name + " 非法";
                double d = n.AsDouble(-1);
                if (d < 0 || d > 8760) return name + " 非法";
            }
            JsonNode vec = payload.Get("fp_vec");
            if (vec != null)
            {
                if (vec.Kind != JsonKind.Object) return "fp_vec 非法";
                foreach (var kv in vec.Members)
                    if (kv.Value.Kind != JsonKind.String) return "fp_vec 非法";
            }
            JsonNode fpOne = payload.Get("fp");
            if (fpOne != null && fpOne.Kind != JsonKind.String) return "fp 非法";
            JsonNode w = payload.Get("fp_weights");
            if (w != null)
            {
                if (w.Kind != JsonKind.Object) return "fp_weights 非法";
                foreach (var kv in w.Members)
                {
                    if (kv.Value.Kind != JsonKind.Number) return "fp_weights 非法";
                    double d = kv.Value.AsDouble(-1);
                    if (d < 0 || d > 100) return "fp_weights 非法";
                }
            }
            JsonNode tr = payload.Get("trial");
            if (tr != null && tr.Kind != JsonKind.Bool) return "trial 非法";
            return null;
        }

        /// <summary>与 Python match_score 同口径：分母固定 100，未绑定组件按未命中计。</summary>
        public static void MatchScore(Dictionary<string, string> stored,
                                      Dictionary<string, string> current,
                                      JsonNode overrides,
                                      out double ratio, out List<string> hits)
        {
            var weights = new Dictionary<string, int>(Fingerprint.Weights, StringComparer.Ordinal);
            if (overrides != null && overrides.Kind == JsonKind.Object)
            {
                foreach (var kv in overrides.Members)
                {
                    if (kv.Value.Kind != JsonKind.Number) continue;
                    if (!weights.ContainsKey(kv.Key)) continue;
                    double d = kv.Value.AsDouble(0.0);
                    if (d < 0) continue;
                    if (d > weights[kv.Key]) d = weights[kv.Key];      // 只许收紧，不许放大
                    weights[kv.Key] = (int)d;
                }
            }
            double denom = 0;
            foreach (int wgt in weights.Values) denom += wgt;
            double matched = 0;
            hits = new List<string>();
            foreach (var kv in stored)
            {
                if (!weights.ContainsKey(kv.Key)) continue;
                if (current.ContainsKey(kv.Key) && current[kv.Key] == kv.Value)
                {
                    matched += weights[kv.Key];
                    hits.Add(kv.Key);
                }
            }
            hits.Sort(StringComparer.Ordinal);
            ratio = denom > 0 ? matched / denom : 0;
        }

        public static bool FpPasses(Dictionary<string, string> stored, List<string> hits, double ratio)
        {
            if (stored.Count < FpMinComponents) return false;
            bool anchor = false;
            foreach (string a in FpAnchorKeys)
                if (hits.Contains(a)) { anchor = true; break; }
            if (!anchor) return false;
            return ratio >= FpMinRatio;
        }

        private static string FormatTime(double epoch)
        {
            try
            {
                DateTime utc = DateTime.FromFileTimeUtc(
                    (long)((epoch + 11644473600.0) * 10000000.0));
                return utc.ToLocalTime().ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
            }
            catch (Exception) { return "?"; }
        }
    }
}
