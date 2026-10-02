#!/usr/bin/env python3
"""Step 3 - how LLVM lowers what rustc produced.

(a) The IR from step 2 through llc (upstream LLVM) and llc (fixed LLVM): how each stack
    argument of esp_nn_max_pool_s8_esp32s3 is stored by the caller.
      byval([2 x i8]) -> a 2-byte copy (s16i) into a 4-byte slot: the upper half keeps whatever
                         was on the stack. This is the bug of #278; LLVM cannot fix it because
                         byval carries no signedness.
      i16 zeroext     -> widened to 32 bits and stored with s32i.
    => the rustc change is required.

(b) Once rustc passes sub-word integers as values, they meet a second, LLVM-side problem:
    CC_Xtensa_Custom only rounds byval sizes below 4 up to 4. A byval aggregate of 5, 6, 7, ...
    bytes leaves the stack offset misaligned, which shows up as
      1. a small integer after it placed at offset 6 instead of 8 (misaligned l32i on hardware)
      2. an outgoing argument area of 6 bytes while the call frame setup uses 8: with a
         non-reserved call frame (dynamic alloca) the stack pointer drifts by 2 on every call
      3. the varargs area of a variadic callee starting at offset 6 instead of 8
    => the LLVM change (byval size rounded up to a whole number of words) is required too.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import OUT, ROOT, Report, config, run, function_asm

R = Report("03_llvm_lowering")
LLCS = {"upstream LLVM": config.LLC_BASE, "fixed LLVM": config.LLC_FIXED}

def asm(llc, ll, mcpu):
    return run([llc, "-mtriple=xtensa", "-O2", f"-mcpu={mcpu}", ll, "-o", "-"]).stdout

# ---------------------------------------------------------------- (a) the issue's call
R("(a) caller of esp_nn_max_pool_s8_esp32s3: stores into the outgoing stack argument area (ESP32-S3, windowed)\n")
for ir_tag in ("upstream", "fixed"):
    ll = os.path.join(OUT, "02_" + ir_tag, "lib.ll")
    if not os.path.exists(ll):
        raise SystemExit("run 02_rustc_abi.py first")
    for llc_name, llc in LLCS.items():
        body = function_asm(asm(llc, ll, "esp32s3"), "call_max_pool")
        # outgoing argument area: 9 stack words at sp+0 .. sp+32 (the frame's own spills sit above)
        stores = {}
        for l in body:
            m = re.search(r"\b(s8i|s16i|s32i)(?:\.n)?\s+a\d+,\s*a1,\s*(\d+)", l)
            if m and int(m.group(2)) <= 32:
                stores[int(m.group(2))] = m.group(1)
        cells = " ".join(f"+{o}:{stores[o]}" for o in sorted(stores))
        R(f"  IR from {ir_tag:<8} rustc + {llc_name:<13}: {cells}")
    R("")
R("  upstream rustc: s16i whatever LLVM does (byval has no signedness to extend)")
R("  fixed rustc:    extended values stored with s32i\n")

# ---------------------------------------------------------------- (b) odd-size byval aggregates
R("(b) byval aggregates whose size is not a multiple of 4 (IR from the LLVM lit test)\n")
lit = os.path.join(ROOT, "patches", "calling-conv-stack-align.ll")
import difflib
cases = [
    ("1. i16 after a 6-byte byval - callee reads it (expected at incoming sp+8)", "callee_i16_after_byval6"),
    ("1. i16 after a 6-byte byval - caller stores it (expected at sp+8)", "caller_i16_after_byval6"),
    ("2. 6-byte byval as last argument, dynamic alloca: sp -8 before, must be +8 after", "caller_byval6_last_dynamic_alloca"),
    ("3. varargs after a 6-byte byval: va_stk = first vararg - 32 (expected sp+8)", "va_start_after_byval6"),
]
for mcpu, abi in (("generic", "call0"), ("esp32s3", "windowed")):
    R(f"  ==== {abi} ABI (-mcpu={mcpu}); lines that differ between upstream (-) and fixed (+) LLVM")
    a_mod, b_mod = asm(config.LLC_BASE, lit, mcpu), asm(config.LLC_FIXED, lit, mcpu)
    for title, fn in cases:
        a, b = function_asm(a_mod, fn), function_asm(b_mod, fn)
        d = [l for l in difflib.unified_diff([x.strip() for x in a], [x.strip() for x in b], lineterm="", n=0) if l[:1] in "+-" and l[:3] not in ("+++", "---")]
        R(f"    {title}")
        for l in d:
            R(f"        {l}")
        if not d:
            R("        (identical)")
    R("")
R("Reading the diffs: an 'addi aX, a1, N' before the access is the argument address. In the windowed")
R("ABI the callee's sp is lowered by its frame ('entry a1, F'), so incoming argument k sits at sp+F+k.")
R("Correct (Xtensa ISA RM 8.1.4, GCC): offset 8, symmetric sp adjustment, varargs starting at offset 8.")
R.save()
