#!/usr/bin/env python3
"""Download the third-party toolchains the harness uses into tools/ (idempotent).

  tools/lx106      Espressif lx106 GCC 8.4 (ESP8266 reference compiler)
  tools/esp-clang  Espressif clang 22.1.4 (Xtensa C ABI front end for the clang sweep)

Not downloaded (see README): the Espressif GCC 15 (comes with the esp-rs toolchain, `espup`),
QEMU, the two llc builds and the rustc builds.
"""
import os, sys, tarfile, urllib.request, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
LX106 = "https://dl.espressif.com/dl/xtensa-lx106-elf-gcc8_4_0-esp-2020r3-win32.zip"
ESP_CLANG = ("https://github.com/espressif/llvm-project/releases/download/esp-22.1.4_20260825/"
             "clang-esp-22.1.4_20260825-x86_64-w64-mingw32.tar.xz")

def get(url, dst):
    if not os.path.exists(dst):
        print("downloading", url)
        urllib.request.urlretrieve(url, dst + ".part")
        os.replace(dst + ".part", dst)
    return dst

def main():
    os.makedirs(TOOLS, exist_ok=True)
    if not os.path.exists(os.path.join(TOOLS, "lx106", "xtensa-lx106-elf", "bin", "xtensa-lx106-elf-gcc.exe")):
        with zipfile.ZipFile(get(LX106, os.path.join(TOOLS, "lx106.zip"))) as z:
            z.extractall(os.path.join(TOOLS, "lx106"))
    if not os.path.exists(os.path.join(TOOLS, "esp-clang", "bin", "clang.exe")):
        with tarfile.open(get(ESP_CLANG, os.path.join(TOOLS, "esp-clang.tar.xz"))) as t:
            t.extractall(TOOLS)          # the archive's top-level directory is esp-clang/
    print("tools ready in", TOOLS)

if __name__ == "__main__":
    sys.exit(main())
