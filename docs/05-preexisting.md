# 5. Pre-existing problems found while testing (not caused by the fix)

Each of these shows up identically with upstream LLVM and with the fixed LLVM. They are listed so
they are not mistaken for regressions and can be reported separately; none of them is addressed
by this change.

1. **LLVM `va_start` and 8-byte-aligned varargs on the stack.** The varargs index of a variadic
   callee starts at `StackSize`, and `va_arg` aligns the *index* for `i64`/`f64`. GCC's
   `va_start` keeps the index relative to the caller's stack pointer, so both agree only when the
   argument area before the varargs is 8-byte aligned. When it is not, a variadic callee
   compiled by LLVM (Rust with `...`, or esp-clang) reads `i64`/`f64` varargs from the wrong
   address. In the sweeps these are the `c->ir` failures of vararg cases; every one of them fails
   in exactly the same way for the word-rounded twin of the case and with upstream LLVM.
2. **LLVM's own IR `va_arg` lowering (`LowerVAARG`).** Its index arithmetic for arguments already
   in the stack area differs from GCC's; with it, twice as many vararg reads fail as with the
   GCC-style expansion rustc emits. rustc does not use it.
3. **`byval` copies lowered to a `memcpy` call nested in the outgoing call sequence.** For some
   aggregate sizes the byval copy becomes a library call emitted between the outer call's
   `CALLSEQ_START` and `CALLSEQ_END`; `-verify-machineinstrs` reports "FrameSetup is after
   another FrameSetup". Most functions that fail the verifier with *both* LLVM builds are this.
4. **call0 + `-O0` + dynamic alloca: `llc` aborts** with "Error while trying to spill A12 from
   class AR: Cannot scavenge register without an emergency spill slot!" (in Prologue/Epilogue
   Insertion). This is why the esp-clang sweep has no runtime results for ESP8266 at `-O0`; the 40
   affected runs are recorded as this known error instead.
5. **GCC is not self-consistent for 16-byte-aligned aggregates followed by varargs.** A
   GCC-compiled variadic callee disagrees with a GCC-compiled caller. These cases have no
   reference and are excluded (they are the `c->c` control failures).
6. **esp-clang does not extend small integers on the stack.** Espressif's clang
   (`clang/lib/CodeGen/Targets/Xtensa.cpp`) passes them `byval` like upstream rustc does, i.e. the
   same bug as #278. In the esp-clang sweep every small-integer failure has the correct low bits
   and unextended upper bits; no fixed-size parameter is ever misplaced.
7. **Upstream `llc` cannot compile some functions of `core`, `compiler_builtins`, `libm` and
   `micromath`** for Xtensa at `-O2`. It fails with "Cannot select: ... TargetConstantPool" (a
   `[2 x float]` constant pool), or with the register-scavenger error of item 4. The corpus stage
   splits these modules with `llvm-split` and compares every part that compiles. The functions
   that still fail are listed in `runs/corpus/split.json`.
8. **Two `check-llvm` tests fail in this Windows build**: `MC/AsmParser/block-comment.s` and
   `tools/llvm-remarkutil/filter/filter.test`. `tools/llvm_tests.py check` reruns both with the
   upstream source restored, and they fail there as well.

Harness notes (not compiler bugs):
* The ESP toolchain's `libgcc` is built for the windowed ABI only, so call0 images carry their own
  `__mulsi3`.
* Bare-metal windowed code needs the MOVSP (alloca) exception handler, which the harness vectors
  provide.
* Large generated modules exceed the `l32r` literal range. `irsweep2.py` therefore compiles with
  `-function-sections`, which places each function's literal pool next to it.
