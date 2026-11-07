// 极简 JSON：解析 + 规范化序列化。
// 不引第三方库（纯 .NET Framework 4.8 BCL），规范化输出必须和 Python
// json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
// 逐字节一致 —— 这是跨语言验签的前提。契约：
//   · 键按序数（ordinal）排序；许可 payload 的键都是 ASCII，与 Python 按码点排序等价
//   · 数字：整数按十进制原样；带小数点/指数的按 "R" 输出并保证至少一个 '.'
//     （许可 payload 只允许整数，浮点仅是兼容）
//   · 字符串：只转义 " \ 和 <0x20 的控制字符，非 ASCII 原样输出（UTF-8）
namespace SjzLauncher
{
    using System;
    using System.Collections.Generic;
    using System.Globalization;
    using System.Text;

    internal enum JsonKind { Null, Bool, Number, String, Array, Object }

    internal sealed class JsonNode
    {
        public JsonKind Kind;
        public bool BoolValue;
        public string Text;                              // Number 原始 token / String 值
        public List<JsonNode> Items;                     // Array
        public Dictionary<string, JsonNode> Members;     // Object

        public static JsonNode NewObject()
        {
            var n = new JsonNode();
            n.Kind = JsonKind.Object;
            n.Members = new Dictionary<string, JsonNode>(StringComparer.Ordinal);
            return n;
        }

        public JsonNode Get(string key)
        {
            if (Kind != JsonKind.Object || Members == null) return null;
            JsonNode v = Members.ContainsKey(key) ? Members[key] : null;
            return v != null && v.Kind != JsonKind.Null ? v : null;
        }

        public string GetString(string key)
        {
            JsonNode v = Get(key);
            return v != null && v.Kind == JsonKind.String ? v.Text : null;
        }

        /// <summary>整数读取：只接受纯整数 token（不做 12.7 → 12 的舍入）。</summary>
        public bool TryLong(string key, out long value)
        {
            value = 0;
            JsonNode v = Get(key);
            if (v == null || v.Kind != JsonKind.Number) return false;
            string t = v.Text;
            if (t.IndexOf('.') >= 0 || t.IndexOf('e') >= 0 || t.IndexOf('E') >= 0)
                return false;
            try
            {
                value = long.Parse(t, CultureInfo.InvariantCulture);
                return true;
            }
            catch (Exception) { return false; }
        }

        public double GetDouble(string key, double fallback)
        {
            JsonNode v = Get(key);
            if (v == null || v.Kind != JsonKind.Number) return fallback;
            return v.AsDouble(fallback);
        }

        /// <summary>直接读当前节点（Number）的值。</summary>
        public double AsDouble(double fallback)
        {
            if (Kind != JsonKind.Number) return fallback;
            try { return double.Parse(Text, CultureInfo.InvariantCulture); }
            catch (Exception) { return fallback; }
        }
    }

    internal static class Json
    {
        public static JsonNode Parse(string text)
        {
            if (text == null) throw new FormatException("JSON 为空");
            int i = 0;
            JsonNode n = ParseValue(text, ref i, 0);
            SkipWs(text, ref i);
            if (i != text.Length) throw new FormatException("JSON 尾部有多余内容");
            return n;
        }

        private const int MaxDepth = 32;

        private static void SkipWs(string s, ref int i)
        {
            while (i < s.Length)
            {
                char c = s[i];
                if (c == ' ' || c == '\t' || c == '\r' || c == '\n') i++;
                else break;
            }
        }

        private static JsonNode ParseValue(string s, ref int i, int depth)
        {
            if (depth > MaxDepth) throw new FormatException("JSON 嵌套过深");
            SkipWs(s, ref i);
            if (i >= s.Length) throw new FormatException("JSON 提前结束");
            char c = s[i];
            if (c == '{') return ParseObject(s, ref i, depth);
            if (c == '[') return ParseArray(s, ref i, depth);
            if (c == '"')
            {
                var n = new JsonNode();
                n.Kind = JsonKind.String;
                n.Text = ParseString(s, ref i);
                return n;
            }
            if (c == 't' || c == 'f')
            {
                var n = new JsonNode();
                n.Kind = JsonKind.Bool;
                if (c == 't' && s.Substring(i).StartsWith("true"))
                { n.BoolValue = true; i += 4; return n; }
                if (c == 'f' && s.Substring(i).StartsWith("false"))
                { n.BoolValue = false; i += 5; return n; }
                throw new FormatException("JSON 布尔值非法");
            }
            if (c == 'n')
            {
                if (!s.Substring(i).StartsWith("null")) throw new FormatException("JSON null 非法");
                i += 4;
                var n = new JsonNode();
                n.Kind = JsonKind.Null;
                return n;
            }
            if (c == '-' || (c >= '0' && c <= '9')) return ParseNumber(s, ref i);
            throw new FormatException("JSON 非法字符 " + c);
        }

        private static JsonNode ParseObject(string s, ref int i, int depth)
        {
            JsonNode n = JsonNode.NewObject();
            i++;                                  // {
            SkipWs(s, ref i);
            if (i < s.Length && s[i] == '}') { i++; return n; }
            while (true)
            {
                SkipWs(s, ref i);
                if (i >= s.Length || s[i] != '"') throw new FormatException("JSON 对象键必须是字符串");
                string key = ParseString(s, ref i);
                SkipWs(s, ref i);
                if (i >= s.Length || s[i] != ':') throw new FormatException("JSON 对象缺少冒号");
                i++;
                JsonNode v = ParseValue(s, ref i, depth + 1);
                if (n.Members.ContainsKey(key)) throw new FormatException("JSON 对象键重复：" + key);
                n.Members[key] = v;
                SkipWs(s, ref i);
                if (i >= s.Length) throw new FormatException("JSON 对象提前结束");
                if (s[i] == ',') { i++; continue; }
                if (s[i] == '}') { i++; return n; }
                throw new FormatException("JSON 对象分隔符非法");
            }
        }

        private static JsonNode ParseArray(string s, ref int i, int depth)
        {
            var n = new JsonNode();
            n.Kind = JsonKind.Array;
            n.Items = new List<JsonNode>();
            i++;                                  // [
            SkipWs(s, ref i);
            if (i < s.Length && s[i] == ']') { i++; return n; }
            while (true)
            {
                n.Items.Add(ParseValue(s, ref i, depth + 1));
                SkipWs(s, ref i);
                if (i >= s.Length) throw new FormatException("JSON 数组提前结束");
                if (s[i] == ',') { i++; continue; }
                if (s[i] == ']') { i++; return n; }
                throw new FormatException("JSON 数组分隔符非法");
            }
        }

        private static string ParseString(string s, ref int i)
        {
            i++;                                  // "
            var sb = new StringBuilder();
            while (true)
            {
                if (i >= s.Length) throw new FormatException("JSON 字符串提前结束");
                char c = s[i++];
                if (c == '"') return sb.ToString();
                if (c != '\\') { sb.Append(c); continue; }
                if (i >= s.Length) throw new FormatException("JSON 转义提前结束");
                char e = s[i++];
                switch (e)
                {
                    case '"': sb.Append('"'); break;
                    case '\\': sb.Append('\\'); break;
                    case '/': sb.Append('/'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    case 'u':
                        if (i + 4 > s.Length) throw new FormatException("JSON \\u 转义不完整");
                        string hex = s.Substring(i, 4);
                        sb.Append((char)int.Parse(hex, NumberStyles.HexNumber, CultureInfo.InvariantCulture));
                        i += 4;
                        break;
                    default: throw new FormatException("JSON 未知转义 \\" + e);
                }
            }
        }

        private static JsonNode ParseNumber(string s, ref int i)
        {
            int start = i;
            if (s[i] == '-') i++;
            while (i < s.Length && char.IsDigit(s[i])) i++;
            if (i < s.Length && s[i] == '.')
            {
                i++;
                while (i < s.Length && char.IsDigit(s[i])) i++;
            }
            if (i < s.Length && (s[i] == 'e' || s[i] == 'E'))
            {
                i++;
                if (i < s.Length && (s[i] == '+' || s[i] == '-')) i++;
                while (i < s.Length && char.IsDigit(s[i])) i++;
            }
            string token = s.Substring(start, i - start);
            if (token == "-" || token.Length == 0) throw new FormatException("JSON 数字非法");
            var n = new JsonNode();
            n.Kind = JsonKind.Number;
            n.Text = token;
            return n;
        }

        /// <summary>与 Python json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False) 对齐。</summary>
        public static string Canonical(JsonNode n)
        {
            var sb = new StringBuilder();
            WriteCanonical(n, sb);
            return sb.ToString();
        }

        private static void WriteCanonical(JsonNode n, StringBuilder sb)
        {
            switch (n.Kind)
            {
                case JsonKind.Null: sb.Append("null"); return;
                case JsonKind.Bool: sb.Append(n.BoolValue ? "true" : "false"); return;
                case JsonKind.Number: sb.Append(FormatNumber(n.Text)); return;
                case JsonKind.String: WriteString(n.Text, sb); return;
                case JsonKind.Array:
                    sb.Append('[');
                    for (int k = 0; k < n.Items.Count; k++)
                    {
                        if (k > 0) sb.Append(',');
                        WriteCanonical(n.Items[k], sb);
                    }
                    sb.Append(']');
                    return;
                case JsonKind.Object:
                    sb.Append('{');
                    var keys = new List<string>(n.Members.Keys);
                    keys.Sort(StringComparer.Ordinal);
                    for (int k = 0; k < keys.Count; k++)
                    {
                        if (k > 0) sb.Append(',');
                        WriteString(keys[k], sb);
                        sb.Append(':');
                        WriteCanonical(n.Members[keys[k]], sb);
                    }
                    sb.Append('}');
                    return;
            }
        }

        private static string FormatNumber(string token)
        {
            if (token.IndexOf('.') < 0 && token.IndexOf('e') < 0 && token.IndexOf('E') < 0)
                return token;                       // 整数原样（Python int 也原样）
            double d;
            try { d = double.Parse(token, CultureInfo.InvariantCulture); }
            catch (Exception) { return token; }
            string r = d.ToString("R", CultureInfo.InvariantCulture);
            r = r.Replace("E", "e");               // Python 用小写 e
            if (r.IndexOf('.') < 0 && r.IndexOf('e') < 0) r += ".0";
            return r;
        }

        private static void WriteString(string s, StringBuilder sb)
        {
            sb.Append('"');
            for (int i = 0; i < s.Length; i++)
            {
                char c = s[i];
                switch (c)
                {
                    case '"': sb.Append("\\\""); continue;
                    case '\\': sb.Append("\\\\"); continue;
                    case '\b': sb.Append("\\b"); continue;
                    case '\f': sb.Append("\\f"); continue;
                    case '\n': sb.Append("\\n"); continue;
                    case '\r': sb.Append("\\r"); continue;
                    case '\t': sb.Append("\\t"); continue;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else sb.Append(c);
                        continue;
                }
            }
            sb.Append('"');
        }
    }
}
