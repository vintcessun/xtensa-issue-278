# 4. Testing

All runtime tests execute on QEMU (`qemu-system-xtensa -M sim`), with GCC as the ABI reference.
A stack full of `0xA5` before each call makes a half-written argument slot visible.

## Pieces

| stage | what it proves | script |
|---|---|---|
| `repro` | the issue, step by step: symptom, rustc IR, LLVM lowering, GCC reference | `repro/0*.py` |
| `rust` | Rust callers and callees against GCC: random signatures, the esp-nn signature, varargs; fixed rustc + fixed LLVM; upstream rustc + fixed LLVM changes nothing but odd-size byval functions | `harness/qemu_matrix.py`, `qemu_twin.py` |
| `v1` | first systematic sweep: 1968 IR cases (byval 1-40 bytes, alignment 1-16, following types, varargs, dynamic-alloca callers); esp-clang front end; base vs fixed on cases the fix must not change; per-function code comparison | `harness/irsweep.py`, `clangsweep.py`, `perfn.py` |
| `v2` | extended sweep: 11714 cases - every stack position reached through GCC's register assignment, padded / packed / nested / float / union aggregates up to 255 bytes, sret returns, several odd aggregates in a row, each odd case paired with a word-rounded twin | `harness/irsweep2.py` |
| `perfn2` | per-function asm + verifier comparison of every v2 module | `harness/perfn.py` |
| `confirm` | every case the fix must not change that fails with the fixed LLVM, rerun with both LLVM builds in the same chip / opt / va_arg configuration | `harness/sweepdata.py`, the sweep scripts |
| `corpus` | real code: esp-nn, mbedTLS, zlib, cJSON, littlefs, Lua, lwIP, SQLite (esp-clang) and core/alloc/compiler_builtins plus 15 no_std crates (rustc); every function whose code changes must involve an odd-size byval aggregate; modules upstream `llc` cannot compile are split with `llvm-split` and compared part by part | `corpus/corpus.py` |
| `lit` / `check-llvm` | the new lit test fails before and passes after; LLVM's whole test suite | LLVM's `llvm-lit` |

## Paths inside one runtime case

```
c->ir      GCC caller   -> LLVM callee        (callee exports every value; aggregates as a checksum
                                               of their data bytes, padding excluded)
ir->c      LLVM caller  -> GCC callee         (callee reads sub-word integers as full words,
                                               like esp-nn's l32i)
irdyn->c   LLVM caller with a dynamic alloca  (non-reserved call frame: ADJCALLSTACK really moves
           -> GCC callee, three calls          sp; any asymmetry misaligns it)
c->c       GCC caller   -> GCC callee         (control: validates the reference itself)
```

## How a failure is attributed

* **Cases the fix must not change** (no aggregate whose size is a multiple-of-4-plus-1..3 and
  larger than 4): run with upstream and fixed LLVM, results compared case by case and field by
  field. They must be identical, including identical pre-existing failures. Each such case that
  fails with the fixed LLVM counts as explained only if it ran identically with both builds in
  the same configuration: chip, `llc` opt level, and IR `va_arg` or not. Stage `confirm` provides
  that comparison for every failing case.
* **Cases the fix does change**: compared with their *twin*, the same case with every odd
  aggregate replaced by a word-multiple aggregate of the rounded size. With a correct fix both
  lay out every other argument identically, so their failures (outside the replaced aggregate)
  must be identical.
* **Code identity**: every generated module is compiled function by function with both LLVM
  builds; functions without an odd-size byval aggregate must produce byte-identical assembly,
  and the fixed build may not fail the machine verifier where upstream passes.

## Configurations

ESP32, ESP32-S2, ESP32-S3 (windowed, each chip's own GCC), generic call0, ESP8266 CPU with ESP32
GCC in call0 mode, and a real ESP8266 setup (Espressif's lx106 GCC 8.4 and QEMU's lx106 core);
llc at `-O0`..`-O3`; rustc at opt-level 0 and 3; esp-clang at `-O0` and `-O2`.

## Running

```
python tools/fetch_tools.py          # lx106 GCC, esp-clang (once)
python run_all.py --jobs 4           # every stage; resumes, retries, lists anything incomplete
python summarize.py                  # writes results/summary.md
```

Tool locations are in [harness/config.py](../harness/config.py) and can be overridden with
environment variables (`XT_LLC_BASE`, `XT_LLC_FIXED`, `XT_RUSTC_FIXED`, ...).

Results: [results/summary.md](../results/summary.md). Pre-existing problems found on the way:
[5. Pre-existing problems](05-preexisting.md).
