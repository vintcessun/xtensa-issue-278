# Xtensa `extern "C"`: small integers and odd-size aggregates on the stack

Tests and reproduction for [esp-rs/rust#278](https://github.com/esp-rs/rust/issues/278): Rust
calling C on an ESP32-S3 passes `u16` arguments that do not fit in `a2`..`a7` with garbage in
their upper half. The investigation leads through rustc into LLVM and ends with a change on each
side. This repository contains the reproduction, the analysis, and everything used to test the
changes.

## The short version

| layer | problem | change |
|---|---|---|
| rustc (`rustc_target/src/callconv/xtensa.rs`) | integers that go on the stack are passed `byval` (plain memory): LLVM copies 2 bytes into a 4-byte slot, nothing extends them | keep integers of 32 bits or less `Direct` with `zeroext`/`signext` on the stack too |
| LLVM (`XtensaISelLowering.cpp`, `CC_Xtensa_Custom`) | a `byval` aggregate of 5, 6, 7, ... bytes is not padded to whole words: a following small integer lands at offset 6, the call frame is 6 bytes while its setup reserves 8 (the stack pointer drifts with a dynamic alloca), a varargs area starts at offset 6 | `ByValSize = alignTo(ByValSize, 4)` ([patch](patches/llvm-xtensa-byval-word-size.patch)) |

Both follow the Xtensa ISA Reference Manual, 8.1.4: *"All arguments consist of an integral
number of 4-byte words ... Integer values smaller than a word ... the upper bits set to zero for
unsigned values or sign-extended for signed values ... If the size of a structure is not an
integral number of words, padding is inserted"*, and match GCC.

## Results at a glance

Full tables: [results/summary.md](results/summary.md).

* The esp-nn signature from the issue, run on QEMU:
  * upstream rustc + LLVM: 7 of 15 arguments arrive wrong;
  * both changes: 0 of 15.
* LLVM lit:
  * the new test fails with the upstream source and passes with the fix;
  * all 110 Xtensa tests pass.
* check-llvm: 78529 tests, 0 new failures. The 2 failures also fail with the upstream source.
* Runtime sweeps against GCC:
  * Coverage: 1968 + 11714 IR cases and 1968 esp-clang cases, on ESP32, ESP32-S2, ESP32-S3, call0,
    ESP8266 and a real lx106 setup, at `llc -O0`..`-O3`.
  * Every case the fix changes fails exactly like its word-rounded twin.
  * Every case it must not change gives field-by-field identical results with upstream LLVM in
    the same configuration.
* Code comparison over 170370 generated functions:
  * only functions with an odd-size byval aggregate change;
  * no new verifier failures;
  * 7626 verifier failures are fixed.
* Real code: 6336 functions from esp-nn, mbedTLS, zlib, cJSON, littlefs, Lua, lwIP, SQLite, core,
  alloc, compiler_builtins and 15 no_std crates. No code changes, no new verifier failures.

## Reading order

1. [The issue](docs/01-issue.md) - symptom, correction of the issue text, what the ABI requires
2. [rustc](docs/02-rustc.md) - how the argument reaches LLVM, what has to change
3. [LLVM](docs/03-llvm.md) - the byval stack slot, the three consequences, the fix and its alternative
4. [Testing](docs/04-testing.md) - what each test proves and how failures are attributed
5. [Pre-existing problems](docs/05-preexisting.md) - found on the way, not caused by the change
6. [Results](results/summary.md) - generated from the raw runs

The reproduction steps print their evidence:
[symptom](results/repro/01_symptom.txt) ·
[rustc IR](results/repro/02_rustc_abi.txt) ·
[LLVM lowering](results/repro/03_llvm_lowering.txt) ·
[GCC reference](results/repro/04_gcc_reference.txt)

## Layout

```
repro/        the issue, step by step (symptom on QEMU, rustc IR, LLVM lowering, GCC reference)
harness/      test generators and runners (QEMU, GCC, rustc, llc, esp-clang)
corpus/       real-world code comparison (esp-nn, mbedTLS, zlib, cJSON, littlefs, Lua, lwIP,
              SQLite; core/alloc/compiler_builtins and no_std crates)
patches/      the LLVM change and its lit test, and the llvm-project commit it applies to
tools/        fetch_tools.py (lx106 GCC, esp-clang) and llvm_tests.py (lit, check-llvm)
docs/         the write-up
results/      summaries (raw runs go to runs/, which is not tracked)
run_all.py    runs or resumes every stage, retries failures, reports anything incomplete
summarize.py  builds results/summary.md from runs/
```

## Running

Requirements: Python 3.10+, QEMU with `qemu-system-xtensa`, the esp-rs toolchain (for Espressif's
GCC 15 and the esp rustc), Git Bash on Windows, and three compiler builds you provide:

* `XT_LLC_BASE` / `XT_LLC_FIXED`: `llc` from llvm-project at the commit in
  [patches/llvm-base-commit.txt](patches/llvm-base-commit.txt), without and with the patch
  (`-DLLVM_EXPERIMENTAL_TARGETS_TO_BUILD=Xtensa`);
* `XT_RUSTC_FIXED`: a rustc with the ABI change described in [docs/02](docs/02-rustc.md);
* `XT_RUSTC_ORIG`: the same rustc without it (for the "upstream" rows).

```
python tools/fetch_tools.py        # downloads the lx106 GCC and esp-clang into tools/
python run_all.py --jobs 4         # all stages; safe to interrupt and re-run
python summarize.py
```

Paths and their environment-variable overrides are in [harness/config.py](harness/config.py).

## Notes

* The rustc change is not included here. It is described in [docs/02](docs/02-rustc.md); the
  tests take any rustc that implements it.
* Authorship and tool use: _to be written by the author._
