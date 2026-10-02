#!/usr/bin/env python3
"""Extended systematic sweep (IR-driven, QEMU), second generation.

Adds to irsweep.py:
  * every stack position of a byval aggregate, reached through prefixes that follow
    GCC's register assignment (8-byte alignment skips, small ints, f32, args already on the stack)
  * aggregate contents: inner/tail padding, float/double members, packed, nested, unions,
    over-aligned small structs, sizes up to 255 bytes (padding bytes are not checked)
  * sret returns (hidden pointer in a2) combined with byval arguments
  * several odd-size aggregates in a row
  * for every case containing an aggregate whose size is not a multiple of 4, a "twin"
    in which each such aggregate is replaced by a plain word-multiple aggregate of the
    rounded size; with a correct fix both place every other argument identically.

Paths per case: c->ir, ir->c, irdyn->c (dynamic alloca caller, 3 calls), c->c (control).

usage: irsweep2.py LLC CHIP OUTDIR [--opt O2] [--chunk I/N] [--cases FILE] [--list] [--list-json]
"""
import argparse, json, os, random, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import qemutest as Q
import irsweep as W   # CHIPS, SC, LX106_* , run()

SC = W.SC

# --------------------------------------------------------------------------- aggregate shapes
class Shape:
    """name, C definition, size, align, mask (bytes that carry data)"""
    def __init__(self, name, cdef, size, align, mask, checks=()):
        self.name, self.cdef, self.size, self.align, self.mask, self.checks = name, cdef, size, align, sorted(mask), checks
    @property
    def odd(self): return self.size % 4 != 0

def arr_shape(n, a):
    elem = {1: ("uint8_t", 1), 2: ("uint16_t", 2), 4: ("uint32_t", 4), 8: ("uint64_t", 8), 16: ("uint32_t", 4)}[a]
    attr = " __attribute__((aligned(16)))" if a == 16 else ""
    return Shape(f"A{n}_{a}", f"typedef struct{attr} {{ {elem[0]} v[{n // elem[1]}]; }} A{n}_{a};", n, a, range(n))

def composite_shapes():
    S = []
    def add(name, body, size, align, mask, offsets=()):
        checks = [f"_Static_assert(sizeof({name}) == {size}, \"size\");", f"_Static_assert(_Alignof({name}) == {align}, \"align\");"]
        checks += [f"_Static_assert(offsetof({name}, {f}) == {o}, \"off\");" for f, o in offsets]
        S.append(Shape(name, f"typedef {body} {name};", size, align, mask, checks))
    add("C_u8_u16", "struct { uint8_t a; uint16_t b; }", 4, 2, [0, 2, 3], [("b", 2)])
    add("C_u16_u8", "struct { uint16_t a; uint8_t b; }", 4, 2, [0, 1, 2], [("b", 2)])
    add("C_u8_u32", "struct { uint8_t a; uint32_t b; }", 8, 4, [0, 4, 5, 6, 7], [("b", 4)])
    add("C_u8_f64", "struct { uint8_t a; double d; }", 16, 8, [0] + list(range(8, 16)), [("d", 8)])
    add("C_f64_u8", "struct { double d; uint8_t c; }", 16, 8, list(range(0, 9)), [("c", 8)])
    add("C_f32_u16", "struct { float f; uint16_t h; }", 8, 4, list(range(0, 6)), [("h", 4)])
    add("C_f32x3_u8", "struct { float f[3]; uint8_t c; }", 16, 4, list(range(0, 13)), [("c", 12)])
    add("C_u64_u8x3", "struct { uint64_t a; uint8_t b[3]; }", 16, 8, list(range(0, 11)), [("b", 8)])
    add("C_u8x5_u16", "struct { uint8_t a[5]; uint16_t h; }", 8, 2, [0, 1, 2, 3, 4, 6, 7], [("h", 6)])
    add("C_u16x3_u8", "struct { uint16_t a[3]; uint8_t b; }", 8, 2, list(range(0, 7)), [("b", 6)])
    add("C_nest3_u16", "struct { struct { uint8_t x[3]; } i; uint16_t h; }", 6, 2, [0, 1, 2, 4, 5], [("h", 4)])
    add("C_nest_f32_u8", "struct { struct { float f; uint8_t c; } i; uint8_t d; }", 12, 4, [0, 1, 2, 3, 4, 8], [("d", 8)])
    add("C_union_f64_u8x3", "union { double d; uint8_t b[3]; }", 8, 8, range(8))
    add("C_union_u32_u8x7", "union { uint32_t w; uint8_t b[7]; }", 8, 4, range(4))
    add("C_al8_u8x3", "struct __attribute__((aligned(8))) { uint8_t b[3]; }", 8, 8, range(3))
    # packed: size not a multiple of 4 -> these are the interesting ones
    add("P_u8_u32_u16", "struct __attribute__((packed)) { uint8_t a; uint32_t b; uint16_t c; }", 7, 1, range(7), [("b", 1), ("c", 5)])
    add("P_u16_f64", "struct __attribute__((packed)) { uint16_t a; double d; }", 10, 1, range(10), [("d", 2)])
    add("P_u8_u16", "struct __attribute__((packed)) { uint8_t a; uint16_t b; }", 3, 1, range(3), [("b", 1)])
    add("P_f64_u8", "struct __attribute__((packed)) { double d; uint8_t c; }", 9, 1, range(9), [("c", 8)])
    add("P_al2_u8_u32", "struct __attribute__((packed, aligned(2))) { uint8_t a; uint32_t b; }", 6, 2, range(5), [("b", 1)])
    add("P_f32_u8x2", "struct __attribute__((packed)) { float f; uint8_t c[2]; }", 6, 1, range(6), [("c", 4)])
    add("P_u32x3_u8", "struct __attribute__((packed)) { uint32_t w[3]; uint8_t c; }", 13, 1, range(13), [("c", 12)])
    return S

def array_shapes():
    ns1 = list(range(1, 14)) + [15, 17, 22, 23, 25, 31, 33, 41, 47, 63, 64, 65, 99, 127, 128, 255]
    ns2 = [2, 4, 6, 10, 14, 18, 26, 34, 50, 126, 254]
    ns4 = [4, 8, 12, 20, 24, 28, 36, 44, 68, 252]
    ns8 = [8, 16, 24, 72]
    ns16 = [16, 32]
    return [arr_shape(n, 1) for n in ns1] + [arr_shape(n, 2) for n in ns2] + [arr_shape(n, 4) for n in ns4] + \
           [arr_shape(n, 8) for n in ns8] + [arr_shape(n, 16) for n in ns16]

SHAPES = {s.name: s for s in array_shapes() + composite_shapes()}
# twin_shape() adds word-multiple shapes to SHAPES; the case list must not depend on how many
# twins were built before, so it is generated from the original shapes only
BASE_SHAPES = dict(SHAPES)
def twin_shape(s):
    n = (s.size + 3) // 4 * 4
    t = arr_shape(n, 4)
    SHAPES.setdefault(t.name, t)
    return t

# --------------------------------------------------------------------------- GCC argument placement
def gcc_words(p):
    if isinstance(p, str) and p.startswith("#"):
        s = SHAPES[p[1:]]; return (s.size + 3) // 4, max(1, min(s.align, 16) // 4)
    if p in ("i64", "f64"): return 2, 2
    return 1, 1

def gcc_place(params, sret=False):
    """list of 'reg'/'stack' for each param, following xtensa_function_arg_{1,advance}"""
    aw, out = (1 if sret else 0), []
    for p in params:
        words, align = gcc_words(p)
        a = (aw + align - 1) // align * align if align > 1 else aw
        if aw < 6 and a + words > 6:
            out.append("stack"); aw = 6 + words      # arg_words = max, then += words
        elif aw >= 6:
            out.append("stack"); aw += words
        else:
            out.append("reg"); aw = a + words
    return out

# --------------------------------------------------------------------------- cases
PREFIXES = [[], ["i32"], ["i32"] * 2, ["i32"] * 3, ["i32"] * 4, ["i32"] * 5, ["i32"] * 6,
            ["i64"], ["i32", "i64"], ["i64", "i64"], ["i32", "i64", "i32"], ["f64", "f32"], ["i64", "i32", "i32", "i32", "i32"],
            ["u16", "u8", "s16"], ["i32", "i32", "i32", "i64"], ["i32", "i32", "i32", "i32", "i32", "i64"],
            ["i32"] * 6 + ["u16"], ["i32"] * 6 + ["i64"], ["i32"] * 6 + ["f64", "u8"], ["i32"] * 6 + ["i32", "i64"]]
FOLLOW_FULL = [[f, f] for f in SC] + [["#P_u8_u32_u16", "u8"], ["#A8_4", "u16"], ["#A3_1", "s8"]]
FOLLOW_SHORT = [["u8"], ["s16", "b"], ["i64"], ["f64", "u16"], ["#A5_1", "u16"], []]
VARARGS = [["i32", "i64", "f64", "i32"], ["f64", "i32"]]

def struct_on_stack(prefix, sname, sret=False):
    return gcc_place(prefix + ["#" + sname], sret)[-1] == "stack"

def base_cases():
    cases = []
    arr = [s for s in BASE_SHAPES.values() if s.name.startswith("A")]
    comp = [s for s in BASE_SHAPES.values() if not s.name.startswith("A")]
    # G1: every position (all prefixes that put the aggregate on the stack), short follower set
    for s in arr + comp:
        for pre in PREFIXES:
            if not struct_on_stack(pre, s.name):
                continue
            for fol in FOLLOW_SHORT:
                cases.append(dict(ret=None, params=pre + ["#" + s.name] + fol, var=None))
            for va in VARARGS:
                cases.append(dict(ret=None, params=pre + ["#" + s.name], var=va))
    # G2: composites with the full follower set at the canonical position
    for s in comp:
        for fol in FOLLOW_FULL:
            cases.append(dict(ret=None, params=["i32"] * 6 + ["#" + s.name] + fol, var=None))
    # G3: sret (hidden pointer in a2) + aggregates
    for r in ("A20_4", "A33_1", "A64_1"):
        for sn in ("A5_1", "A6_2", "A7_1", "A8_4", "A33_1", "P_u8_u32_u16", "C_nest3_u16", "P_f64_u8"):
            for pre in ([], ["i32"] * 4, ["i32"] * 5, ["i32", "i64"]):
                if not struct_on_stack(pre, sn, sret=True):
                    continue
                for fol, va in ((["u16"], None), (["s8", "f64"], None), ([], None), ([], ["i32", "f64"])):
                    cases.append(dict(ret=r, params=pre + ["#" + sn] + fol, var=va))
    # G4: several odd aggregates in a row
    seqs = [["A5_1", "A6_2", "A7_1"], ["A6_2", "u16", "A5_1", "s8", "A7_1", "i64"], ["A3_1", "A5_1", "A1_1", "A9_1"],
            ["A7_1"] * 4, ["P_u8_u32_u16", "C_nest3_u16", "P_f64_u8", "u8"], ["A33_1", "A2_2", "A65_1", "u16"],
            ["A10_2", "f64", "A13_1", "i32"], ["P_u16_f64", "P_al2_u8_u32", "P_u32x3_u8", "b"]]
    for seq in seqs:
        ps = [("#" + x if x in BASE_SHAPES else x) for x in seq]
        for pre in (["i32"] * 6, ["i32"] * 5, ["i64", "i64", "i64"]):
            if gcc_place(pre + ps)[len(pre)] != "stack":
                continue
            cases.append(dict(ret=None, params=pre + ps + ["u16"], var=None))
            cases.append(dict(ret=None, params=pre + ps, var=["i32", "f64"]))
    return cases

def twin_case(c):
    params = [("#" + twin_shape(SHAPES[p[1:]]).name if isinstance(p, str) and p.startswith("#") and SHAPES[p[1:]].odd else p) for p in c["params"]]
    ret = c["ret"]
    return dict(ret=ret, params=params, var=c["var"])

def is_odd_case(c):
    return any(isinstance(p, str) and p.startswith("#") and SHAPES[p[1:]].odd and SHAPES[p[1:]].size > 4 for p in c["params"])

def all_cases():
    base = base_cases()
    out, seen = [], set()
    def add(c):
        k = json.dumps(c, sort_keys=True)
        if k not in seen:
            seen.add(k); out.append(c)
    for c in base:
        add(c)
    for c in base:
        if any(isinstance(p, str) and p.startswith("#") and SHAPES[p[1:]].odd for p in c["params"]):
            add(twin_case(c))
    return out

# --------------------------------------------------------------------------- code generation
FNV0, FNVP = 2166136261, 16777619

def build(cases, rng):
    shapes = sorted({p[1:] for c in cases for p in c["params"] if isinstance(p, str) and p.startswith("#")} |
                    {c["ret"] for c in cases if c["ret"]})
    sdefs = "\n".join(SHAPES[s].cdef for s in shapes) + "\n" + "\n".join(x for s in shapes for x in SHAPES[s].checks)
    masks = "\n".join(f"static const unsigned char mask_{s}[] = {{{', '.join(map(str, SHAPES[s].mask))}}};" for s in shapes)
    H = [Q.C_PRELUDE, "#include <stdarg.h>", "#include <stddef.h>", sdefs, masks, """
void sh_write(const char *p, int n);
static void puts_(const char *s) { int n = 0; while (s[n]) n++; sh_write(s, n); }
static void hex_(uint32_t v) { char b[9]; for (int i = 0; i < 8; i++) { int d = (v >> (28 - 4 * i)) & 15; b[i] = d < 10 ? '0' + d : 'a' + d - 10; } b[8] = 0; puts_(b); }
__attribute__((noinline)) void poison(void) { volatile uint32_t buf[256]; for (int i = 0; i < 256; i++) buf[i] = 0xA5A5A5A5u; }
uint32_t csum_m(const void *p, const unsigned char *m, int n) { const volatile unsigned char *b = p; uint32_t h = 2166136261u; for (int i = 0; i < n; i++) { h ^= b[m[i]]; h *= 16777619u; } return h; }
static uint32_t fails, checks;
static void chk(const char *path, int f, const char *what, uint32_t got, uint32_t exp) {
    checks++;
    if (got != exp) { fails++; puts_("MISMATCH path="); puts_(path); puts_(" c"); hex_(f); puts_(" "); puts_(what); puts_(" got="); hex_(got); puts_(" exp="); hex_(exp); puts_("\\n"); }
}
volatile uint32_t dyn_n = 24;
"""]
    CC = [Q.C_PRELUDE, "#include <stdarg.h>", "#include <stddef.h>", sdefs]
    IR = ['target datalayout = "e-m:e-p:32:32-i8:8:32-i16:16:32-i64:64-n32"', 'target triple = "xtensa"',
          "declare void @llvm.va_start.p0(ptr)", "declare void @llvm.va_end.p0(ptr)", "declare i32 @llvm.umax.i32(i32, i32)",
          "@dyn_n = external global i32"]
    main = []
    ctr = [0]
    def t():
        ctr[0] += 1
        return f"%t{ctr[0]}"
    def ir_csum(L, ptr, s):
        h = str(FNV0)
        for i in SHAPES[s].mask:
            a, b, z, x, m = t(), t(), t(), t(), t()
            L += [f"  {a} = getelementptr i8, ptr {ptr}, i32 {i}", f"  {b} = load i8, ptr {a}", f"  {z} = zext i8 {b} to i32",
                  f"  {x} = xor i32 {h}, {z}", f"  {m} = mul i32 {x}, {FNVP}"]
            h = m
        return h
    for ci, c in enumerate(cases):
        params, var, ret = c["params"], c["var"] or [], c["ret"]
        isS = lambda p: isinstance(p, str) and p.startswith("#")
        sn = lambda p: p[1:]
        srcs = [f"s{ci}_{k}" for k in range(len(params))]
        vsrc = [f"v{ci}_{j}" for j in range(len(var))]
        rsrc = f"r{ci}"
        # ---- sources (unions let us initialise every byte, padding included)
        for k, p in enumerate(params):
            if isS(p):
                s = SHAPES[sn(p)]
                H.append(f"union {{ {s.name} t; unsigned char raw[{s.size}]; }} {srcs[k]}_u = {{ .raw = {{{', '.join(str(rng.randrange(1, 255)) for _ in range(s.size))}}} }};")
                H.append(f"#define {srcs[k]} ({srcs[k]}_u.t)")
                CC.append(f"extern union {{ {s.name} t; unsigned char raw[{s.size}]; }} {srcs[k]}_u;\n#define {srcs[k]} ({srcs[k]}_u.t)")
                IR.append(f"@{srcs[k]}_u = external global [{s.size} x i8]")
            else:
                bits = SC[p][3]
                H.append(f"{'uint64_t' if bits == 64 else 'uint32_t'} {srcs[k]} = {W.rnd_src(rng, p)};")
                CC.append(f"extern {'uint64_t' if bits == 64 else 'uint32_t'} {srcs[k]};")
                IR.append(f"@{srcs[k]} = external global i{bits}")
        for j, ty in enumerate(var):
            H.append(f"{'uint32_t' if ty == 'i32' else 'uint64_t'} {vsrc[j]} = {W.rnd_src(rng, ty)};")
            CC.append(f"extern {'uint32_t' if ty == 'i32' else 'uint64_t'} {vsrc[j]};")
            IR.append(f"@{vsrc[j]} = external global {'i32' if ty == 'i32' else 'i64'}")
        if ret:
            s = SHAPES[ret]
            H.append(f"union {{ {s.name} t; unsigned char raw[{s.size}]; }} {rsrc}_u = {{ .raw = {{{', '.join(str(rng.randrange(1, 255)) for _ in range(s.size))}}} }};")
            H.append(f"#define {rsrc} ({rsrc}_u.t)")
            CC.append(f"extern union {{ {s.name} t; unsigned char raw[{s.size}]; }} {rsrc}_u;\n#define {rsrc} ({rsrc}_u.t)")
            IR.append(f"@{rsrc}_u = external global [{s.size} x i8]")
        # ---- expected words
        exp = []
        for k, p in enumerate(params):
            if isS(p): exp.append((f"p{k}", f"csum_m(&{srcs[k]}, mask_{sn(p)}, sizeof(mask_{sn(p)}))"))
            elif SC[p][3] == 64: exp += [(f"p{k}lo", f"(uint32_t){srcs[k]}"), (f"p{k}hi", f"(uint32_t)({srcs[k]} >> 32)")]
            elif p == "f32": exp.append((f"p{k}", f"(uint32_t){srcs[k]}"))
            else: exp.append((f"p{k}", SC[p][4].format(s=srcs[k])))
        for j, ty in enumerate(var):
            if ty == "i32": exp.append((f"va{j}", f"(uint32_t){vsrc[j]}"))
            else: exp += [(f"va{j}lo", f"(uint32_t){vsrc[j]}"), (f"va{j}hi", f"(uint32_t)({vsrc[j]} >> 32)")]
        if ret:
            exp.append(("ret", f"csum_m(&{rsrc}, mask_{ret}, sizeof(mask_{ret}))"))
        for w, _ in exp:
            H.append(f"uint32_t o{ci}_{w}; uint32_t c{ci}_{w};")
            IR.append(f"@o{ci}_{w} = external global i32")
        if ret:
            IR.append(f"@c{ci}_ret = external global i32")   # the IR callers store the returned checksum here
        ctype = lambda p: SHAPES[sn(p)].name if isS(p) else SC[p][1]
        wtype = lambda p: SHAPES[sn(p)].name if isS(p) else SC[p][2]
        rtype = SHAPES[ret].name if ret else "void"
        # ---- widened GCC callee
        body = []
        for k, p in enumerate(params):
            if isS(p): body.append(f"c{ci}_p{k} = csum_m(&p{k}, mask_{sn(p)}, sizeof(mask_{sn(p)}));")
            elif SC[p][3] == 64: body.append(f"{{ union {{ {SC[p][1]} x; uint64_t u; }} q = {{ .x = p{k} }}; c{ci}_p{k}lo = (uint32_t)q.u; c{ci}_p{k}hi = (uint32_t)(q.u >> 32); }}")
            elif p == "f32": body.append(f"c{ci}_p{k} = ((UF){{.f = p{k}}}).u;")
            elif p == "ptr": body.append(f"c{ci}_p{k} = (uint32_t)(size_t)p{k};")
            else: body.append(f"c{ci}_p{k} = p{k};")
        if var:
            last = f"p{len(params) - 1}"
            body.append(f"va_list ap; va_start(ap, {last});")
            for j, ty in enumerate(var):
                if ty == "i32": body.append(f"c{ci}_va{j} = (uint32_t)va_arg(ap, int32_t);")
                elif ty == "i64": body.append(f"{{ uint64_t q = va_arg(ap, uint64_t); c{ci}_va{j}lo = (uint32_t)q; c{ci}_va{j}hi = (uint32_t)(q >> 32); }}")
                else: body.append(f"{{ UD q = {{ .d = va_arg(ap, double) }}; c{ci}_va{j}lo = (uint32_t)q.u; c{ci}_va{j}hi = (uint32_t)(q.u >> 32); }}")
            body.append("va_end(ap);")
        if ret: body.append(f"return {rsrc};")
        wparams = ", ".join(f"{wtype(p)} p{k}" for k, p in enumerate(params)) or "void"
        H.append(f"{rtype} w{ci}({wparams}{', ...' if var else ''}) {{ {' '.join(body)} }}")
        # ---- GCC callers
        def c_arg(p, s):
            if isS(p): return s
            if p == "f32": return f"((UF){{.u = {s}}}).f"
            if p == "f64": return f"((UD){{.u = {s}}}).d"
            if p == "ptr": return f"(void *)(size_t){s}"
            if p == "b": return f"(bool)({s} & 1)"
            return f"({SC[p][1]}){s}"
        c_va = [(f"(int32_t){vsrc[j]}" if ty == "i32" else f"(int64_t){vsrc[j]}" if ty == "i64" else f"((UD){{.u = {vsrc[j]}}}).d") for j, ty in enumerate(var)]
        cargs = ", ".join([c_arg(p, srcs[k]) for k, p in enumerate(params)] + c_va)
        rproto = ", ".join(ctype(p) for p in params) or "void"
        CC.append(f"{rtype} w{ci}({rproto}{', ...' if var else ''}); {rtype} ircallee{ci}({rproto}{', ...' if var else ''});")
        CC.append(f"uint32_t csum_m(const void *p, const unsigned char *m, int n);")
        if ret:
            CC.append(f"static const unsigned char rmask_{ci}[] = {{{', '.join(map(str, SHAPES[ret].mask))}}};")
            CC.append(f"extern uint32_t o{ci}_ret, c{ci}_ret;")
            CC.append(f"void gcir{ci}(void) {{ {rtype} r = ircallee{ci}({cargs}); o{ci}_ret = csum_m(&r, rmask_{ci}, sizeof(rmask_{ci})); }}")
            CC.append(f"void gcc{ci}(void) {{ {rtype} r = w{ci}({cargs}); c{ci}_ret = csum_m(&r, rmask_{ci}, sizeof(rmask_{ci})); }}")
        else:
            CC.append(f"void gcir{ci}(void) {{ ircallee{ci}({cargs}); }}")
            CC.append(f"void gcc{ci}(void) {{ w{ci}({cargs}); }}")
        H.append(f"void gcir{ci}(void); void gcc{ci}(void); void ircaller{ci}(void); void irdyn{ci}(void);")
        # ---- IR callee
        irp = []
        if ret: irp.append(f"ptr sret([{SHAPES[ret].size} x i8]) align {SHAPES[ret].align} %ret")
        for k, p in enumerate(params):
            irp.append(f"ptr byval([{SHAPES[sn(p)].size} x i8]) align {SHAPES[sn(p)].align} %p{k}" if isS(p) else f"{SC[p][0]} %p{k}")
        L = [f"define void @ircallee{ci}({', '.join(irp)}{', ...' if var else ''}) {{"]
        def st32(v, g): L.append(f"  store volatile i32 {v}, ptr @{g}")
        for k, p in enumerate(params):
            if isS(p):
                st32(ir_csum(L, f"%p{k}", sn(p)), f"o{ci}_p{k}")
                continue
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
            L += ["  %ap = alloca [12 x i8], align 4", "  call void @llvm.va_start.p0(ptr %ap)"]
            for j, ty in enumerate(var):
                llty = {"i32": "i32", "i64": "i64", "f64": "double"}[ty]
                size, align = (4, 4) if ty == "i32" else (8, 8)
                preg, pndx = t(), t()
                L += [f"  {preg} = getelementptr i8, ptr %ap, i32 4", f"  {pndx} = getelementptr i8, ptr %ap, i32 8"]
                ndx, stk, reg = t(), t(), t()
                L += [f"  {ndx} = load i32, ptr {pndx}", f"  {stk} = load ptr, ptr %ap", f"  {reg} = load ptr, ptr {preg}"]
                if align == 8:
                    a1, a2 = t(), t(); L += [f"  {a1} = add i32 {ndx}, 7", f"  {a2} = and i32 {a1}, -8"]; ndx = a2
                new, inreg, mx, stknew, fin, base, off, addr, v = t(), t(), t(), t(), t(), t(), t(), t(), t()
                L += [f"  {new} = add i32 {ndx}, {size}", f"  {inreg} = icmp ule i32 {new}, 24",
                      f"  {mx} = call i32 @llvm.umax.i32(i32 {ndx}, i32 32)", f"  {stknew} = add i32 {mx}, {size}",
                      f"  {fin} = select i1 {inreg}, i32 {new}, i32 {stknew}", f"  store i32 {fin}, ptr {pndx}",
                      f"  {base} = select i1 {inreg}, ptr {reg}, ptr {stk}", f"  {off} = sub i32 {fin}, {size}",
                      f"  {addr} = getelementptr i8, ptr {base}, i32 {off}", f"  {v} = load {llty}, ptr {addr}, align 4"]
                if ty == "i32": st32(v, f"o{ci}_va{j}")
                else:
                    w = v
                    if ty == "f64": w = t(); L.append(f"  {w} = bitcast double {v} to i64")
                    lo, sh, hi = t(), t(), t()
                    L += [f"  {lo} = trunc i64 {w} to i32", f"  {sh} = lshr i64 {w}, 32", f"  {hi} = trunc i64 {sh} to i32"]
                    st32(lo, f"o{ci}_va{j}lo"); st32(hi, f"o{ci}_va{j}hi")
            L.append("  call void @llvm.va_end.p0(ptr %ap)")
        if ret:
            L.append(f"  call void @llvm.memcpy.p0.p0.i32(ptr align {SHAPES[ret].align} %ret, ptr align {SHAPES[ret].align} @{rsrc}_u, i32 {SHAPES[ret].size}, i1 false)")
        L += ["  ret void", "}"]
        IR += L
        # ---- IR callers
        decl_parts = ([f"ptr sret([{SHAPES[ret].size} x i8]) align {SHAPES[ret].align}"] if ret else []) + \
                     [f"ptr byval([{SHAPES[sn(p)].size} x i8]) align {SHAPES[sn(p)].align}" if isS(p) else SC[p][0] for p in params]
        IR.append(f"declare void @w{ci}({', '.join(decl_parts)}{', ...' if var else ''})")
        plain = ", ".join((["ptr"] if ret else []) + ["ptr" if isS(p) else SC[p][0].split()[0] for p in params])
        for kind in ("ircaller", "irdyn"):
            L = [f"define void @{kind}{ci}() {{"]
            args = []
            if ret:
                L.append(f"  %rbuf = alloca [{SHAPES[ret].size} x i8], align {SHAPES[ret].align}")
                args.append(f"ptr sret([{SHAPES[ret].size} x i8]) align {SHAPES[ret].align} %rbuf")
            for k, p in enumerate(params):
                if isS(p):
                    args.append(f"ptr byval([{SHAPES[sn(p)].size} x i8]) align {SHAPES[sn(p)].align} @{srcs[k]}_u"); continue
                ty, bits = SC[p][0].split()[0], SC[p][3]
                raw = t(); L.append(f"  {raw} = load volatile i{bits}, ptr @{srcs[k]}")
                if p in ("u8", "s8", "u16", "s16", "b"): v = t(); L.append(f"  {v} = trunc i32 {raw} to {ty}")
                elif p == "f32": v = t(); L.append(f"  {v} = bitcast i32 {raw} to float")
                elif p == "f64": v = t(); L.append(f"  {v} = bitcast i64 {raw} to double")
                elif p == "ptr": v = t(); L.append(f"  {v} = inttoptr i32 {raw} to ptr")
                else: v = raw
                args.append(f"{SC[p][0]} {v}")
            for j, ty in enumerate(var):
                raw = t(); L.append(f"  {raw} = load volatile {'i32' if ty == 'i32' else 'i64'}, ptr @{vsrc[j]}")
                if ty == "f64": v = t(); L.append(f"  {v} = bitcast i64 {raw} to double"); args.append(f"double {v}")
                else: args.append(f"{'i32' if ty == 'i32' else 'i64'} {raw}")
            reps = 1
            if kind == "irdyn":
                n = t(); L += [f"  {n} = load volatile i32, ptr @dyn_n", f"  %dyn = alloca i8, i32 {n}, align 4", "  store volatile i8 1, ptr %dyn"]
                reps = 3
            for _ in range(reps):
                L.append(f"  call void ({plain}{', ...' if var else ''}) @w{ci}({', '.join(args)})")
            if ret:
                st = ir_csum(L, "%rbuf", ret)
                L.append(f"  store volatile i32 {st}, ptr @c{ci}_ret")
            if kind == "irdyn":
                L.append("  store volatile i8 2, ptr %dyn")
            L += ["  ret void", "}"]
            IR += L
        # ---- driver
        m = [f"static void t{ci}(void) {{", f"  poison(); gcir{ci}();"]
        m += [f'  chk("c->ir", {ci}, "{w}", o{ci}_{w}, {e});' for w, e in exp]
        for path, call in (("ir->c", f"ircaller{ci}()"), ("irdyn->c", f"irdyn{ci}()"), ("c->c", f"gcc{ci}()")):
            m += [f"  c{ci}_{w} = 0x5A5A5A5Au;" for w, _ in exp]
            m.append(f"  poison(); {call};")
            m += [f'  chk("{path}", {ci}, "{w}", c{ci}_{w}, {e});' for w, e in exp]
        m.append("}")
        H += m
        main.append(f"  t{ci}();")
    IR.append("declare void @llvm.memcpy.p0.p0.i32(ptr, ptr, i32, i1)")
    H.append("int main(void) {\n" + "\n".join(main) + '\n  puts_("DONE checks="); hex_(checks); puts_(" fails="); hex_(fails); puts_("\\n"); return 0; }')
    return "\n".join(H) + "\n", "\n".join(CC) + "\n", "\n".join(IR) + "\n"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("llc", nargs="?"); ap.add_argument("chip", nargs="?", choices=list(W.CHIPS)); ap.add_argument("out", nargs="?")
    ap.add_argument("--opt", default="O2"); ap.add_argument("--chunk", default="0/1"); ap.add_argument("--seed", type=int, default=278)
    ap.add_argument("--cases"); ap.add_argument("--list", action="store_true"); ap.add_argument("--list-json")
    a = ap.parse_args()
    allc = all_cases()
    if a.list:
        print(len(allc), "cases;", sum(1 for c in allc if is_odd_case(c)), "with an odd aggregate > 4 bytes")
        return
    if a.list_json:
        json.dump(allc, open(a.list_json, "w")); return
    ci, cn = map(int, a.chunk.split("/"))
    idx = [int(x) for x in open(a.cases).read().split()] if a.cases else list(range(ci, len(allc), cn))
    sel = [allc[i] for i in idx]
    ch = W.CHIPS[a.chip]
    os.makedirs(a.out, exist_ok=True)
    P = lambda n: os.path.join(a.out, n)
    rng = random.Random(a.seed * 100003 + (idx[0] if idx else 0))
    h, c, ir = build(sel, rng)
    real8266 = ch["gcc"] == "LX106"
    for n, tx in (("harness.c", h), ("ccaller.c", c), ("ir.ll", ir), ("libc.c", Q.LIBC), ("link.ld", W.LX106_LD if real8266 else Q.LDSCRIPT)):
        open(P(n), "w", newline="\n").write(tx)
    json.dump(idx, open(P("case_index.json"), "w"))
    start = W.LX106_START if real8266 else Q.START[ch["abi"]] + Q.VECTORS
    if not ch["fpu"] and not real8266:
        start = start.replace("    movi a2, 1           /* enable coprocessor 0 (FPU) */\n    wsr a2, cpenable\n", "")
    open(P("start.S"), "w").write(start)
    gcc = W.LX106_GCC if real8266 else os.path.join(W.GCCBIN, ch["gcc"])
    gabi = ["-mabi=call0"] if ch["abi"] == "call0" and not real8266 else []
    res = dict(chip=a.chip, opt=a.opt, chunk=a.chunk, llc=a.llc, cases=len(sel), first_case=idx[0] if idx else None)
    try:
        for src in ("harness.c", "ccaller.c", "libc.c"):
            W.run([gcc, "-O2", "-ffreestanding", "-fno-builtin", "-fno-strict-aliasing", "-fno-tree-loop-distribute-patterns",
                   "-mtext-section-literals", "-mlongcalls", "-c"] + gabi + [P(src), "-o", P(src + ".o")])
        W.run([gcc] + gabi + ["-c", P("start.S"), "-o", P("start.o")])
        llc = [a.llc, f"-{a.opt}", f"-mcpu={ch['mcpu']}", P("ir.ll")]
        # Some chunks (generic CPU without MUL32, -O0) are too large for one .literal section to
        # stay within l32r range; per-function sections keep every literal pool next to its
        # function. Placement only, the code of each function is unchanged.
        llc.insert(1, "-function-sections")
        W.run(llc + ["-filetype=obj", "-o", P("ir.o")])
        asm = W.run(llc + ["-o", "-"]).stdout
        res["abi_ok"] = (re.search(r"^\s+entry\s+a1,", asm, re.M) is not None) == (ch["abi"] == "windowed")
        W.run([gcc, "-nostdlib", "-nostartfiles", "-T", P("link.ld"), "-Wl,--no-relax", "-Wl,--gc-sections",
               P("start.o"), P("harness.c.o"), P("ccaller.c.o"), P("ir.o"), P("libc.c.o"), "-o", P("test.elf")] + gabi + ["-lgcc"])
        try:
            p = subprocess.run([Q.QEMU, "-M", "sim", "-cpu", ch.get("qemu_cpu", "de233_fpu"), "-display", "none", "-monitor", "none",
                                "-serial", "none", "-semihosting", "-kernel", P("test.elf")], capture_output=True, text=True, timeout=600,
                               encoding="utf-8", errors="replace")
            out = p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            out = "TIMEOUT"
        open(P("qemu.out"), "w", encoding="utf-8").write(out)
        done = [l for l in out.splitlines() if l.startswith("DONE")]
        res["done"] = bool(done); res["exception"] = "EXCEPTION" in out; res["timeout"] = out == "TIMEOUT"
        if done:
            f = done[0].split(); res["checks"] = int(f[1].split("=")[1], 16); res["fails"] = int(f[2].split("=")[1], 16)
    except Exception as e:
        res["error"] = str(e)[:3000]
    print(json.dumps(res))

if __name__ == "__main__":
    main()
