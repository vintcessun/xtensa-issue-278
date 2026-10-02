#!/usr/bin/env python3
"""Systematic Xtensa byval/stack-argument sweep, IR-driven (no rustc), run on QEMU.

Every case is a signature  [i32 x K] ++ [byval S(N,A)] ++ followers  (optionally ++ varargs)
where S is a C struct of N bytes and alignment A, placed so that GCC passes it on the stack
(only then is an LLVM `byval` a correct lowering of it).

Per case, four paths are executed on QEMU and every argument is checked:
  c->ir      GCC caller          -> llc-compiled IR callee (exports every value, struct = byte checksum)
  ir->c      llc IR caller       -> GCC "widened" callee (sub-word ints read as full words)
  irdyn->c   llc IR caller with a dynamic alloca (non-reserved call frame, ADJCALLSTACK
             really moves SP), calling 3 times in a row
  c->c       GCC caller          -> the same widened callee (control: validates the reference)
Varargs in the IR callee are read with a hand-written GCC-style va_arg (what rustc emits), so the
test isolates llvm.va_start; --ir-va-arg uses LLVM's own `va_arg` instruction instead.

usage: irsweep.py LLC CHIP OUTDIR [--opt O2] [--chunk I/N] [--ir-va-arg]
"""
import argparse, json, os, random, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qemutest as Q

GCCBIN = Q.GCCBIN
CHIPS = {
    "esp32":   dict(gcc="xtensa-esp32-elf-gcc.exe",   abi="windowed", mcpu="esp32",   fpu=True),
    "esp32s2": dict(gcc="xtensa-esp32s2-elf-gcc.exe", abi="windowed", mcpu="esp32s2", fpu=False),
    "esp32s3": dict(gcc="xtensa-esp32s3-elf-gcc.exe", abi="windowed", mcpu="esp32s3", fpu=True),
    "call0":   dict(gcc="xtensa-esp32s3-elf-gcc.exe", abi="call0",    mcpu="generic", fpu=True),
    "esp8266": dict(gcc="xtensa-esp32-elf-gcc.exe",   abi="call0",    mcpu="esp8266", fpu=True),
    # real ESP8266: Espressif's lx106 GCC as the reference and QEMU's lx106 core
    "esp8266real": dict(gcc="LX106", abi="call0", mcpu="esp8266", fpu=False, qemu_cpu="lx106"),
}
import config
LX106_GCC = config.LX106_GCC

# lx106 on QEMU "sim": reset vector 0x50000000 (sysrom0), sysram0 at 0x60000000; call0 only, no FPU,
# no windowed/loop options. Every vector slot jumps to _fatal.
LX106_START = r"""
    .section .reset, "ax"
    .global _reset
_reset:
    j _start
    .section .vectors, "ax"
    .global _vecbase
_vecbase:
""" + "".join(f"    .org 0x{o:02x}\n    j _fatal\n" for o in range(0, 0x100, 0x10)) + r"""
    .section .text.start, "ax"
    .global _start
    .align 4
    .literal_position
    .literal .Lstack, __stack_top
    .literal .Lps0, 0x2f
    .literal .Lvec, _vecbase
    .literal .Lexcmsg, _excmsg
_start:
    l32r a1, .Lstack
    l32r a2, .Lvec
    wsr a2, vecbase
    l32r a2, .Lps0
    wsr a2, ps
    rsync
    call0 main
    movi a2, 1
    movi a3, 0
    simcall
1:  j 1b
_fatal:
    movi a2, 4
    movi a3, 1
    l32r a4, .Lexcmsg
    movi a5, 10
    simcall
    movi a2, 1
    movi a3, 1
    simcall
2:  j 2b
    .text
    .global sh_write
    .align 4
sh_write:
    mov a4, a2
    mov a5, a3
    movi a2, 4
    movi a3, 1
    simcall
    ret
    .section .rodata
_excmsg: .ascii "EXCEPTION\n"
"""
LX106_LD = """
ENTRY(_reset)
SECTIONS {
  . = 0x50000000;
  .reset : { KEEP(*(.reset)) }
  . = 0x50000400;
  .vectors : { KEEP(*(.vectors)) }
  .text : { *(.literal.start) KEEP(*(.text.start)) *(.literal .text .literal.* .text.*) }
  .rodata : { *(.rodata .rodata.*) }
  . = 0x60000000;
  .data : { *(.data .data.*) *(.sdata .sdata.*) }
  .bss : { *(.bss .bss.*) *(.sbss .sbss.*) *(COMMON) }
  __stack_top = 0x64000000;
}
"""

# scalar kinds: IR type + attrs, C type, widened C type, source kind (32/64-bit), expected expr on source s
SC = {
    "u8":  ("i8 zeroext",  "uint8_t",  "uint32_t", 32, "(uint32_t)(uint8_t){s}"),
    "s8":  ("i8 signext",  "int8_t",   "uint32_t", 32, "(uint32_t)(int32_t)(int8_t){s}"),
    "u16": ("i16 zeroext", "uint16_t", "uint32_t", 32, "(uint32_t)(uint16_t){s}"),
    "s16": ("i16 signext", "int16_t",  "uint32_t", 32, "(uint32_t)(int32_t)(int16_t){s}"),
    "b":   ("i1 zeroext",  "bool",     "uint32_t", 32, "(uint32_t)({s} & 1)"),
    "i32": ("i32",         "uint32_t", "uint32_t", 32, "(uint32_t){s}"),
    "f32": ("float",       "float",    "float",    32, None),
    "ptr": ("ptr",         "void *",   "void *",   32, "(uint32_t){s}"),
    "i64": ("i64",         "uint64_t", "uint64_t", 64, None),
    "f64": ("double",      "double",   "double",   64, None),
}
VA = ["i32", "i64", "f64"]

def shapes():
    out = [(n, 1) for n in range(1, 34)] + [(n, 2) for n in range(2, 35, 2)] + [(n, 4) for n in range(4, 37, 4)]
    out += [(n, 8) for n in (8, 16, 24, 32)] + [(n, 16) for n in (16, 32)]
    return out

def on_stack(k_before, n, a):
    """GCC: does S(n,a) go on the stack after k_before one-word args?"""
    left = 6 - k_before
    words = (n + 3) // 4
    if a >= 8 and words <= left:     # 8-byte aligned: skip to even register first
        left -= (k_before % 2)
    if a == 16 and k_before != 0:
        return True
    return words > left

def cases():
    res = []
    followers = [[f, f] for f in SC] + [[("S", 6, 2), ("S", 5, 1)], [("S", 8, 4), "u16"], [("S", 3, 1), "s8"]]
    for (n, a) in shapes():
        ks = sorted({k for k in range(0, 7) if on_stack(k, n, a)} & ({min(k for k in range(0, 7) if on_stack(k, n, a)), 6}))
        for k in ks:
            pre = ["i32"] * k
            for fol in followers:
                res.append((pre + [("S", n, a)] + fol, None))
            res.append((pre + [("S", n, a)], None))                     # struct is the last argument
            res.append((pre + [("S", n, a)], ["i32", "i64", "f64", "i32"]))  # varargs right after it
            res.append((pre + [("S", n, a), "u16"], ["f64", "i32"]))
    return res

def sname(n, a): return f"S{n}_{a}"
def is_s(p): return isinstance(p, tuple)

def c_struct_defs(all_shapes):
    s = []
    for n, a in sorted(all_shapes):
        elem = {1: ("uint8_t", 1), 2: ("uint16_t", 2), 4: ("uint32_t", 4), 8: ("uint64_t", 8), 16: ("uint32_t", 4)}[a]
        attr = " __attribute__((aligned(16)))" if a == 16 else ""
        s.append(f"typedef struct{attr} {{ {elem[0]} v[{n // elem[1]}]; }} {sname(n, a)};")
    return "\n".join(s)

def rnd_src(rng, p):
    if is_s(p):
        n, a = p[1], p[2]
        return "{{" + ", ".join(str(rng.randrange(1, 255)) for _ in range(n // {1: 1, 2: 2, 4: 4, 8: 8, 16: 4}[a])) + "}}"
    bits = SC[p][3]
    if p == "f32": return hex((rng.getrandbits(1) << 31) | (rng.randint(1, 254) << 23) | rng.getrandbits(23))
    if p == "f64": return hex((rng.getrandbits(1) << 63) | (rng.randint(1, 2046) << 52) | rng.getrandbits(52)) + "ULL"
    if p == "b": return hex((rng.getrandbits(32) & ~0xff) | rng.getrandbits(1) | 0xA5000000)
    v = rng.getrandbits(bits)
    if p in ("u8", "s8", "u16", "s16"): v |= 0xA5000000
    return hex(v) + ("ULL" if bits == 64 else "")

def build(case_list, rng, ir_va_arg):
    shapes_used = {(p[1], p[2]) for c, _ in case_list for p in c if is_s(p)}
    H = [Q.C_PRELUDE, "#include <stdarg.h>", "#include <stddef.h>", c_struct_defs(shapes_used), """
void sh_write(const char *p, int n);
static void puts_(const char *s) { int n = 0; while (s[n]) n++; sh_write(s, n); }
static void hex_(uint32_t v) { char b[9]; for (int i = 0; i < 8; i++) { int d = (v >> (28 - 4 * i)) & 15; b[i] = d < 10 ? '0' + d : 'a' + d - 10; } b[8] = 0; puts_(b); }
__attribute__((noinline)) void poison(void) { volatile uint32_t buf[256]; for (int i = 0; i < 256; i++) buf[i] = 0xA5A5A5A5u; }
uint32_t csum(const void *p, int n) { const volatile unsigned char *b = p; uint32_t h = 2166136261u; for (int i = 0; i < n; i++) { h ^= b[i]; h *= 16777619u; } return h; }
static uint32_t fails, checks;
static void chk(const char *path, int f, const char *what, uint32_t got, uint32_t exp) {
    checks++;
    if (got != exp) { fails++; puts_("MISMATCH path="); puts_(path); puts_(" c"); hex_(f); puts_(" "); puts_(what); puts_(" got="); hex_(got); puts_(" exp="); hex_(exp); puts_("\\n"); }
}
volatile uint32_t dyn_n = 24;
"""]
    CC = [Q.C_PRELUDE, "#include <stdarg.h>", "#include <stddef.h>", c_struct_defs(shapes_used)]
    IR = ['target datalayout = "e-m:e-p:32:32-i8:8:32-i16:16:32-i64:64-n32"', 'target triple = "xtensa"',
          "declare void @llvm.va_start.p0(ptr)", "declare void @llvm.va_end.p0(ptr)", "@dyn_n = external global i32"]
    main = []
    for ci, (params, var) in enumerate(case_list):
        srcs = [f"s{ci}_{k}" for k in range(len(params))]
        vsrc = [f"v{ci}_{j}" for j in range(len(var or []))]
        ctype = lambda p: sname(p[1], p[2]) if is_s(p) else SC[p][1]
        wtype = lambda p: sname(p[1], p[2]) if is_s(p) else SC[p][2]
        srct = lambda p: sname(p[1], p[2]) if is_s(p) else ("uint64_t" if SC[p][3] == 64 else "uint32_t")
        for k, p in enumerate(params):
            H.append(f"{srct(p)} {srcs[k]} = {rnd_src(rng, p)};")
            CC.append(f"extern {srct(p)} {srcs[k]};")
            IR.append(f"@{srcs[k]} = external global " + ("i64" if not is_s(p) and SC[p][3] == 64 else "i32" if not is_s(p) else f"[{p[1]} x i8]"))
        for j, t in enumerate(var or []):
            H.append(f"{'uint32_t' if t == 'i32' else 'uint64_t'} {vsrc[j]} = {rnd_src(rng, {'i32': 'i32', 'i64': 'i64', 'f64': 'f64'}[t])};")
            CC.append(f"extern {'uint32_t' if t == 'i32' else 'uint64_t'} {vsrc[j]};")
            IR.append(f"@{vsrc[j]} = external global " + ("i32" if t == "i32" else "i64"))
        # expected words: (field name, expected C expr)
        exp = []
        for k, p in enumerate(params):
            s = srcs[k]
            if is_s(p): exp.append((f"p{k}", f"csum(&{s}, {p[1]})"))
            elif SC[p][3] == 64: exp += [(f"p{k}lo", f"(uint32_t){s}"), (f"p{k}hi", f"(uint32_t)({s} >> 32)")]
            elif p == "f32": exp.append((f"p{k}", f"(uint32_t){s}"))
            else: exp.append((f"p{k}", SC[p][4].format(s=s)))
        for j, t in enumerate(var or []):
            if t == "i32": exp.append((f"va{j}", f"(uint32_t){vsrc[j]}"))
            else: exp += [(f"va{j}lo", f"(uint32_t){vsrc[j]}"), (f"va{j}hi", f"(uint32_t)({vsrc[j]} >> 32)")]
        for w, _ in exp:
            H.append(f"uint32_t o{ci}_{w}; uint32_t c{ci}_{w};")
            IR.append(f"@o{ci}_{w} = external global i32")
        # ---- widened GCC callee
        wparams = ", ".join(f"{wtype(p)} p{k}" for k, p in enumerate(params)) + (", ..." if var else "")
        body = []
        for k, p in enumerate(params):
            if is_s(p): body.append(f"c{ci}_p{k} = csum(&p{k}, {p[1]});")
            elif SC[p][3] == 64:
                body.append(f"{{ union {{ {SC[p][1]} x; uint64_t u; }} t = {{ .x = p{k} }}; c{ci}_p{k}lo = (uint32_t)t.u; c{ci}_p{k}hi = (uint32_t)(t.u >> 32); }}")
            elif p == "f32": body.append(f"c{ci}_p{k} = ((UF){{.f = p{k}}}).u;")
            elif p == "ptr": body.append(f"c{ci}_p{k} = (uint32_t)(size_t)p{k};")
            else: body.append(f"c{ci}_p{k} = p{k};")
        if var:
            body.append(f"va_list ap; va_start(ap, p{len(params) - 1});")
            for j, t in enumerate(var):
                if t == "i32": body.append(f"c{ci}_va{j} = (uint32_t)va_arg(ap, int32_t);")
                elif t == "i64": body.append(f"{{ uint64_t t = va_arg(ap, uint64_t); c{ci}_va{j}lo = (uint32_t)t; c{ci}_va{j}hi = (uint32_t)(t >> 32); }}")
                else: body.append(f"{{ UD t = {{ .d = va_arg(ap, double) }}; c{ci}_va{j}lo = (uint32_t)t.u; c{ci}_va{j}hi = (uint32_t)(t.u >> 32); }}")
            body.append("va_end(ap);")
        H.append(f"void w{ci}({wparams}) {{ {' '.join(body)} }}")
        # ---- C callers (real prototype)
        rparams = ", ".join(f"{ctype(p)}" for p in params) + (", ..." if var else "")
        def c_arg(p, s):
            if is_s(p): return s
            if p == "f32": return f"((UF){{.u = {s}}}).f"
            if p == "f64": return f"((UD){{.u = {s}}}).d"
            if p == "ptr": return f"(void *)(size_t){s}"
            if p == "b": return f"(bool)({s} & 1)"
            return f"({SC[p][1]}){s}"
        c_va = [(f"(int32_t){vsrc[j]}" if t == "i32" else f"(int64_t){vsrc[j]}" if t == "i64" else f"((UD){{.u = {vsrc[j]}}}).d") for j, t in enumerate(var or [])]
        cargs = ", ".join([c_arg(p, srcs[k]) for k, p in enumerate(params)] + c_va)
        CC.append(f"void w{ci}({rparams}); void ircallee{ci}({rparams});")
        CC.append(f"void gcir{ci}(void) {{ ircallee{ci}({cargs}); }}")
        CC.append(f"void gcc{ci}(void) {{ w{ci}({cargs}); }}")
        H.append(f"void gcir{ci}(void); void gcc{ci}(void); void ircaller{ci}(void); void irdyn{ci}(void);")
        # ---- IR callee
        irp = []
        for k, p in enumerate(params):
            irp.append(f"ptr byval([{p[1]} x i8]) align {p[2]} %p{k}" if is_s(p) else f"{SC[p][0]} %p{k}")
        L = [f"define void @ircallee{ci}({', '.join(irp)}{', ...' if var else ''}) {{"]
        tmp = [0]
        def t():
            tmp[0] += 1
            return f"%t{tmp[0]}"
        def st32(v, g): L.append(f"  store volatile i32 {v}, ptr @{g}")
        for k, p in enumerate(params):
            if is_s(p):
                h = "2166136261"
                for i in range(p[1]):
                    a, b, x, m, z = t(), t(), t(), t(), t()
                    L.append(f"  {a} = getelementptr i8, ptr %p{k}, i32 {i}")
                    L.append(f"  {b} = load i8, ptr {a}")
                    L.append(f"  {z} = zext i8 {b} to i32")
                    L.append(f"  {x} = xor i32 {h}, {z}")
                    L.append(f"  {m} = mul i32 {x}, 16777619")
                    h = m
                st32(h, f"o{ci}_p{k}")
            else:
                ty = SC[p][0].split()[0]
                if p in ("u8", "u16", "b"): v = t(); L.append(f"  {v} = zext {ty} %p{k} to i32"); st32(v, f"o{ci}_p{k}")
                elif p in ("s8", "s16"): v = t(); L.append(f"  {v} = sext {ty} %p{k} to i32"); st32(v, f"o{ci}_p{k}")
                elif p == "i32": st32(f"%p{k}", f"o{ci}_p{k}")
                elif p == "f32": v = t(); L.append(f"  {v} = bitcast float %p{k} to i32"); st32(v, f"o{ci}_p{k}")
                elif p == "ptr": v = t(); L.append(f"  {v} = ptrtoint ptr %p{k} to i32"); st32(v, f"o{ci}_p{k}")
                else:
                    w = f"%p{k}"
                    if p == "f64": w = t(); L.append(f"  {w} = bitcast double %p{k} to i64")
                    lo, sh, hi = t(), t(), t()
                    L += [f"  {lo} = trunc i64 {w} to i32", f"  {sh} = lshr i64 {w}, 32", f"  {hi} = trunc i64 {sh} to i32"]
                    st32(lo, f"o{ci}_p{k}lo"); st32(hi, f"o{ci}_p{k}hi")
        if var:
            L.append("  %ap = alloca [12 x i8], align 4")
            L.append("  call void @llvm.va_start.p0(ptr %ap)")
            for j, ty in enumerate(var):
                llty = {"i32": "i32", "i64": "i64", "f64": "double"}[ty]
                if ir_va_arg:
                    v = t(); L.append(f"  {v} = va_arg ptr %ap, {llty}")
                else:
                    size, align = (4, 4) if ty == "i32" else (8, 8)
                    pndx, pstk, preg = t(), t(), t()
                    L += [f"  {preg} = getelementptr i8, ptr %ap, i32 4", f"  {pndx} = getelementptr i8, ptr %ap, i32 8"]
                    ndx, stk, reg = t(), t(), t()
                    L += [f"  {ndx} = load i32, ptr {pndx}", f"  {stk} = load ptr, ptr %ap", f"  {reg} = load ptr, ptr {preg}"]
                    if align == 8:
                        a1, a2 = t(), t(); L += [f"  {a1} = add i32 {ndx}, 7", f"  {a2} = and i32 {a1}, -8"]; ndx = a2
                    new, inreg, mx, stknew, fin, base, off, addr, v = t(), t(), t(), t(), t(), t(), t(), t(), t()
                    L += [f"  {new} = add i32 {ndx}, {size}",
                          f"  {inreg} = icmp ule i32 {new}, 24",
                          f"  {mx} = call i32 @llvm.umax.i32(i32 {ndx}, i32 32)",
                          f"  {stknew} = add i32 {mx}, {size}",
                          f"  {fin} = select i1 {inreg}, i32 {new}, i32 {stknew}",
                          f"  store i32 {fin}, ptr {pndx}",
                          f"  {base} = select i1 {inreg}, ptr {reg}, ptr {stk}",
                          f"  {off} = sub i32 {fin}, {size}",
                          f"  {addr} = getelementptr i8, ptr {base}, i32 {off}",
                          f"  {v} = load {llty}, ptr {addr}, align 4"]
                if ty == "i32": st32(v, f"o{ci}_va{j}")
                else:
                    w = v
                    if ty == "f64": w = t(); L.append(f"  {w} = bitcast double {v} to i64")
                    lo, sh, hi = t(), t(), t()
                    L += [f"  {lo} = trunc i64 {w} to i32", f"  {sh} = lshr i64 {w}, 32", f"  {hi} = trunc i64 {sh} to i32"]
                    st32(lo, f"o{ci}_va{j}lo"); st32(hi, f"o{ci}_va{j}hi")
            L.append("  call void @llvm.va_end.p0(ptr %ap)")
        L += ["  ret void", "}"]
        IR += L
        # ---- IR callers (plain and with a dynamic alloca)
        decl = ", ".join(f"ptr byval([{p[1]} x i8]) align {p[2]}" if is_s(p) else SC[p][0] for p in params)
        IR.append(f"declare void @w{ci}({decl}{', ...' if var else ''})")
        for kind in ("ircaller", "irdyn"):
            L = [f"define void @{kind}{ci}() {{"]
            tmp[0] = 1000
            args = []
            for k, p in enumerate(params):
                if is_s(p):
                    args.append(f"ptr byval([{p[1]} x i8]) align {p[2]} @{srcs[k]}"); continue
                ty, bits = SC[p][0].split()[0], SC[p][3]
                raw = t(); L.append(f"  {raw} = load volatile i{bits}, ptr @{srcs[k]}")
                if p in ("u8", "s8", "u16", "s16", "b"): v = t(); L.append(f"  {v} = trunc i32 {raw} to {ty}")
                elif p == "f32": v = t(); L.append(f"  {v} = bitcast i32 {raw} to float")
                elif p == "f64": v = t(); L.append(f"  {v} = bitcast i64 {raw} to double")
                elif p == "ptr": v = t(); L.append(f"  {v} = inttoptr i32 {raw} to ptr")
                else: v = raw
                args.append(f"{SC[p][0]} {v}")
            for j, ty in enumerate(var or []):
                raw = t(); L.append(f"  {raw} = load volatile {'i32' if ty == 'i32' else 'i64'}, ptr @{vsrc[j]}")
                if ty == "f64": v = t(); L.append(f"  {v} = bitcast i64 {raw} to double"); args.append(f"double {v}")
                else: args.append(f"{'i32' if ty == 'i32' else 'i64'} {raw}")
            plain = ", ".join("ptr" if is_s(p) else SC[p][0].split()[0] for p in params)
            callee_ty = f"void ({plain}{', ...' if var else ''})"
            if kind == "irdyn":
                n = t(); L += [f"  {n} = load volatile i32, ptr @dyn_n", f"  %dyn = alloca i8, i32 {n}, align 4", "  store volatile i8 1, ptr %dyn"]
                reps = 3
            else:
                reps = 1
            for _ in range(reps):
                L.append(f"  call {callee_ty} @w{ci}({', '.join(args)})")
            if kind == "irdyn":
                L.append("  store volatile i8 2, ptr %dyn")
            L += ["  ret void", "}"]
            IR += L
        # ---- test driver
        m = [f"static void t{ci}(void) {{", f"  poison(); gcir{ci}();"]
        m += [f'  chk("c->ir", {ci}, "{w}", o{ci}_{w}, {e});' for w, e in exp]
        for path, call in (("ir->c", f"ircaller{ci}()"), ("irdyn->c", f"irdyn{ci}()"), ("c->c", f"gcc{ci}()")):
            m += [f"  c{ci}_{w} = 0x5A5A5A5Au;" for w, _ in exp]
            m.append(f"  poison(); {call};")
            m += [f'  chk("{path}", {ci}, "{w}", c{ci}_{w}, {e});' for w, e in exp]
        m.append("}")
        H += m
        main.append(f"  t{ci}();")
    IR.append("declare i32 @llvm.umax.i32(i32, i32)")
    H.append("int main(void) {\n" + "\n".join(main) + '\n  puts_("DONE checks="); hex_(checks); puts_(" fails="); hex_(fails); puts_("\\n"); return 0; }')
    return "\n".join(H) + "\n", "\n".join(CC) + "\n", "\n".join(IR) + "\n"

def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if p.returncode:
        # keep the head of stderr too: that is where LLVM prints "LLVM ERROR: ..." before the backtrace
        raise RuntimeError(f"FAILED ({p.returncode}): {' '.join(map(str, cmd))}\n{p.stdout[-1000:]}\n"
                           f"{p.stderr[:800]}\n...\n{p.stderr[-800:]}")
    return p

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("llc"); ap.add_argument("chip", choices=list(CHIPS)); ap.add_argument("out")
    ap.add_argument("--opt", default="O2"); ap.add_argument("--chunk", default="0/1"); ap.add_argument("--seed", type=int, default=278)
    ap.add_argument("--ir-va-arg", action="store_true"); ap.add_argument("--list", action="store_true")
    ap.add_argument("--cases", help="file with global case indices to run instead of a chunk")
    a = ap.parse_args()
    allc = cases()
    if a.list:
        print(len(allc)); return
    ci, cn = map(int, a.chunk.split("/"))
    sel = allc[ci::cn]
    if a.cases:
        # explicit global case indices; reported indices are then positions in this list
        idx = [int(x) for x in open(a.cases).read().split()]
        sel = [allc[i] for i in idx]
    ch = CHIPS[a.chip]
    os.makedirs(a.out, exist_ok=True)
    P = lambda n: os.path.join(a.out, n)
    rng = random.Random(a.seed * 1000 + ci)
    h, c, ir = build(sel, rng, a.ir_va_arg)
    real8266 = ch["gcc"] == "LX106"
    for n, tx in (("harness.c", h), ("ccaller.c", c), ("ir.ll", ir), ("libc.c", Q.LIBC), ("link.ld", LX106_LD if real8266 else Q.LDSCRIPT)):
        open(P(n), "w", newline="\n").write(tx)
    start = Q.START[ch["abi"]] + Q.VECTORS
    if not ch["fpu"]:
        start = start.replace("    movi a2, 1           /* enable coprocessor 0 (FPU) */\n    wsr a2, cpenable\n", "")
    if real8266:
        start = LX106_START
    open(P("start.S"), "w").write(start)
    gcc = LX106_GCC if real8266 else os.path.join(GCCBIN, ch["gcc"])
    gabi = ["-mabi=call0"] if ch["abi"] == "call0" and not real8266 else []
    res = dict(chip=a.chip, opt=a.opt, chunk=a.chunk, llc=a.llc, cases=len(sel), ir_va_arg=a.ir_va_arg)
    try:
        for src in ("harness.c", "ccaller.c", "libc.c"):
            run([gcc, "-O2", "-ffreestanding", "-fno-builtin", "-fno-tree-loop-distribute-patterns", "-mtext-section-literals", "-mlongcalls", "-c"]
                + gabi + [P(src), "-o", P(src + ".o")])
        run([gcc] + gabi + ["-c", P("start.S"), "-o", P("start.o")])
        llc = [a.llc, f"-{a.opt}", f"-mcpu={ch['mcpu']}", P("ir.ll")]
        run(llc + ["-filetype=obj", "-o", P("ir.o")])
        asm = run(llc + ["-o", "-"]).stdout
        res["abi_ok"] = (re.search(r"^\s+entry\s+a1,", asm, re.M) is not None) == (ch["abi"] == "windowed")
        v = subprocess.run(llc + ["-verify-machineinstrs", "-o", os.devnull], capture_output=True, text=True, encoding="utf-8", errors="replace")
        res["verifier_bad"] = sorted(set(re.findall(r"- function:\s+(\S+)", v.stderr)))
        res["verifier_exit"] = v.returncode
        run([gcc, "-nostdlib", "-nostartfiles", "-T", P("link.ld"), "-Wl,--no-relax", "-Wl,--gc-sections",
             P("start.o"), P("harness.c.o"), P("ccaller.c.o"), P("ir.o"), P("libc.c.o"), "-o", P("test.elf")] + gabi + ["-lgcc"])
        try:
            p = subprocess.run([Q.QEMU, "-M", "sim", "-cpu", ch.get("qemu_cpu", "de233_fpu"), "-display", "none", "-monitor", "none", "-serial", "none",
                                "-semihosting", "-kernel", P("test.elf")], capture_output=True, text=True, timeout=300,
                               encoding="utf-8", errors="replace")
            out = p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            out = "TIMEOUT"
        open(P("qemu.out"), "w", encoding="utf-8").write(out)
        done = [l for l in out.splitlines() if l.startswith("DONE")]
        res["done"] = bool(done); res["exception"] = "EXCEPTION" in out; res["timeout"] = out == "TIMEOUT"
        if done:
            f = done[0].split()
            res["checks"] = int(f[1].split("=")[1], 16); res["fails"] = int(f[2].split("=")[1], 16)
        else:
            res["last_case_started"] = None
        mism = [l for l in out.splitlines() if l.startswith("MISMATCH")]
        per = {}
        for l in mism:
            pp = l.split(); per.setdefault(pp[1].split("=")[1], set()).add(int(pp[2][1:], 16))
        res["fail_cases"] = {k: sorted(v) for k, v in per.items()}
        res["case_sigs"] = {str(i): repr(sel[i]) for vs in per.values() for i in vs}
    except Exception as e:
        res["error"] = str(e)[:3000]
    print(json.dumps(res))

if __name__ == "__main__":
    main()
