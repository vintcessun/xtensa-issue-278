# 2. rustc: how the argument reaches LLVM

## The call chain

```
rustc_ty_utils::abi::fn_abi_new_uncached
  -> FnAbi::adjust_for_foreign_abi            (rustc_target/src/callconv/mod.rs)
     -> xtensa::compute_abi_info -> classify_arg_ty   (rustc_target/src/callconv/xtensa.rs)
  -> rustc_codegen_llvm: PassMode -> LLVM IR parameter attributes
       Direct + ext   -> i16 zeroext / signext
       Indirect on_stack -> ptr byval(<type>)
  -> LLVM Xtensa backend assigns a register or a stack slot and emits the stores
```

Calls between Rust functions (the Rust ABI) do not go through `xtensa.rs` and are not affected.

## What upstream rustc does

`classify_arg_ty` first counts the argument registers that are left. When an argument no longer
fits, it sets `must_use_stack` and passes the argument with `arg.pass_by_stack_offset(None)`,
i.e. `PassMode::Indirect { on_stack: true }` - for *every* type, scalars included. Only the
branch for arguments that do fit applies `extend_integer_width_to(32)`.

For the issue's call this gives, in LLVM IR
([results/repro/02_rustc_abi.txt](../results/repro/02_rustc_abi.txt)):

```
 6 stride_wd  u16   ptr noundef byval([2 x i8]) align 2     <- upstream and esp-rs rustc
 6 stride_wd  u16   i16 noundef zeroext                     <- rustc with the fix
```

`byval([2 x i8])` means "copy two bytes of memory onto the stack". It carries no signedness, so
LLVM cannot extend it: whatever LLVM does, the caller writes two bytes (`s16i`) into a four-byte
slot ([results/repro/03_llvm_lowering.txt](../results/repro/03_llvm_lowering.txt), part a).
`extend_integer_width_to` only acts on `PassMode::Direct`, so calling it in the stack branch
would not help either.

## What rustc has to do instead

Integers of 32 bits or less (and `bool`, `f32`, pointers, and `repr(transparent)` wrappers of
them) must stay `PassMode::Direct` when they go on the stack, with the same `zeroext`/`signext`
extension as in registers. Only aggregates and scalars wider than 32 bits keep being passed
`byval` when they do not fit, so that:

* an `i64`/`f64` that finds only one free register still goes entirely on the stack (the ABI
  forbids splitting it), and
* aggregates keep LLVM's "byval uses up the remaining registers" behaviour, which matches GCC.

A scalar of 32 bits or less only goes to the stack once no register is left, so rustc's and
LLVM's register counting cannot disagree.

> The rustc change itself is not part of this repository. The Rust project's LLM policy requires
> that this change be written by a human contributor; this repository only tests a rustc that has
> it (`XT_RUSTC_FIXED`, see [harness/config.py](../harness/config.py)).

Once rustc passes sub-word integers by value, a second problem appears, this time in LLVM.

Next: [3. LLVM](03-llvm.md)
