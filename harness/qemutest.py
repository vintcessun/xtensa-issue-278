#!/usr/bin/env python3
"""Run-time Xtensa C-ABI test on QEMU: GCC <-> rustc, both directions.

For every random signature f:
  path R: GCC-compiled caller  -> rustc-compiled callee r<f>   (callee exports its params)
  path G: rustc-compiled caller g<f> -> GCC-compiled "widened" callee w<f>
  path C: GCC-compiled caller gc<f>  -> the same widened callee   (control)
The widened callee declares every sub-word integer parameter as uint32_t, i.e. it
reads the whole 4-byte slot like hand-written assembly using l32i does. The rust
caller derives sub-word arguments by truncating u32 values whose upper bits are junk,
and the stack is poisoned before each call, so any missing extension or misplaced
argument shows up as a wrong value.

usage: qemutest.py RUSTC OUTDIR [--abi call0|windowed] [--n N] [--seed S]
"""
import argparse, os, random, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from difftest import STRUCTS, SCALARS, FIELD_NAMES, gen_sigs, REPO, TARGET

import config
GCCBIN = config.ESP_GCC_BIN
GCC = os.path.join(GCCBIN, "xtensa-esp32s3-elf-gcc.exe")
QEMU = config.QEMU
SMALL = {"u8": ("uint8_t", False, 8), "i8": ("int8_t", True, 8), "u16": ("uint16_t", False, 16),
         "i16": ("int16_t", True, 16), "bool": ("bool", False, 1)}
FSIZE = {"u8": 1, "u16": 2, "u32": 4, "u64": 8}

def ctype(p): return p if p in STRUCTS else SCALARS[p][0]
def rtype(p): return p if p in STRUCTS else SCALARS[p][1]

def outputs(p):
    """(suffix, C expr on param x, Rust expr on param x, expected C expr on source s) per exported word."""
    if p in STRUCTS:
        fields, _ = STRUCTS[p]
        res = []
        for i in ([0] if len(fields) == 1 else [0, len(fields) - 1]):
            fn = FIELD_NAMES[i]
            if fields[i] == "u64":
                res += [(fn + "lo", f"(uint32_t)x.{fn}", f"x.{fn} as u32", f"(uint32_t)s.{fn}"),
                        (fn + "hi", f"(uint32_t)(x.{fn} >> 32)", f"transmute::<u64, W>(x.{fn}).hi", f"(uint32_t)(s.{fn} >> 32)")]
            else:
                res.append((fn, f"(uint32_t)x.{fn}", f"x.{fn} as u32", f"(uint32_t)s.{fn}"))
        return res
    if p in ("u64", "i64", "f64"):
        cx = "((UD){.d=x}).u" if p == "f64" else "(uint64_t)x"
        rx = "transmute::<f64, W>(x)" if p == "f64" else "transmute::<_, W>(x)"
        return [("lo", f"(uint32_t){cx}", f"{rx}.lo", "(uint32_t)s"), ("hi", f"(uint32_t)({cx} >> 32)", f"{rx}.hi", "(uint32_t)(s >> 32)")]
    if p == "f32":
        return [("", "((UF){.f=x}).u", "transmute::<f32, u32>(x)", "s")]
    if p == "bool":
        return [("", "(uint32_t)x", "x as u32", "(s & 1)")]
    if p in SMALL:
        ct, signed, _ = SMALL[p]
        return [("", "(uint32_t)x", "x as u32", f"(uint32_t)(int32_t)({ct})s" if signed else f"(uint32_t)({ct})s")]
    return [("", "(uint32_t)x", "x as u32", "(uint32_t)s")]

def src_ctype(p):
    if p in STRUCTS: return p
    return "uint64_t" if p in ("u64", "i64", "f64") else "uint32_t"

def c_arg_from_src(p, s):
    """argument expression of real type p built from source global s (C)."""
    if p in STRUCTS: return s
    if p == "f32": return f"((UF){{.u={s}}}).f"
    if p == "f64": return f"((UD){{.u={s}}}).d"
    if p == "bool": return f"(bool)({s} & 1)"
    return f"({SCALARS[p][0]}){s}"

def r_arg_from_src(p, s):
    if p in STRUCTS: return s
    if p == "f32": return f"transmute::<u32, f32>({s})"
    if p == "f64": return f"transmute::<u64, f64>({s})"
    if p == "bool": return f"transmute::<u8, bool>({s} as u8)"  # low byte is 0/1, upper bytes junk
    return f"{s} as {SCALARS[p][1]}"

def rand_src(rng, p):
    if p in STRUCTS:
        fields, _ = STRUCTS[p]
        return "{" + ", ".join(hex(rng.getrandbits(8 * FSIZE[t])) for t in fields) + "}"
    if p == "bool":
        return hex((rng.getrandbits(32) & ~0xff) | rng.getrandbits(1))
    if p in ("u64", "i64"): return hex(rng.getrandbits(64)) + "ULL"
    if p == "f64": return hex((rng.getrandbits(1) << 63) | (rng.randint(1, 2046) << 52) | rng.getrandbits(52)) + "ULL"
    if p == "f32": return hex((rng.getrandbits(1) << 31) | (rng.randint(1, 254) << 23) | rng.getrandbits(23))
    v = rng.getrandbits(32)
    if p in SMALL:  # force junk in the upper bits and exercise the sign bit
        v |= 0xA5000000
    return hex(v)

C_PRELUDE = """#include <stdint.h>
#include <stdbool.h>
typedef union { float f; uint32_t u; } UF; typedef union { double d; uint64_t u; } UD;
"""

def c_structs():
    s = []
    for name, (fields, align) in STRUCTS.items():
        attr = f" __attribute__((aligned({align})))" if align else ""
        s.append(f"typedef struct{attr} {{ {' '.join(f'{SCALARS[t][0]} {FIELD_NAMES[i]};' for i, t in enumerate(fields))} }} {name};")
    return "\n".join(s) + "\n"

REAL_CORE = False

def emit(sigs, rng):
    hdr = C_PRELUDE + c_structs()
    harness, ccaller, rcallee, rcaller = [hdr], [hdr], [], []
    rpre = (["#![no_std]", '#![crate_type = "lib"]', "#![allow(non_upper_case_globals, static_mut_refs, unused, improper_ctypes)]",
             "use core::mem::transmute;"] if REAL_CORE else
            ["#![feature(no_core, lang_items)]", "#![no_core]", '#![crate_type = "lib"]',
            "#![allow(non_upper_case_globals, static_mut_refs, unused, internal_features, improper_ctypes)]",
            "extern crate minicore;", "use minicore::*;", "use minicore::mem::transmute;"]) + [
            "#[repr(C)] pub struct W { lo: u32, hi: u32 } impl Copy for W {}"]
    for name, (fields, align) in STRUCTS.items():
        rep = f"#[repr(C, align({align}))]" if align else "#[repr(C)]"
        rpre.append(f"{rep} pub struct {name} {{ {', '.join(f'{FIELD_NAMES[i]}: {SCALARS[t][1]}' for i, t in enumerate(fields))} }} impl Copy for {name} {{}}")
    if REAL_CORE:
        import re as _re
        rpre = [_re.sub(r" impl Copy for (\w+) \{\}", lambda m: f" impl Clone for {m.group(1)} {{ fn clone(&self) -> Self {{ *self }} }} impl Copy for {m.group(1)} {{}}", l) for l in rpre]
    rcallee += rpre; rcaller += rpre
    harness.append("""
void sh_write(const char *p, int n);
static void puts_(const char *s) { int n = 0; while (s[n]) n++; sh_write(s, n); }
static void hex_(uint32_t v) { char b[9]; for (int i = 0; i < 8; i++) { int d = (v >> (28 - 4 * i)) & 15; b[i] = d < 10 ? '0' + d : 'a' + d - 10; } b[8] = 0; puts_(b); }
static void dec_(uint32_t v) { hex_(v); }
__attribute__((noinline)) void poison(void) { volatile uint32_t buf[256]; for (int i = 0; i < 256; i++) buf[i] = 0xA5A5A5A5u; }
static uint32_t fails, checks;
static void chk(const char *path, int f, const char *what, uint32_t got, uint32_t exp) {
    checks++;
    if (got != exp) { fails++; puts_("MISMATCH path="); puts_(path); puts_(" f"); dec_(f); puts_(" "); puts_(what); puts_(" got="); hex_(got); puts_(" exp="); hex_(exp); puts_("\\n"); }
}
""")
    rcaller.append('extern "C" {')
    tests = []
    for f, params in enumerate(sigs):
        real_c = ", ".join(f"{ctype(p)} p{k}" for k, p in enumerate(params))
        wide_c = ", ".join(f"{'uint32_t' if p in SMALL else ctype(p)} p{k}" for k, p in enumerate(params))
        real_r = ", ".join(f"p{k}: {rtype(p)}" for k, p in enumerate(params))
        srcs = [f"s{f}_{k}" for k in range(len(params))]
        for k, p in enumerate(params):
            harness.append(f"{src_ctype(p)} {srcs[k]} = {rand_src(rng, p)};")
            ccaller.append(f"extern {src_ctype(p)} {srcs[k]};")
            rcaller.append(f"    static {srcs[k]}: {'u64' if src_ctype(p) == 'uint64_t' else ('u32' if src_ctype(p) == 'uint32_t' else p)};")
        # outputs: o = from rust callee, c = from widened C callee
        wbody, rbody, checks = [], [], []
        for k, p in enumerate(params):
            for suf, cx, rx, ex in outputs(p):
                o, c = f"o{f}_{k}{suf}", f"c{f}_{k}{suf}"
                harness.append(f"uint32_t {o}; uint32_t {c};")
                rcallee.append(f"extern \"C\" {{ static mut {o}: u32; }}")
                x = f"p{k}"
                wexpr = f"p{k}" if p in SMALL else cx.replace("x", x)
                wbody.append(f"{c} = {wexpr};")
                rbody.append(f"{o} = {rx.replace('x', x)};")
                checks.append((o, c, ex.replace("s", srcs[k]) if p not in STRUCTS else ex.replace("s.", srcs[k] + "."), f"p{k}{suf}"))
        harness.append(f"void w{f}({wide_c}) {{ {' '.join(wbody)} }}")
        harness.append(f"void r{f}({real_c}); void g{f}(void); void gc{f}(void);")
        ccaller.append(f"void w{f}({real_c});")
        ccaller.append(f"void gc{f}(void) {{ w{f}({', '.join(c_arg_from_src(p, srcs[k]) for k, p in enumerate(params))}); }}")
        rcallee.append(f'#[no_mangle] pub unsafe extern "C" fn r{f}({real_r}) {{ {" ".join(rbody)} }}')
        rcaller.append(f"    fn w{f}({real_r});")
        rcaller_fn = f'#[no_mangle] pub unsafe extern "C" fn g{f}() {{ w{f}({", ".join(r_arg_from_src(p, srcs[k]) for k, p in enumerate(params))}); }}'
        tests.append((f, params, srcs, checks, rcaller_fn))
    rcaller.append("}")
    rcaller += [t[4] for t in tests]
    body = []
    for f, params, srcs, checks, _ in tests:
        call_r = f"r{f}({', '.join(c_arg_from_src(p, srcs[k]) for k, p in enumerate(params))});"
        lines = [f"static void t{f}(void) {{"]
        lines.append(f"  poison(); {call_r}")
        lines += [f'  chk("gcc->rust", {f}, "{w}", {o}, {e});' for o, c, e, w in checks]
        lines += [f"  {c} = 0x5A5A5A5A;" for o, c, e, w in checks]
        lines.append(f"  poison(); g{f}();")
        lines += [f'  chk("rust->gcc", {f}, "{w}", {c}, {e});' for o, c, e, w in checks]
        lines += [f"  {c} = 0x5A5A5A5A;" for o, c, e, w in checks]
        lines.append(f"  poison(); gc{f}();")
        lines += [f'  chk("gcc->gcc(control)", {f}, "{w}", {c}, {e});' for o, c, e, w in checks]
        lines.append("}")
        body += lines
    body.append("int main(void) {")
    body += [f"  t{f}();" for f, *_ in tests]
    body.append('  puts_("DONE checks="); dec_(checks); puts_(" fails="); dec_(fails); puts_("\\n"); return 0; }')
    harness += body
    return "\n".join(harness) + "\n", "\n".join(ccaller) + "\n", "\n".join(rcallee) + "\n", "\n".join(rcaller) + "\n"

VECTORS = r"""
    .section .vectors, "ax"
    .align 4
    .global _vecbase
_vecbase:
    .org 0x00               /* WindowOverflow4 */
    s32e a0, a5, -16
    s32e a1, a5, -12
    s32e a2, a5, -8
    s32e a3, a5, -4
    rfwo
    .org 0x40               /* WindowUnderflow4 */
_WindowUnderflow4:
    l32e a0, a5, -16
    l32e a1, a5, -12
    l32e a2, a5, -8
    l32e a3, a5, -4
    rfwu
    .org 0x80               /* WindowOverflow8 */
    s32e a0, a9, -16
    l32e a0, a1, -12
    s32e a2, a9, -8
    s32e a1, a9, -12
    s32e a3, a9, -4
    s32e a4, a0, -32
    s32e a5, a0, -28
    s32e a6, a0, -24
    s32e a7, a0, -20
    rfwo
    .org 0xC0               /* WindowUnderflow8 */
_WindowUnderflow8:
    l32e a1, a9, -12
    l32e a0, a9, -16
    l32e a7, a1, -12
    l32e a2, a9, -8
    l32e a4, a7, -32
    l32e a3, a9, -4
    l32e a5, a7, -28
    l32e a6, a7, -24
    l32e a7, a7, -20
    rfwu
    .org 0x100              /* WindowOverflow12 */
    s32e a0, a13, -16
    l32e a0, a1, -12
    s32e a1, a13, -12
    s32e a2, a13, -8
    s32e a3, a13, -4
    s32e a4, a0, -48
    s32e a5, a0, -44
    s32e a6, a0, -40
    s32e a7, a0, -36
    s32e a8, a0, -32
    s32e a9, a0, -28
    s32e a10, a0, -24
    s32e a11, a0, -20
    rfwo
    .org 0x140              /* WindowUnderflow12 */
_WindowUnderflow12:
    l32e a1, a13, -12
    l32e a0, a13, -16
    l32e a11, a1, -12
    l32e a2, a13, -8
    l32e a4, a11, -48
    l32e a8, a11, -32
    l32e a3, a13, -4
    l32e a5, a11, -44
    l32e a6, a11, -40
    l32e a7, a11, -36
    l32e a9, a11, -28
    l32e a10, a11, -24
    l32e a11, a11, -20
    rfwu
    .org 0x300              /* KernelException */
    j _fatal
    .org 0x340              /* UserException: only MOVSP (alloca, cause 5) is handled */
    wsr a0, excsave1
    rsr a0, exccause
    beqi a0, 5, _alloca_exc
    j _fatal
    .org 0x3C0              /* DoubleException */
    j _fatal
    .org 0x400
    /* MOVSP found the caller's window spilled: reload it with the matching underflow
       handler (which returns to the MOVSP), as FreeRTOS's _xt_alloca_exc does. */
_alloca_exc:
    rsr a0, windowbase
    rotw -1
    rsr a2, ps
    extui a3, a2, 8, 4
    xor a3, a3, a4
    rsr a4, excsave1
    slli a3, a3, 8
    xor a2, a2, a3
    wsr a2, ps
    rsync
    bbci.l a4, 31, _WindowUnderflow4
    rotw -1
    bbci.l a8, 30, _WindowUnderflow8
    rotw -1
    j _WindowUnderflow12
    .section .text.start, "ax"
    .align 4
    .literal_position
    .literal .Lexcmsg, _excmsg
_fatal:
    movi a2, 4
    movi a3, 1
    l32r a4, .Lexcmsg
    movi a5, 10
    simcall
    movi a2, 1
    movi a3, 1
    simcall
1:  j 1b
    .section .rodata
_excmsg: .ascii "EXCEPTION\n"
"""
START = {
    "call0": """
    .section .reset, "ax"
    .global _reset
_reset:
    j _start
    .section .text.start, "ax"
    .global _start
    .align 4
    .literal_position
    .literal .Lstack, __stack_top
    .literal .Lps0, 0x2f
    .literal .Lvec, _vecbase
_start:
    l32r a1, .Lstack
    l32r a2, .Lvec
    wsr a2, vecbase
    movi a2, 1           /* enable coprocessor 0 (FPU) */
    wsr a2, cpenable
    l32r a2, .Lps0       /* PS: EXCM=0 (else zero-overhead loops never loop back), INTLEVEL=15 */
    wsr a2, ps
    rsync
    call0 main
    movi a2, 1
    movi a3, 0
    simcall
1:  j 1b
    .text
    .global sh_write
    .align 4
sh_write:            /* (a2 = buf, a3 = len) */
    mov a4, a2
    mov a5, a3
    movi a2, 4
    movi a3, 1
    simcall
    ret
""",
    "windowed": """
    .section .reset, "ax"
    .global _reset
_reset:
    j _start
    .section .text.start, "ax"
    .global _start
    .align 4
    .literal_position
    .literal .Lstack, __stack_top
    .literal .Lps, 0x4002f
    .literal .Lvec, _vecbase
_start:
    l32r a1, .Lstack
    l32r a2, .Lvec
    wsr a2, vecbase
    movi a2, 1           /* enable coprocessor 0 (FPU) */
    wsr a2, cpenable
    l32r a2, .Lps        /* PS: WOE=1, EXCM=0, INTLEVEL=15 */
    wsr a2, ps
    rsync
    call4 main
    movi a2, 1
    movi a3, 0
    simcall
1:  j 1b
    .text
    .global sh_write
    .align 4
sh_write:
    entry a1, 32
    mov a4, a2
    mov a5, a3
    movi a2, 4
    movi a3, 1
    simcall
    retw
""",
}

LIBC = """#include <stddef.h>
void *memcpy(void *d, const void *s, size_t n) { volatile char *dd = d; const volatile char *ss = s; while (n--) *dd++ = *ss++; return d; }
void *memset(void *d, int c, size_t n) { volatile char *dd = d; while (n--) *dd++ = (char)c; return d; }
void *memmove(void *d, const void *s, size_t n) { volatile char *dd = d; const volatile char *ss = s; if (dd < ss) while (n--) *dd++ = *ss++; else while (n--) dd[n] = ss[n]; return d; }
/* The toolchain's libgcc is built for the windowed ABI only; call0 images need their own. */
unsigned int __mulsi3(unsigned int a, unsigned int b) { unsigned int r = 0; while (b) { if (b & 1) r += a; a <<= 1; b >>= 1; } return r; }
"""

LDSCRIPT = """
ENTRY(_reset)
SECTIONS {
  . = 0xfe000000;
  .reset : { KEEP(*(.reset)) }
  . = 0xfe001000;
  .vectors : { KEEP(*(.vectors)) }
  .text : { *(.literal.start) KEEP(*(.text.start)) *(.literal .text .literal.* .text.*) }
  .rodata : { *(.rodata .rodata.*) }
  . = 0x00001000;
  .data : { *(.data .data.*) *(.sdata .sdata.*) }
  .bss : { *(.bss .bss.*) *(.sbss .sbss.*) *(COMMON) }
  __stack_top = 0x04000000;
}
"""

def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if p.returncode:
        sys.exit(f"FAILED ({p.returncode}): {' '.join(cmd)}\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}")
    return p

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rustc"); ap.add_argument("out")
    ap.add_argument("--abi", default="call0", choices=["call0", "windowed"])
    ap.add_argument("--n", type=int, default=150); ap.add_argument("--seed", type=int, default=278)
    ap.add_argument("--cpu", default="de233_fpu")
    ap.add_argument("--ropt", default="3", help="rustc -Copt-level")
    ap.add_argument("--real-core", action="store_true", help="use the toolchain's core instead of minicore")
    ap.add_argument("--no-odd-structs", action="store_true", help="drop signatures containing S5/S6/S7")
    ap.add_argument("--only-espnn", action="store_true", help="only the esp_nn_max_pool_s8_esp32s3 signature from esp-rs/rust#278")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    global REAL_CORE
    REAL_CORE = a.real_core
    rng = random.Random(a.seed)
    sigs = gen_sigs(rng, a.n)
    sigs = [["u32"] * 5 + ["S6", "u16"], ["u32"] * 5 + ["S5", "i8", "bool"], ["u32"] * 3 + ["i64", "u16", "u32"],
            ["u32"] * 6 + ["u16", "i16", "u8", "i8", "bool"], ["u16", "i16", "u8", "i8", "bool", "u16", "i8", "i16"],
            # esp_nn_max_pool_s8_esp32s3 from esp-rs/rust#278 (pointers as u32)
            ["u32", "u16", "u16", "u32", "u16", "u16", "u16", "u16", "u16", "u16", "u16", "u16", "i32", "i32", "u16"]] + sigs
    if a.only_espnn:
        sigs = [["u32", "u16", "u16", "u32", "u16", "u16", "u16", "u16", "u16", "u16", "u16", "u16", "i32", "i32", "u16"]]
    open(os.path.join(a.out, "libc.c"), "w").write(LIBC)
    if a.no_odd_structs:
        sigs = [x for x in sigs if not {"S5", "S6", "S7"} & set(x)]
    names = ["harness.c", "ccaller.c", "rcallee.rs", "rcaller.rs"]
    for n, t in zip(names, emit(sigs, rng)):
        open(os.path.join(a.out, n), "w").write(t)
    open(os.path.join(a.out, "start.S"), "w").write(START[a.abi] + VECTORS)
    open(os.path.join(a.out, "link.ld"), "w").write(LDSCRIPT)
    P = lambda n: os.path.join(a.out, n)
    gflags = ["-O2", "-ffreestanding", "-fno-builtin", "-fno-tree-loop-distribute-patterns", "-c"] + (["-mabi=call0"] if a.abi == "call0" else [])
    rfeat = ["-Ctarget-feature=+windowed"] if a.abi == "windowed" else []
    rflags = ["--target", TARGET, f"-Copt-level={a.ropt}", "-Cpanic=abort", "-Awarnings", "--emit", "obj"] + rfeat
    mc = P("libminicore.rlib")
    if not a.real_core: run([a.rustc, "--target", TARGET, "--crate-type", "rlib", "-Copt-level=3", "-Cpanic=abort", "-Awarnings",
         os.path.join(REPO, "tests", "auxiliary", "minicore.rs"), "-o", mc] + rfeat)
    for src in ("harness.c", "ccaller.c", "libc.c"):
        run([GCC] + gflags + [P(src), "-o", P(src + ".o")])
    run([GCC] + (["-mabi=call0"] if a.abi == "call0" else []) + ["-c", P("start.S"), "-o", P("start.o")])
    if a.real_core:
        cargo = os.path.join(os.path.dirname(a.rustc), "cargo.exe")
        env = dict(os.environ, CARGO_TARGET_DIR=config.CARGO_TARGET_DIR, RUSTC=a.rustc, RUSTFLAGS=" ".join(["-Copt-level=3", "-Awarnings"] + rfeat))
        # one crate holding both callee and caller, so core/panic handler are linked once
        crate = P("rboth_crate")
        os.makedirs(os.path.join(crate, "src"), exist_ok=True)
        open(os.path.join(crate, "Cargo.toml"), "w").write(
            '[package]\nname = "rboth"\nversion = "0.0.0"\nedition = "2021"\n'
            '[lib]\npath = "src/lib.rs"\ncrate-type = ["staticlib"]\n'
            '[profile.release]\npanic = "abort"\nopt-level = 3\n[profile.dev]\npanic = "abort"\n')
        callee = open(P("rcallee.rs")).read().split("\n")
        seen = set(callee)
        caller = [l for l in open(P("rcaller.rs")).read().split("\n") if l not in seen or l == "}"]
        code = "\n".join(callee + caller).replace('#![crate_type = "lib"]', "") + \
            "\n#[panic_handler] fn ph(_: &core::panic::PanicInfo) -> ! { loop {} }\n"
        open(os.path.join(crate, "src", "lib.rs"), "w").write(code)
        run([cargo, "build", "--release", "-Zbuild-std=core", "--target", TARGET, "--manifest-path", os.path.join(crate, "Cargo.toml")], env=env)
        import shutil
        shutil.copy(os.path.join(config.CARGO_TARGET_DIR, TARGET, "release", "librboth.a"), P("rcallee.rs.o"))
        if os.path.exists(P("rcaller.rs.o")): os.remove(P("rcaller.rs.o"))
        run([os.path.join(GCCBIN, "xtensa-esp32s3-elf-ar.exe"), "rc", P("rcaller.rs.o")])
    for src in (() if a.real_core else ("rcallee.rs", "rcaller.rs")):
        run([a.rustc] + rflags + ([] if a.real_core else ["--extern", f"minicore={mc}"]) + [P(src), "-o", P(src + ".o")])
    run([GCC, "-nostdlib", "-nostartfiles", "-T", P("link.ld"), "-Wl,--no-relax", "-Wl,--gc-sections",
         P("start.o"), P("harness.c.o"), P("ccaller.c.o"), P("rcallee.rs.o"), P("rcaller.rs.o"), P("libc.c.o"), "-o", P("test.elf")]
        + (["-mabi=call0"] if a.abi == "call0" else []) + ["-lgcc"])
    try:
        p = subprocess.run([QEMU, "-M", "sim", "-cpu", a.cpu, "-display", "none", "-monitor", "none", "-serial", "none",
                            "-semihosting", "-kernel", P("test.elf")], capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
        out = p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        out += "\nTIMEOUT (program hung)"
    open(P("qemu.out"), "w", encoding="utf-8").write(out)
    lines = out.splitlines()
    mism = [l for l in lines if l.startswith("MISMATCH")]
    done = [l for l in lines if l.startswith("DONE")]
    print(f"abi={a.abi} cpu={a.cpu} signatures={len(sigs)} -> {done[0] if done else 'NO DONE LINE: ' + out[-300:]}")
    by_path = {}
    for l in mism:
        path = l.split()[1].split("=")[1]
        by_path.setdefault(path, set()).add(l.split()[2])
    if done:
        c, fl = done[0].split()[1:3]
        print(f"  checks={int(c.split('=')[1], 16)} fails={int(fl.split('=')[1], 16)}")
    for path, fs in by_path.items():
        print(f"  {path}: {len(fs)} signatures with wrong values")
    for l in mism[:8]:
        f = int(l.split()[2][1:], 16)
        print(f"    {l}   sig=({', '.join(sigs[f])})")

if __name__ == "__main__":
    main()
