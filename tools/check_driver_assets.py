"""构建期闸门：Interception 驱动资源必须完整可装。

背景（2026-11 现场教训）：
  Interception 的驱动**不是** interception.sys + 服务 interception，而是
  keyboard.sys / mouse.sys 两个上滤驱动 + 服务 keyboard / mouse；官方 v1.0.1
  发行包里没有独立的 .sys/.inf/.cat，驱动以 DRIVER/KBDNT、DRIVER/MOUNT 资源
  **内嵌在 install-interception.exe 资源段**。所以「分包里只有 dll」不是缺件，
  真正的缺件风险是：这个 exe 被裁掉、被替换成别的版本、或资源段被剥离 —— 那
  用户装完就永远装不上驱动。本脚本在打包前把这件事查死。

用法：
  python tools/check_driver_assets.py [--root <repo>] [--quiet]
退出码：0 = 齐全；1 = 缺件/不完整（打印缺哪个、期望什么）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys

EXE_REL = os.path.join("tools", "interception", "install-interception.exe")
# 官方 v1.0.1（github.com/oblitum/Interception releases）唯一发行版安装器
KNOWN_SHA256 = {
    "E137863A79DA797F08E7A137280FF2A123809044A888FD75CE9C973198915ABE":
        "Interception v1.0.1 官方发行包",
}
NEED_RES_TYPES = ("KBDNT", "MOUNT")   # 键盘上滤 / 鼠标上滤
MIN_BLOBS_PER_TYPE = 5                # 5 个系统版本 × (x86/x64/IA64)


def _pe_resources(data: bytes) -> list[tuple[str, int, bytes]]:
    """返回 [(类型名, 资源名, blob)]，仅解析 PE 资源树。"""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("不是 PE 文件")
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    optoff = pe + 24
    magic = struct.unpack_from("<H", data, optoff)[0]
    dd = optoff + (112 if magic == 0x20B else 96)
    res_rva = struct.unpack_from("<I", data, dd + 16)[0]
    if not res_rva:
        raise ValueError("没有资源段")

    secs = []
    so = optoff + struct.unpack_from("<H", data, pe + 20)[0]
    for i in range(nsec):
        vsz, va, rsz, ro = struct.unpack_from("<IIII", data, so + i * 40 + 8)
        secs.append((va, vsz, ro, rsz))

    def rva2off(rva: int) -> int:
        for va, vsz, ro, rsz in secs:
            if va <= rva < va + max(vsz, rsz):
                return ro + (rva - va)
        raise ValueError("资源 RVA 越界")

    base = rva2off(res_rva)
    out: list[tuple[str, int, bytes]] = []

    def label(raw: int, at: int) -> str:
        if raw & 0x80000000:
            off = at + (raw & 0x7FFFFFFF)
            ln = struct.unpack_from("<H", data, off)[0]
            raw_name = data[off + 2:off + 2 + ln]
            # 资源名可能是 UTF-16LE（Inno/老 RC 编译常见），也可能纯 ANSI
            if b"\x00" in raw_name:
                txt = raw_name.decode("utf-16-le", "ignore")
            else:
                txt = raw_name.decode("latin1")
            return txt.strip("\x00")
        return "#%d" % raw

    def walk(off: int, type_name: str, res_name: str) -> None:
        nname, nid = struct.unpack_from("<HH", data, off + 12)
        for i in range(nname + nid):
            e = off + 16 + i * 8
            nm, sub = struct.unpack_from("<II", data, e)
            lab = label(nm, base)
            if sub & 0x80000000:
                if not type_name:
                    walk(base + (sub & 0x7FFFFFFF), lab, res_name)
                else:
                    walk(base + (sub & 0x7FFFFFFF), type_name, lab)
            else:
                doff, dsz = struct.unpack_from("<II", data, base + sub)
                real = rva2off(doff)
                out.append((type_name, res_name, data[real:real + dsz]))

    walk(base, "", "")
    return out


def check(root: str) -> tuple[bool, list[str]]:
    msgs: list[str] = []
    path = os.path.join(root, EXE_REL)
    if not os.path.isfile(path):
        return False, [f"缺文件：{EXE_REL}（驱动安装器不在仓库里，安装包会缺少驱动能力）"]

    size = os.path.getsize(path)
    if size <= 0:
        return False, [f"文件为空：{EXE_REL}"]

    sha = hashlib.sha256(open(path, "rb").read()).hexdigest().upper()
    if sha not in KNOWN_SHA256:
        msgs.append(f"{EXE_REL} 的 SHA256 不在白名单：{sha}；期望官方 v1.0.1 的 "
                    f"{next(iter(KNOWN_SHA256))}（换了版本请同步更新本脚本与 docs/driver-install.md）")
    else:
        msgs.append(f"{EXE_REL}: {size:,} B, {KNOWN_SHA256[sha]} 哈希命中")

    try:
        res = _pe_resources(open(path, "rb").read())
    except Exception as e:                    # noqa: BLE001
        return False, msgs + [f"{EXE_REL} 资源段解析失败：{type(e).__name__}: {e}"]

    types: dict[str, int] = {}
    bad: list[str] = []
    for tname, rname, blob in res:
        key = rname or tname
        if key not in NEED_RES_TYPES:
            continue
        if blob[:2] != b"MZ":
            bad.append(f"DRIVER/{key} 有一个资源不是 PE 驱动镜像")
            continue
        p2 = struct.unpack_from("<I", blob, 0x3C)[0]
        machine = struct.unpack_from("<H", blob, p2 + 4)[0]
        if machine not in (0x014C, 0x8664, 0x0200):        # x86 / x64 / IA64
            bad.append(f"DRIVER/{key} 有一个驱动的 machine=0x{machine:04x}，不是 x86/x64/IA64")
            continue
        if len(blob) == 0:
            bad.append(f"DRIVER/{key} 有一个空驱动资源")
            continue
        types[key] = types.get(key, 0) + 1

    for tname in NEED_RES_TYPES:
        n = types.get(tname, 0)
        if n < MIN_BLOBS_PER_TYPE:
            msgs.append(f"驱动资源不足：DRIVER/{tname} 只有 {n} 个 PE blob，"
                        f"至少需要 {MIN_BLOBS_PER_TYPE} 个（x86/x64/IA64 × 多个系统版本）")
    if bad:
        msgs.extend(bad)

    ok = all(("不足" not in m and "不在白名单" not in m and "缺文件" not in m
              and "解析失败" not in m and "为空" not in m) for m in msgs)
    return ok, msgs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    ok, msgs = check(a.root)
    if not a.quiet:
        for m in msgs:
            print(("[ok] " if ok else "     ") + m)
    if ok:
        print("DRIVER_ASSETS=ok")
        return 0
    print("DRIVER_ASSETS=FAIL —— 安装包会被构建成「有 dll、没驱动」的残缺包")
    print("补齐办法见 docs/driver-install.md（工具是内嵌驱动的官方单文件安装器）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
