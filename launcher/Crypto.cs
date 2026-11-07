// ECDSA P-256 验签、哈希、token。全部用 BCL，不引第三方库。
// 与 core/license.py 的约定：
//   · 曲线 P-256（secp256r1），公钥 = base64(X||Y) 各 32 字节大端
//   · 消息哈希 SHA-256(规范化 JSON UTF-8)
//   · 签名 = base64(r||s) 64 字节（IEEE P1363），不是 DER
namespace SjzLauncher
{
    using System;
    using System.Security.Cryptography;
    using System.Text;

    internal static class Crypto
    {
        // 和 core/license.py 的 PUBLIC_KEY_B64 必须逐字符一致（换钥时一起替换）。
        public const string PublicKeyB64 =
            "VtWMhwsKS9K8VVHWI7ITr9O3jzWpPkcojeCvU8JGeGnRfVFE/FsxMn8OBxUuNZjAEPokQD+Iyd/YiPax/yZjWw==";

        private const uint BcryptEcdsaPublicP256Magic = 0x31534345;   // 'ECS1'

        public static byte[] Sha256(byte[] data)
        {
            using (var h = new SHA256Managed())
            {
                return h.ComputeHash(data);
            }
        }

        public static byte[] Sha256Utf8(string text)
        {
            return Sha256(Encoding.UTF8.GetBytes(text));
        }

        public static string Hex(byte[] data)
        {
            var sb = new StringBuilder(data.Length * 2);
            for (int i = 0; i < data.Length; i++) sb.Append(data[i].ToString("x2"));
            return sb.ToString();
        }

        public static byte[] FromBase64Strict(string text)
        {
            // Convert.FromBase64String 会忽略空白，这里显式拒绝：
            // Python 侧用的是 validate=True，两边口径要一致。
            if (text == null) throw new FormatException("base64 为空");
            string t = text.Trim();
            if (t.Length != text.Length) throw new FormatException("base64 含首尾空白");
            return Convert.FromBase64String(t);
        }

        /// <summary>P-256 公钥（X||Y，64 字节）→ CngKey。</summary>
        public static CngKey ImportPublicKey(byte[] xy)
        {
            if (xy == null || xy.Length != 64) throw new ArgumentException("公钥必须是 64 字节 X||Y");
            // BCRYPT_ECCPUBLIC_BLOB：dwMagic / cbKey / X[32] / Y[32]，坐标大端
            byte[] blob = new byte[8 + 64];
            BitConverter.GetBytes(BcryptEcdsaPublicP256Magic).CopyTo(blob, 0);
            BitConverter.GetBytes((uint)32).CopyTo(blob, 4);
            Array.Copy(xy, 0, blob, 8, 64);
            return CngKey.Import(blob, CngKeyBlobFormat.EccPublicBlob);
        }

        public static byte[] EmbeddedPublicKey()
        {
#if SJZ_TEST_BUILD
            // 仅测试构建（SjzLauncherTest.exe）存在：pytest 每轮随机生成临时密钥，
            // 用这个变量把对应公钥注入进来，跑完即随进程消失。
            // 发布构建（SjzLauncher.exe）没有这段，公钥只能来自上面的编译期常量。
            string injected = Environment.GetEnvironmentVariable("SJZ_LAUNCHER_PUBKEY_B64");
            if (!string.IsNullOrEmpty(injected)) return FromBase64Strict(injected);
#endif
            return FromBase64Strict(PublicKeyB64);
        }

        /// <summary>
        /// 验签。sig64 = r||s。任何格式问题都返回 false，绝不抛到上层 ——
        /// 启动器的判定结果必须是「通过 / 中文原因」，不是异常栈。
        /// </summary>
        public static bool VerifyP256(byte[] message, byte[] sig64)
        {
            if (message == null || sig64 == null || sig64.Length != 64) return false;
            try
            {
                byte[] hash = Sha256(message);
                using (CngKey key = ImportPublicKey(EmbeddedPublicKey()))
                using (var ec = new ECDsaCng(key))
                {
                    return ec.VerifyHash(hash, sig64);
                }
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>控制面 token：每次启动随机 16 字节（32 位十六进制）。</summary>
        public static string NewToken()
        {
            byte[] raw = new byte[16];
            using (var rng = new RNGCryptoServiceProvider()) rng.GetBytes(raw);
            return Hex(raw);
        }

        /// <summary>本机 token 比较用（仅日志/状态里做掩码，不落盘）。</summary>
        public static string MaskToken(string token)
        {
            if (string.IsNullOrEmpty(token)) return "(none)";
            if (token.Length <= 4) return "****";
            return "****" + token.Substring(token.Length - 4);
        }
    }
}
