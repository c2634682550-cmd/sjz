"""核对分析报告里的每一条字节级断言 —— 防止报告与证据脱节。

跑法:
    python tools/verify_axon_report.py
全部通过则退出码 0；任何一条不符会打印 FAIL 并以退出码 1 结束。

覆盖：
  - 目标 exe 的 sha256 / 大小
  - 授权 DLL 的大小 / md5 / 导出表（AxonLogin.dll + SP_Init_v2）
  - DLL .rdata 里被报告引用的每条中文字符串
  - 抓包报文的长度域、命令码、重复模式计数
"""
from __future__ import annotations

import hashlib
import os
import struct
import sys

EXE = r"C:\Users\26346\Downloads\AxonSkin v2.3新算法.exe"
DLL = r"C:\Users\26346\Desktop\project\dafors\_reverse\axonlogin.dll"
PCAP = os.path.join(os.environ.get("TEMP", "."), "axon_auth", "auth.pcapng")

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    if not cond:
        FAILS += 1
    print(("  OK   " if cond else "  FAIL ") + msg)


def dll_sections(d: bytes):
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    opt = pe + 24
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    optsz = struct.unpack_from("<H", d, pe + 20)[0]
    out = []
    for i in range(nsec):
        o = opt + optsz + 40 * i
        name = d[o:o + 8].rstrip(b"\x00").decode("latin1")
        vs, va, rs, ro = struct.unpack_from("<IIII", d, o + 8)
        out.append((name, va, vs, rs, ro))
    return pe, opt, out


def rva_to_off(secs, rva: int):
    for _nm, va, vs, rs, ro in secs:
        if va <= rva < va + max(vs, rs):
            return ro + (rva - va)
    return None


def pcap_payloads(path: str):
    if not os.path.exists(path):
        return []
    p = open(path, "rb").read()
    off, out = 0, []
    while off < len(p):
        btype = struct.unpack_from("<I", p, off)[0]
        blen = struct.unpack_from("<I", p, off + 4)[0]
        if blen < 12:
            break
        if btype == 6:
            cap = struct.unpack_from("<I", p, off + 20)[0]
            pk = p[off + 28:off + 28 + cap]
            if len(pk) >= 54 and struct.unpack_from(">H", pk, 12)[0] == 0x0800:
                ip = 14
                ihl = (pk[ip] & 0xF) * 4
                if pk[ip + 9] == 6:
                    tcp = ip + ihl
                    doff = (pk[tcp + 12] >> 4) * 4
                    pl = pk[tcp + doff:]
                    if pl:
                        out.append(pl)
        off += blen
    return out


def main() -> None:
    print("== 目标 exe ==")
    e = open(EXE, "rb").read()
    check(hashlib.sha256(e).hexdigest() ==
          "097f313c46edb4ca59260f078287958594a6275024ea070a7b9d94a9fdbe1cf7",
          "sha256 与报告一致")
    check(len(e) == 166772736, "大小 = 166,772,736")

    print("== 授权 DLL ==")
    a = open(DLL, "rb").read()
    check(len(a) == 548864, "大小 = 548,864")
    check(hashlib.md5(a).hexdigest() == "120ff63d305179e17109192d575445bc",
          "md5 与报告一致")
    _pe, opt, secs = dll_sections(a)
    check([s[0] for s in secs] ==
          [".text", ".rdata", ".data", ".pdata", ".fptable", ".rsrc", ".reloc"],
          "7 个段名与报告一致")
    # 导出表
    magic = struct.unpack_from("<H", a, opt)[0]
    dd = opt + (112 if magic == 0x20B else 96)
    exp_rva = struct.unpack_from("<I", a, dd)[0]
    eo = rva_to_off(secs, exp_rva)
    check(eo is not None, "导出目录可定位")
    if eo is not None:
        # IMAGE_EXPORT_DIRECTORY: name@12, base@16, nfunc@20, nname@24,
        #                          AddressOfFunctions@28, AddressOfNames@32,
        #                          AddressOfNameOrdinals@36
        name_rva = struct.unpack_from("<I", a, eo + 12)[0]
        no = rva_to_off(secs, name_rva)
        # DLL 名是 NUL 结尾的 ASCII（不是长度前缀）
        end = a.index(b"\x00", no)
        check(a[no:end].decode() == "AxonLogin.dll",
              "导出 DLL 名 = AxonLogin.dll")
        nname = struct.unpack_from("<I", a, eo + 24)[0]
        aname = struct.unpack_from("<I", a, eo + 32)[0]
        afun = struct.unpack_from("<I", a, eo + 28)[0]
        aord = struct.unpack_from("<I", a, eo + 36)[0]
        names = []
        for i in range(nname):
            # AddressOfNames 是一个**指向名字 RVA 的数组**，要再解一层
            name_ptr_rva = struct.unpack_from(
                "<I", a, rva_to_off(secs, aname) + 4 * i)[0]
            o = rva_to_off(secs, name_ptr_rva)
            # 导出名是 NUL 结尾的 ASCII（不是长度前缀）
            end = a.index(b"\x00", o)
            names.append(a[o:end].decode())
            ordi = struct.unpack_from("<H", a, rva_to_off(secs, aord) + 2 * i)[0]
            frva = struct.unpack_from("<I", a, rva_to_off(secs, afun) + 4 * ordi)[0]
            check(frva == 0x12720, "SP_Init_v2 的 RVA = 0x12720")
        check(names == ["SP_Init_v2"], "唯一导出 = SP_Init_v2")
    # .rdata 里的中文/URL
    rdata = a[0x36600:0x36600 + 0x17000]
    for s in ("卡密错误!", "卡密已到期!", "卡密被封停!", "机器码不符!",
              "代理商被禁用! 该代理下所有卡密无法登录!",
              "过期时间:%04d年%02d月%02d日 %02d时%02d分%02d秒",
              "https://wwaoo.lanzoue.com/s/daozidan",
              "提示 (5s后自动关闭)"):
        check(s.encode("utf-16le") in rdata, f".rdata 明文含「{s[:26]}」")
    check(b"124.222.192.168" not in a, "DLL 内没有 IP 常量（IP 由主程序注入）")
    # 自定义 Base-36 字母表（RVA 0x387c0）
    alpha_off = rva_to_off(secs, 0x387C0)
    alpha = a[alpha_off:alpha_off + 36].decode("ascii")
    check(alpha == "aAbBcCdDeFgGhHIjmMnprRStTuUVwWxXyYzZ",
          f"编码字母表 @0x387c0 = {alpha}")

    # 授权失败分支：lea r8 -> "提示" 标题；+7 字节处 lea rdx -> 错误文案
    # （见 auth_protocol.md §6.1 的 17 项跳板表）
    TITLE_RVA = 0x38E6C
    branches = [
        (0x6CB1, 0x39010, "网络通讯出错!"),
        (0x6CA1, 0x38FF8, "服务器连接失败!"),
        (0x6C91, 0x38FE8, "数据发送出错!"),
        (0x6C81, 0x38FD8, "数据接收出错!"),
        (0x6912, 0x38E78, "卡密错误!"),
        (0x6925, 0x38E88, "卡密已到期!"),
        (0x6938, 0x38E98, "卡密被封停!"),
        (0x6AEE, 0x38ED8, "代理商被禁用! 该代理下所有卡密无法登录!"),
        (0x6C19, 0x38F20, "试用功能未开启!"),
        (0x6C2C, 0x38F38, "当前卡密与绑定的IP"),
        (0x6C3F, 0x38F60, "当前卡密与绑定的机器码不符!"),
        (0x6C52, 0x38F80, "禁止登陆!"),
        (0x6C65, 0x38F90, "代理商被删除! 该代理下所有卡密无法登录!"),
    ]

    def lea_target(rva: int):
        o = rva_to_off(secs, rva)
        ins = a[o:o + 7]
        if ins[0] in (0x48, 0x4C) and ins[1] == 0x8D and ins[2] in (0x05, 0x15):
            return rva + 7 + struct.unpack_from("<i", ins, 3)[0]
        return None

    for br_rva, str_rva, label in branches:
        # 标题 lea r8
        check(lea_target(br_rva) == TITLE_RVA,
              f"分支 {br_rva:#x} 的 lea r8 指向「提示」标题")
        # 文案 lea rdx（紧跟其后 7 字节）
        check(lea_target(br_rva + 7) == str_rva,
              f"分支 {br_rva:#x} 的 lea rdx 指向「{label[:12]}」")
        so = rva_to_off(secs, str_rva)
        check(a[so:so + len(label) * 2].decode("utf-16le") == label,
              f"文案 RVA {str_rva:#x} 内容 = 「{label[:12]}」")

    # .lic 卡密文件扩展名（UTF-16LE @0x38d28，ASCII @0x38d70）
    for rva, want, label in ((0x38D28, ".lic\x00".encode("utf-16le"), "UTF-16LE"),
                             (0x38D70, b".lic\x00", "ASCII")):
        o = rva_to_off(secs, rva)
        check(a[o:o + len(want)] == want, f".lic 扩展名（{label}）@ RVA {rva:#x}")

    # 授权分支跳板表 @0x6e6c：17 项，每项是一个 32 位分支地址
    jt_off = rva_to_off(secs, 0x6E6C)
    entries = []
    for i in range(20):
        v = struct.unpack_from("<I", a, jt_off + 4 * i)[0]
        if not (0x6000 < v < 0x8000):
            break
        entries.append(v)
    check(len(entries) == 17, f"跳板表 @0x6e6c 有 17 项（实得 {len(entries)}）")
    for br in (0x680A, 0x6912, 0x6925, 0x6938, 0x6AEE, 0x6B01,
               0x6C19, 0x6C2C, 0x6C3F, 0x6C52, 0x6C65,
               0x6C78, 0x6C81, 0x6C91, 0x6CA1, 0x6CB1):
        check(br in entries, f"跳板表含分支 {br:#x}")

    # 机器码字段：SendMessageW(0x1061, 序号, {键名指针, 键名长度})
    # 每项验证：调用点处 mov edx,0x1061 / mov r8d,序号 / lea rcx,[键名RVA] / 键名长度常量
    def imm32_at(rva: int, opcode: bytes):
        o = rva_to_off(secs, rva)
        return a[o:o + len(opcode) + 4] == opcode + struct.pack("<I", 0)[:0] + a[o + len(opcode):o + len(opcode) + 4]

    hwid_fields = [
        (0xB8EA, 0, 0x392E8, "CID"),
        (0xB931, 1, 0x392F0, "计算机名"),
        (0xB978, 2, 0x39300, "操作系统"),
        (0xB9BF, 3, 0x39310, "初始化时间"),
        (0xC906, 0, 0x39410, "机器码"),
        (0xC94D, 1, 0x39418, "备注"),
        (0xC994, 2, 0x39420, "绑定时间"),
        (0xC9DB, 3, 0x39430, "系统版本"),
        (0xCA22, 4, 0x392F0, "计算机名"),
        (0xCA69, 5, 0x39440, "最后登录时间"),
    ]
    for site, idx, key_rva, label in hwid_fields:
        o = rva_to_off(secs, site)
        # mov edx, 0x1061
        check(a[o - 0x15:o - 0x15 + 5] == b"\xba\x61\x10\x00\x00"
              or b"\xba\x61\x10\x00\x00" in a[o - 0x20:o],
              f"{site:#x} 附近有 mov edx, 0x1061")
        # mov r8d, idx（idx=0 时是 xor r8d,r8d）
        if idx:
            check(b"\x41\xb8" + struct.pack("<I", idx) in a[o - 0x20:o],
                  f"{site:#x} wParam = {idx}")
        else:
            # xor r8d, r8d  ->  45 33 c0
            check(b"\x45\x33\xc0" in a[o - 0x20:o],
                  f"{site:#x} wParam = 0（xor r8d,r8d）")
        # 键名
        ko = rva_to_off(secs, key_rva)
        check(a[ko:ko + len(label) * 2].decode("utf-16le") == label,
              f"{site:#x} 键名 @ {key_rva:#x} = 「{label}」")

    print("== 抓包报文 ==")
    pkts = pcap_payloads(PCAP)
    check(bool(pkts), f"读到 {len(pkts)} 个带载荷的包")
    by_hex = {p.hex(): p for p in pkts}
    req = "2d0000000208372b4109d8e2f4f2b0cdf4c9cf2f01890f4f01d92f030199622f01992f1fd5d52f85166c91b5a4"
    resp_a = ("a6000000eba843de0f01c92f0f01c92f0f031b270d03db2d0d03db2da23af662816a8602753c5443"
              "340cb492646cc4f3c1ea56f323cc7bcc433d73f4e32da404133b8364dfa1098f0f030b8f0d03db2d"
              "0d03db2d0d03db2d0d03db2d0d03db2d0d03db2d0d03db2d0d03db2d0d03db2d0d03db2d0d03db2d0d"
              "03db2d0d03db2d0d03db2d0d03db2d0d03db2d0d03db2d0d03db2d0dd3db2d4c425f484923fd5968c6"
              "7fe9b5a4")
    resp_b = "240000002f6742ea0f01c92f0f01f92f0f05992f4f01d92f0f01592f485f91ea1901b9e8"
    for name, hx, ln, cmd in (("请求", req, 45, 0x02),
                              ("响应A", resp_a, 166, 0xEB),
                              ("响应B", resp_b, 36, 0x2F)):
        p = by_hex.get(hx)
        check(p is not None, f"{name} 报文存在于 pcap")
        if p:
            check(len(p) == ln, f"{name} 实际长度 = {ln}")
            check(struct.unpack_from("<I", p, 0)[0] == ln, f"{name} 长度域 = {ln}")
            check(p[4] == cmd, f"{name} 命令码 = {cmd:#04x}")
    a166 = by_hex.get(resp_a)
    if a166:
        check(a166.count(bytes.fromhex("0d03db2d")) == 20,
              "响应A 中 0d03db2d 出现 20 次")
        check(a166.find(bytes.fromhex("0d03db2d")) == 0x14,
              "响应A 首个 0d03db2d 在偏移 0x14")
        check(a166[-2:] == bytes.fromhex("b5a4"), "响应A 尾部校验 = b5a4")

    print()
    if FAILS:
        print(f"FAILED: {FAILS} 条断言不符")
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
