# 3. LLVM: stack slots after a byval aggregate

## Where it happens

The stack offset of every argument is decided during SelectionDAG construction, in the calling
convention analysis `CC_Xtensa_Custom` (`llvm/lib/Target/Xtensa/XtensaISelLowering.cpp`), which
`LowerCall` (caller) and `LowerFormalArguments` (callee) run for every argument. It keeps a
running `StackSize` and places each stack argument with `AllocateStack(size, alignment)`:
`offset = alignTo(StackSize, alignment); StackSize = offset + size`.

For a `byval` aggregate, upstream code does

```cpp
unsigned ByValSize = ArgFlags.getByValSize();
if (ByValSize < 4) {
  ByValSize = 4;
}
if (ByValAlign < Align(4)) {
  ByValAlign = Align(4);
}
unsigned Offset = State.AllocateStack(ByValSize, ByValAlign);
```

Sizes below 4 are rounded up, but 5, 6, 7, 9, ... are not. After a 6-byte aggregate at offset 0,
`StackSize` is 6. What follows decides whether that goes unnoticed:

| next | alignment used | offset | correct (GCC, ISA RM 8.1.4) |
|---|---|---|---|
| `i32`, `f32` | 4 | 8 | 8 |
| `i64`, `f64` | 8 | 8 | 8 |
| another `byval` | >= 4 | 8 | 8 |
| **`i8`/`i16`** (promoted to i32, original alignment 1/2) | 1 or 2 | **6** | 8 |
| **varargs area** (`va_start` uses `StackSize` directly) | none | **6** | 8 |
| **outgoing argument area size** (`CALLSEQ_END` uses the raw size, `CALLSEQ_START` the aligned one) | - | **6 vs 8** | 8 |

Before the rustc fix, rustc never produced the first case: small integers on the stack were
`byval([N x i8])` with N < 4, which the code above rounds up. The other two cases exist today
with any front end ([results/repro/03_llvm_lowering.txt](../results/repro/03_llvm_lowering.txt), part b):

1. **small integer after the aggregate at offset 6**: callee and caller use a misaligned
   `l32i`/`s32i` (a hardware exception on Xtensa) and disagree with GCC;
2. **asymmetric call frame**: `ADJCALLSTACKDOWN 8` / `ADJCALLSTACKUP 6`. With a reserved call
   frame the pseudos disappear (only `-verify-machineinstrs` notices), but with a dynamic alloca
   they become real stack pointer updates, `sp -= 8` before and `sp += 6` after every call, and
   the stack pointer drifts out of alignment;
3. **varargs area at offset 6**: a variadic callee reads its first stack vararg 2 bytes too low.

## The fix

```cpp
    // Every stack argument occupies whole words, so a byval aggregate whose
    // size is not a multiple of 4 must not leave the next stack argument, the
    // varargs area or the call frame size misaligned.
    unsigned ByValSize = alignTo(ArgFlags.getByValSize(), 4);
```

(`patches/llvm-xtensa-byval-word-size.patch`, against llvm-project `3b8bb2720664`.) The copy
itself still uses the original size (`Flags.getByValSize()` in `LowerCall`), only the space
reserved for the argument is rounded up, exactly the padding the ISA manual describes.

### Why this is safe

Only calls with a byval aggregate whose size is not a multiple of 4 change, and in those only
the three wrong results above change. Everything else produces byte-identical code; this was
checked function by function on every generated test module and on a real-world corpus
([4. Testing](04-testing.md)).

### The alternative that was not chosen

Raising the alignment of promoted `i8`/`i16` stack arguments to 4
(`std::max(OrigAlign, Align(4))` in the non-byval branch) fixes only case 1. The tests show it
leaves case 2 (verifier errors, SP drift) and case 3 (a variadic callee reads the wrong address
and faults) unchanged. Applied together with the byval rounding it produces byte-identical code
to the byval rounding alone, so it is redundant.

Next: [4. Testing](04-testing.md)
