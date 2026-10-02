"""Paths to the tools this test suite drives. Every entry can be overridden by an
environment variable of the same name; the defaults are the author's machine."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

def _p(name, default):
    return os.environ.get(name, default)

# rust-lang/rust checkout: only tests/auxiliary/minicore.rs is used from it
RUST_REPO = _p("XT_RUST_REPO", r"C:\Users\vintces\Desktop\contribute\rust")
MINICORE = os.path.join(RUST_REPO, "tests", "auxiliary", "minicore.rs")

# Espressif GCC 15 (xtensa-esp32/esp32s2/esp32s3-elf-gcc), the ABI reference
ESP_GCC_BIN = _p("XT_ESP_GCC_BIN", os.path.expanduser(r"~/.rustup/toolchains/esp/xtensa-esp-elf/bin"))
# Espressif lx106 GCC 8.4 for the ESP8266 (tools/fetch_tools.py downloads it)
LX106_GCC = _p("XT_LX106_GCC", os.path.join(ROOT, "tools", "lx106", "xtensa-lx106-elf", "bin", "xtensa-lx106-elf-gcc.exe"))
# QEMU with the Xtensa "sim" machine (upstream QEMU is enough)
QEMU = _p("XT_QEMU", r"D:\Software\qemu\qemu-system-xtensa.exe")
# esp-clang (Espressif's clang with the Xtensa C ABI), for the clang-frontend sweep
ESP_CLANG = _p("XT_ESP_CLANG", os.path.join(ROOT, "tools", "esp-clang", "bin", "clang.exe"))

# llc builds: upstream LLVM main, and the same tree with the fix applied
LLC_BASE = _p("XT_LLC_BASE", r"C:\Users\vintces\Desktop\contribute\llc-variants\llc-base.exe")
LLC_FIXED = _p("XT_LLC_FIXED", r"C:\Users\vintces\Desktop\contribute\llc-variants\llc-B.exe")
LLC_A = _p("XT_LLC_A", r"C:\Users\vintces\Desktop\contribute\llc-variants\llc-A.exe")
LLC_AB = _p("XT_LLC_AB", r"C:\Users\vintces\Desktop\contribute\llc-variants\llc-AB.exe")

# llvm-project checkout with the fix in its working tree, and build directories:
# LLVM_BUILD provides llvm-lit/llc for the Xtensa tests, LLVM_CHECK_BUILD is configured with
# /utf-8 (MSVC) for the complete check-llvm run
LLVM_SRC = _p("XT_LLVM_SRC", r"C:\Users\vintces\Desktop\contribute\llvm-project")
LLVM_BUILD = _p("XT_LLVM_BUILD", r"C:\Users\vintces\Desktop\contribute\llvm-build")
LLVM_CHECK_BUILD = _p("XT_LLVM_CHECK_BUILD", r"C:\Users\vintces\Desktop\contribute\llvm-build-check")
VCVARS = _p("XT_VCVARS", r"C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat")

# rustc builds: with the rustc-side fix (small integers stay Direct on the stack),
# without it (upstream), and the esp-rs toolchain the issue was found with
RUSTC_FIXED = _p("XT_RUSTC_FIXED", r"C:\Users\vintces\Desktop\contribute\rust\build\x86_64-pc-windows-msvc\stage1\bin\rustc.exe")
RUSTC_ORIG = _p("XT_RUSTC_ORIG", os.path.join(ROOT, "tools", "rustc-orig", "bin", "rustc.exe"))
RUSTC_ESP = _p("XT_RUSTC_ESP", os.path.expanduser(r"~/.rustup/toolchains/esp/bin/rustc.exe"))

# Git Bash, needed on Windows where some LLVM utilities rely on a POSIX shell
BASH = _p("XT_BASH", r"D:\Software\Git\usr\bin\bash.exe")

# where runs write their (large) output; results/ only receives summaries
RUNS = _p("XT_RUNS", os.path.join(ROOT, "runs"))
CARGO_TARGET_DIR = _p("XT_CARGO_TARGET_DIR", r"C:\t\xt278")   # short path: Windows MAX_PATH
