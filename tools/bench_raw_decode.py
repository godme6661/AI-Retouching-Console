"""测量并对比两条 RAW 解码路径的**真实交付代价**（供"推广到其它机器"的选型决策）。

测量项：
  1. rawpy 的 wheel 覆盖情况（决定目标机器能否 pip 装上，含离线打包体积）
  2. WIC 路径：整条调用链（起 PowerShell → 解码 → 落盘 → Python 读入）耗时与临时文件体积
  3. 内嵌 JPEG 路径：耗时与体积（零依赖基线）
"""
from __future__ import annotations
import os

import glob
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from cogitator import raw_io  # noqa: E402

HERE = Path(__file__).resolve().parent
WIC_PS1 = HERE / "wic_dump.ps1"
CR3 = os.environ.get("COGITATOR_RAW_SAMPLE", "")


def pypi_wheel_coverage() -> None:
    print("== 1) rawpy 的 wheel 覆盖（决定目标机器能否 pip 装上）==")
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/rawpy/json", timeout=25) as r:
            data = json.load(r)
        ver = data["info"]["version"]
        files = data["releases"][ver]
        by_plat: dict[str, list[str]] = {}
        total = 0
        for f in files:
            name = f["filename"]
            if not name.endswith(".whl"):
                continue
            total += f["size"]
            if "win_amd64" in name:
                key = "Windows x64"
            elif "manylinux" in name:
                key = "Linux x64"
            elif "macosx" in name:
                key = "macOS"
            else:
                key = "其它"
            tag = name.split("-")[2] if name.count("-") >= 3 else "?"
            by_plat.setdefault(key, []).append(tag)
        print(f"  最新版本 {ver}　wheel 总体积 {total/1048576:.1f} MB")
        for k, tags in sorted(by_plat.items()):
            uniq = sorted(set(tags))
            print(f"  {k:12} {len(tags):2} 个 wheel　Python 标签：{', '.join(uniq)}")
        print("  （含 cp313 的 win_amd64 wheel 体积见下载结果；源码包另计）")
    except Exception as e:
        print(f"  查询失败：{type(e).__name__} {e}")


def read_wic_bin(path: Path) -> tuple[np.ndarray, dict]:
    with path.open("rb") as f:
        head = f.read(16)
        magic = head[:4]
        if magic != b"WIC1":
            raise ValueError(f"头部不对：{magic!r}")
        w, h, ch = (int.from_bytes(head[4:8], "little"),
                    int.from_bytes(head[8:12], "little"),
                    int.from_bytes(head[12:16], "little"))
        buf = np.frombuffer(f.read(), dtype=np.uint8)
    arr = buf.reshape(h, w, ch)[:, :, ::-1].copy()      # BGR -> RGB
    return arr, {"w": w, "h": h, "ch": ch}


def bench_wic(src: str, repeats: int = 2) -> dict:
    print("\n== 2) WIC 路径（起 PowerShell → 解码 → 落盘 → Python 读入）==")
    out: dict = {}
    for i in range(repeats):
        tmp_bin = Path(tempfile.gettempdir()) / f"wic_bench_{i}.bin"
        tmp_bin.unlink(missing_ok=True)
        t0 = time.time()
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(WIC_PS1), "-Path", src, "-Out", str(tmp_bin)],
            capture_output=True, text=True, timeout=300)
        t_call = time.time() - t0
        line = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else proc.stderr[:200]
        t0 = time.time()
        arr, meta = read_wic_bin(tmp_bin)
        t_read = time.time() - t0
        size_mb = tmp_bin.stat().st_size / 1048576
        print(f"  第{i+1}次：{line}")
        print(f"    整链 {t_call:.2f}s（含进程启动）+ 读入 {t_read:.2f}s = "
              f"{t_call + t_read:.2f}s　临时文件 {size_mb:.0f} MB　{meta['w']}×{meta['h']}")
        out = {"total": t_call + t_read, "call": t_call, "read": t_read,
               "mb": size_mb, "size": (meta["w"], meta["h"]), "arr": arr}
        tmp_bin.unlink(missing_ok=True)
    return out


def bench_embedded(src: str) -> dict:
    print("\n== 3) 内嵌 JPEG 路径（零依赖基线）==")
    t0 = time.time()
    dec = raw_io.embedded_preview(src)
    t = time.time() - t0
    arr = np.asarray(dec.image, np.uint8)
    print(f"  {t:.2f}s　{dec.image.size[0]}×{dec.image.size[1]}　"
          f"原始字节 {len(dec.jpeg_bytes)/1048576:.1f} MB（落盘即用，无解码）")
    return {"total": t, "size": dec.image.size, "arr": arr}


def main() -> int:
    pypi_wheel_coverage()
    if not Path(CR3).is_file():
        print("样本 CR3 不存在，跳过实测")
        return 1
    if shutil.which("powershell.exe") is None:
        print("没有 powershell.exe（非 Windows？），跳过 WIC 实测")
        return 1
    wic = bench_wic(CR3)
    emb = bench_embedded(CR3)

    print("\n== 4) 结论数据 ==")
    print(f"  WIC 20MP 级整链 {wic['total']:.2f}s/张（另有 {wic['mb']:.0f} MB 临时文件往返）")
    print(f"  内嵌 JPEG {emb['total']:.2f}s/张、无临时文件")
    print(f"  rawpy 本机未装；wheel 仅 0.9 MB，装了才能实测其耗时")
    print(f"  高光宽容度：WIC 比内嵌 JPEG 好约 30 倍（见 .verify 的对比结论）")
    print(f"  例：228 张批量 —— WIC 约 {wic['total']*228/60:.1f} 分钟；"
          f"内嵌 JPEG 约 {emb['total']*228/60:.1f} 分钟（不含后续修图）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
