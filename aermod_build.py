"""下載美國環保署（EPA）AERMOD／AERMET／AERMAP 原始碼並用 gfortran 編譯。

用法：python aermod_build.py [aermod aermet aermap]   （預設三個都編）
輸出：bin/aermod、bin/aermet、bin/aermap，以及 bin/VERSIONS.txt

編譯採「多輪重試」：每輪編譯所有尚未成功的檔案，模組依賴未就緒的檔案會失敗，
下一輪再試，直到全部成功或沒有進展。這樣不必寫死檔案順序，EPA 改版增減檔案也能編。"""
from __future__ import annotations

import glob
import io
import os
import shutil
import subprocess
import sys
import time
import zipfile

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "bin")
SCRAM = "https://gaftp.epa.gov/Air/aqmg/SCRAM/models"
TARGETS = {
    #  名稱: (下載網址, 主程式檔名, 副檔名, 編譯參數)
    "aermod": (f"{SCRAM}/preferred/aermod/aermod_source.zip", "aermod.f", "*.f",
               ["-O2", "-frecursive", "-fno-range-check", "-w"]),
    "aermet": (f"{SCRAM}/met/aermet/aermet_source.zip", "aermet.f90", "*.f90",
               ["-O2", "-ffree-form", "-w"]),
    "aermap": (f"{SCRAM}/related/aermap/aermap_source.zip", "aermap.f", "*.f",
               ["-O2", "-frecursive", "-w"]),
}


def download(url: str, tries: int = 6) -> bytes:
    for i in range(1, tries + 1):
        try:
            r = requests.get(url, timeout=(30, 300))
            r.raise_for_status()
            zipfile.ZipFile(io.BytesIO(r.content)).testzip()  # 確認是完整的 zip
            return r.content
        except Exception as e:  # noqa: BLE001
            print(f"  下載失敗（第 {i}/{tries} 次）：{e}")
            time.sleep(10 * i)
    raise RuntimeError(f"無法下載 {url}")


def build(name: str, src_dir: str | None = None) -> str:
    url, marker, pattern, flags = TARGETS[name]
    work = os.path.join(HERE, "build_tmp", name)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    if src_dir is None:
        print(f"[{name}] 下載 {url}")
        zipfile.ZipFile(io.BytesIO(download(url))).extractall(work)
        hits = glob.glob(os.path.join(work, "**", marker), recursive=True)
        if not hits:
            raise RuntimeError(f"壓縮檔內找不到 {marker}")
        src_dir = os.path.dirname(hits[0])
    version = os.path.basename(os.path.normpath(src_dir))
    files = sorted(glob.glob(os.path.join(src_dir, pattern)))
    print(f"[{name}] 原始碼目錄 {version}，共 {len(files)} 個檔案")
    obj = os.path.join(work, "obj")
    os.makedirs(obj, exist_ok=True)
    pending, done, last_err = list(files), [], {}
    for rnd in range(1, 15):
        progress = False
        for f in list(pending):
            o = os.path.join(obj, os.path.splitext(os.path.basename(f))[0] + ".o")
            p = subprocess.run(["gfortran", "-c", *flags, "-J", obj, "-I", obj, f, "-o", o],
                               capture_output=True, text=True)
            if p.returncode == 0:
                pending.remove(f)
                done.append(o)
                progress = True
            else:
                last_err[f] = p.stderr[-800:]
        print(f"[{name}] 第 {rnd} 輪：已完成 {len(done)}／{len(files)}")
        if not pending:
            break
        if not progress:
            for f in pending:
                print(f"  無法編譯 {os.path.basename(f)}：\n{last_err.get(f, '')}")
            raise RuntimeError(f"{name} 編譯失敗")
    os.makedirs(BIN, exist_ok=True)
    exe = os.path.join(BIN, name)
    p = subprocess.run(["gfortran", "-O2", "-o", exe, *done], capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stderr[-2000:])
        raise RuntimeError(f"{name} 連結失敗")
    print(f"[{name}] 完成：{exe}")
    return version


def main():
    names = sys.argv[1:] or list(TARGETS)
    versions = []
    for n in names:
        src = os.environ.get(f"{n.upper()}_SRC_DIR")
        versions.append(f"{n}: {build(n, src)}")
    with open(os.path.join(BIN, "VERSIONS.txt"), "w") as f:
        f.write("\n".join(versions) + "\n")
    print("\n".join(versions))
    shutil.rmtree(os.path.join(HERE, "build_tmp"), ignore_errors=True)


if __name__ == "__main__":
    main()
