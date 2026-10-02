#!/usr/bin/env python3
"""Differential Xtensa C-ABI test: GCC (reference) vs rustc.

For random extern "C" signatures we compile
  * a callee that copies every parameter (or struct field) into its own global, and
  * a caller that loads every argument from its own global and calls the callee,
with both GCC and rustc, then symbolically trace the assembly to recover
  * callee: which register / incoming stack word (and bit offset) each value is read from;
  * caller: what ends up in each argument register / outgoing stack word, including
    how the upper bits were extended and how wide the stack store was.
Any difference to GCC is an ABI mismatch.

usage: difftest.py RUSTC OUTDIR [--abi call0|windowed] [--n N] [--seed S]
"""
import argparse, os, random, re, subprocess, sys
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
import config
REPO = config.RUST_REPO
GCC = os.path.join(config.ESP_GCC_BIN, "xtensa-esp32s3-elf-gcc.exe")
TARGET = "xtensa-esp32s3-none-elf"

# name: (C type, Rust type, kind)   kind: int bits/sign, float, wide(64-bit), struct
SCALARS = {
    "u8": ("uint8_t", "u8"), "i8": ("int8_t", "i8"), "u16": ("uint16_t", "u16"),
    "i16": ("int16_t", "i16"), "bool": ("bool", "bool"), "u32": ("uint32_t", "u32"),
    "i32": ("int32_t", "i32"), "f32": ("float", "f32"), "u64": ("uint64_t", "u64"),
    "i64": ("int64_t", "i64"), "f64": ("double", "f64"),
}
# struct name: list of (field type) ; optional align
STRUCTS = OrderedDict([
    ("S1", (["u8"], None)), ("S2", (["u16"], None)), ("S3", (["u8"] * 3, None)),
    ("S5", (["u8"] * 5, None)), ("S6", (["u16"] * 3, None)), ("S7", (["u8"] * 7, None)),
    ("S8", (["u32"] * 2, None)), ("SL", (["u64"], None)), ("S12", (["u32"] * 3, None)),
    ("S20", (["u32"] * 5, None)), ("B28", (["u32"] * 7, None)), ("A16", (["u32"], 16)),
    ("P4", (["u16", "u8"], None)),
])
FIELD_NAMES = "abcdefg"

def gen_sigs(rng, n):
    small = ["u8", "i8", "u16", "i16", "bool"]
    word = ["u32", "i32", "f32"]
    wide = ["u64", "i64", "f64"]
    structs = list(STRUCTS)
    sigs = []
    for _ in range(n):
        k = rng.randint(1, 11)
        params = []
        for _ in range(k):
            r = rng.random()
            if r < 0.40: params.append(rng.choice(small))
            elif r < 0.60: params.append(rng.choice(word))
            elif r < 0.75: params.append(rng.choice(wide))
            else: params.append(rng.choice(structs))
        sigs.append(params)
    return sigs

# ---------------------------------------------------------------- source emitters
def c_prelude():
    s = ["#include <stdint.h>", "#include <stdbool.h>"]
    for name, (fields, align) in STRUCTS.items():
        attr = f" __attribute__((aligned({align})))" if align else ""
        body = " ".join(f"{SCALARS[t][0]} {FIELD_NAMES[i]};" for i, t in enumerate(fields))
        s.append(f"typedef struct{attr} {{ {body} }} {name};")
    s.append("typedef union { float f; uint32_t u; } UF; typedef union { double d; uint64_t u; } UD;")
    return "\n".join(s) + "\n"

def rs_prelude():
    s = ["#![feature(no_core, lang_items)]", "#![no_core]", '#![crate_type = "lib"]',
         "#![allow(non_upper_case_globals, static_mut_refs, unused, internal_features)]",
         "extern crate minicore;", "use minicore::*;", "use minicore::mem::transmute;",
         "#[repr(C)] pub struct W { lo: u32, hi: u32 } impl Copy for W {}"]
    for name, (fields, align) in STRUCTS.items():
        rep = f"#[repr(C, align({align}))]" if align else "#[repr(C)]"
        body = ", ".join(f"{FIELD_NAMES[i]}: {SCALARS[t][1]}" for i, t in enumerate(fields))
        s.append(f"{rep} pub struct {name} {{ {body} }} impl Copy for {name} {{}}")
    return "\n".join(s) + "\n"

def outputs(p):
    """list of (suffix, C expr template, Rust expr template) for values to export from param p."""
    if p in STRUCTS:
        fields, _ = STRUCTS[p]
        idx = [0] if len(fields) == 1 else [0, len(fields) - 1]
        res = []
        for i in idx:
            ft = fields[i]; fn = FIELD_NAMES[i]
            if ft == "u64":
                res += [(f"{fn}lo", f"(uint32_t)({{x}}.{fn})", f"{{x}}.{fn} as u32"),
                        (f"{fn}hi", f"(uint32_t)({{x}}.{fn} >> 32)", f"transmute::<u64, W>({{x}}.{fn}).hi")]
            else:
                res.append((fn, f"{{x}}.{fn}", f"{{x}}.{fn} as u32"))
        return res
    if p in ("u64", "i64"):
        return [("lo", "(uint32_t){x}", "transmute::<_, W>({x}).lo"),
                ("hi", "(uint32_t)((uint64_t){x} >> 32)", "transmute::<_, W>({x}).hi")]
    if p == "f64":
        return [("lo", "(uint32_t)((UD){{.d={x}}}).u", "transmute::<f64, W>({x}).lo"),
                ("hi", "(uint32_t)(((UD){{.d={x}}}).u >> 32)", "transmute::<f64, W>({x}).hi")]
    if p == "f32":
        return [("", "((UF){{.f={x}}}).u", "transmute::<f32, u32>({x})")]
    return [("", "{x}", "{x} as u32")]

def ctype(p): return p if p in STRUCTS else SCALARS[p][0]
def rtype(p): return p if p in STRUCTS else SCALARS[p][1]

def emit(sigs):
    cc, cr, rc, rr = [c_prelude()], [c_prelude()], [rs_prelude()], [rs_prelude()]
    rr.append('extern "C" {')
    for f, params in enumerate(sigs):
        cargs = ", ".join(f"{ctype(p)} p{k}" for k, p in enumerate(params))
        rargs = ", ".join(f"p{k}: {rtype(p)}" for k, p in enumerate(params))
        # callee
        cbody, rbody = [], []
        for k, p in enumerate(params):
            for suf, ce, re_ in outputs(p):
                g = f"o{f}_{k}{suf}"
                cc.append(f"volatile uint32_t {g};")
                rc.append(f"#[no_mangle] pub static mut {g}: u32 = 0;")
                cbody.append(f"{g} = {ce.format(x=f'p{k}')};")
                rbody.append(f"{g} = {re_.format(x=f'p{k}')};")
        cc.append(f"void f{f}({cargs}) {{ {' '.join(cbody)} }}")
        rc.append(f'#[no_mangle] pub unsafe extern "C" fn f{f}({rargs}) {{ {" ".join(rbody)} }}')
        # caller
        cr.append(f"void f{f}({cargs});")
        rr.append(f"    fn f{f}({rargs});")
        for k, p in enumerate(params):
            cr.append(f"{ctype(p)} i{f}_{k};")
        cr.append(f"void g{f}(void) {{ f{f}({', '.join(f'i{f}_{k}' for k in range(len(params)))}); }}")
    rr.append("}")
    zero = {"f32": "0.0", "f64": "0.0", "bool": "false"}
    for f, params in enumerate(sigs):
        for k, p in enumerate(params):
            if p in STRUCTS:
                fields, _ = STRUCTS[p]
                init = f"{p} {{ " + ", ".join(f"{FIELD_NAMES[i]}: {zero.get(t, '0')}" for i, t in enumerate(fields)) + " }"
            else:
                init = zero.get(p, "0")
            rr.append(f"#[no_mangle] pub static mut i{f}_{k}: {rtype(p)} = {init};")
        rr.append(f"#[no_mangle] pub unsafe extern \"C\" fn g{f}() {{ f{f}({', '.join(f'i{f}_{k}' for k in range(len(params)))}); }}")
    return "\n".join(cc) + "\n", "\n".join(cr) + "\n", "\n".join(rc) + "\n", "\n".join(rr) + "\n"

# ---------------------------------------------------------------- asm parsing
def parse_asm(text):
    lits, funcs, cur, pending = {}, {}, None, None
    for line in text.splitlines():
        m = re.match(r"\s*\.literal\s+(\S+),\s*(\S+)", line)
        if m:
            lits[m.group(1)] = m.group(2); continue
        m = re.match(r"^(\.LCPI\S+):\s*$", line)
        if m:
            pending = m.group(1); continue
        m = re.match(r"\s*\.(long|word)\s+(\S+)", line)
        if m and pending:
            lits[pending] = m.group(2); pending = None; continue
        m = re.match(r"^([A-Za-z_]\w*):\s*$", line)
        if m:
            cur = m.group(1); funcs[cur] = []; continue
        if cur and line.startswith("\t") and not line.strip().startswith("."):
            ins = line.strip().split(None, 1)
            op = ins[0]
            ops = [o.strip() for o in ins[1].split(",")] if len(ins) > 1 else []
            funcs[cur].append((op, ops))
        if re.match(r"\s*\.size\s", line):
            cur = None
    return lits, funcs

def lit_value(s):
    m = re.match(r"^([A-Za-z_]\w*)(?:\+(\d+))?$", s)
    if m and not re.match(r"^-?\d", s):
        return ("sym", m.group(1), int(m.group(2) or 0))
    try:
        return ("const", int(s, 0) & 0xffffffff)
    except ValueError:
        return ("unk",)

def reg(r):
    return "a1" if r == "sp" else r

LOADS = {"l8ui": (8, "z8"), "l16ui": (16, "z16"), "l16si": (16, "s16"), "l32i": (32, "full"), "l32i.n": (32, "full")}
STORES = {"s8i": 8, "s16i": 16, "s32i": 32, "s32i.n": 32}

def trace(insns, lits, windowed, role, target_call):
    """Symbolic execution. Data values: ('v', src, shift, ext). src = ('r', n) | ('s', word) | ('g', sym, off)."""
    R = {f"a{i}": ("unk",) for i in range(16)}
    R["a1"] = ("sp", 0)
    if role == "callee":
        for i in range(2, 8):
            R[f"a{i}"] = ("v", ("r", i), 0, "full")
    mem, outs, snap, notes = {}, {}, None, []
    def load(base, off, kind, width):
        b = R.get(base, ("unk",))
        if b[0] == "sp":
            a = b[1] + off
            if a in mem:
                return mem[a][0]
            if role == "callee" and a >= 0:
                if width == 32 and a % 4:
                    notes.append(f"misaligned l32i @+{a}")
                return ("v", ("s", a - a % 4), (a % 4) * 8, kind)
            return ("unk",)
        if b[0] == "sym":
            return ("v", ("g", b[1], b[2] + off), 0, kind)
        return ("unk",)
    def data(v): return v[0] == "v"
    for op, o in insns:
        o = [reg(x) for x in o]
        d = o[0] if o else None
        if op == "entry":
            R["a1"] = ("sp", -int(o[1], 0)); continue
        if op in ("l32r",):
            R[d] = lit_value(lits.get(o[1], "?")); continue
        if op in ("movi", "movi.n"):
            R[d] = ("const", int(o[1], 0) & 0xffffffff); continue
        if op in ("mov", "mov.n"):
            R[d] = R[o[1]]; continue
        if op == "or":
            a, b = R[o[1]], R[o[2]]
            R[d] = a if o[1] == o[2] else (a if b == ("const", 0) else (b if a == ("const", 0) else ("unk",)))
            continue
        if op == "and":
            a, b = R[o[1]], R[o[2]]
            if b[0] == "const" and data(a): a, b = a, b
            elif a[0] == "const" and data(b): a, b = b, a
            else: R[d] = ("unk",); continue
            m = {0xff: "z8", 0xffff: "z16", 0x1: "z1"}.get(b[1])
            R[d] = ("v", a[1], a[2], m) if m else ("unk",); continue
        if op == "extui":
            a = R[o[1]]; sh, bits = int(o[2], 0), int(o[3], 0)
            R[d] = ("v", a[1], a[2] + sh, f"z{bits}") if data(a) else ("unk",); continue
        if op == "srli":
            a = R[o[1]]; n = int(o[2], 0)
            R[d] = ("v", a[1], a[2] + n, "lshr") if data(a) else ("unk",); continue
        if op == "sext":
            a = R[o[1]]; b = int(o[2], 0)
            R[d] = ("v", a[1], a[2], f"s{b + 1}") if data(a) else ("unk",); continue
        if op == "slli":
            a = R[o[1]]; n = int(o[2], 0)
            R[d] = ("shl", a, n) if data(a) else ("unk",); continue
        if op == "srai":
            a = R[o[1]]; n = int(o[2], 0)
            if a[0] == "shl" and a[2] <= n:
                v = a[1]; R[d] = ("v", v[1], v[2] + (n - a[2]), f"s{32 - n}")
            elif data(a) and a[3] == "full" and n == 31:
                R[d] = ("v", a[1], a[2] + 31, "sign")
            else:
                R[d] = ("unk",)
            continue
        if op in ("addi", "addi.n"):
            a = R[o[1]]; imm = int(o[2], 0)
            R[d] = ("sp", a[1] + imm) if a[0] == "sp" else (("const", (a[1] + imm) & 0xffffffff) if a[0] == "const" else ("unk",))
            continue
        if op in ("add", "add.n"):
            a, b = R[o[1]], R[o[2]]
            if a[0] == "sp" and b[0] == "const": R[d] = ("sp", a[1] + b[1])
            elif b[0] == "sp" and a[0] == "const": R[d] = ("sp", b[1] + a[1])
            else: R[d] = ("unk",)
            continue
        if op in LOADS:
            w, kind = LOADS[op]
            R[d] = load(o[1], int(o[2], 0), kind, w); continue
        if op in STORES:
            w = STORES[op]; v = R[o[0]]; b = R.get(o[1], ("unk",)); off = int(o[2], 0)
            if b[0] == "sym" and role == "callee":
                outs[b[1]] = v
            elif b[0] == "sp":
                mem[b[1] + off] = (v, w)
            continue
        if op in ("j", "j.l") and o and o[0] == target_call:
            op = "call0"
        if op in ("call0", "callx0", "call8", "callx8"):
            if role == "caller" and (op.startswith("callx") and R[o[0]] == ("sym", target_call, 0) or o[0] == target_call):
                sp = R["a1"][1]
                regs = range(10, 16) if op.endswith("8") else range(2, 8)
                snap = {"regs": {f"r{i - (8 if op.endswith('8') else 0)}": R[f"a{i}"] for i in regs},
                        "stack": {a - sp: mem[a] for a in mem if a - sp >= 0}}
            else:
                notes.append(f"other call {o}")
            continue
        if op in ("memw", "ret", "ret.n", "retw", "retw.n", "nop", "nop.n", "isync"):
            continue
        if op.startswith("j") or op.startswith("b"):
            notes.append(f"branch {op}")
            continue
        if d and re.match(r"a\d+$", d):
            R[d] = ("unk",)
    return outs, snap, notes

def fmt_src(v):
    if v[0] != "v": return "?"
    s = v[1]
    loc = f"a{s[1]}" if s[0] == "r" else (f"stk+{s[1]}" if s[0] == "s" else f"{s[1]}+{s[2]}")
    return f"{loc}>>{v[2]}" if v[2] else loc


def c_layout(p):
    """offsets of data bytes of type p (C layout)."""
    if p not in STRUCTS:
        return set(range({"u8":1,"i8":1,"bool":1,"u16":2,"i16":2,"u64":8,"i64":8,"f64":8}.get(p, 4)))
    fields, _ = STRUCTS[p]
    off, res = 0, set()
    for t in fields:
        sz = {"u8":1,"u16":2,"u32":4,"u64":8}[t]
        off = (off + sz - 1) // sz * sz
        res |= set(range(off, off + sz)); off += sz
    return res
valid_bytes = c_layout

def value_bytes(v, w):
    if v[0] != "v":
        return ["?"] * w
    src, shift, ext = v[1], v[2], v[3]
    m = re.match(r"([zs])(\d+)$", ext or "")
    out = []
    for i in range(w):
        bit = 8 * i
        if m and bit >= int(m.group(2)):
            out.append(("Z",) if m.group(1) == "z" else ("S", src))
        elif ext in ("lshr", "sign") or shift % 8 or src[0] != "g":
            out.append("?" if src[0] == "g" else ("r", src, shift + bit))
        else:
            out.append(("d", src[1], src[2] + shift // 8 + i))
    return out

def fmt_b(b):
    if isinstance(b, str): return b
    if b[0] == "Z": return "zero-ext"
    if b[0] == "S": return f"sign-ext({b[1][1]})"
    if b[0] == "d": return f"{b[1]}[{b[2]}]"
    return str(b)

# ---------------------------------------------------------------- driver
def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        sys.exit(f"FAILED: {' '.join(cmd)}\n{p.stderr[-4000:]}")
    return p

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rustc"); ap.add_argument("out")
    ap.add_argument("--abi", default="call0", choices=["call0", "windowed"])
    ap.add_argument("--n", type=int, default=300); ap.add_argument("--seed", type=int, default=278)
    ap.add_argument("--show", type=int, default=12)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rng = random.Random(a.seed)
    sigs = gen_sigs(rng, a.n)
    # fixed regression shapes first
    sigs = [["u32"] * 5 + ["S6", "u16"], ["u32"] * 5 + ["S5", "i8", "bool"], ["u32"] * 3 + ["i64", "u16", "u32"],
            ["u32"] * 6 + ["u16", "i16", "u8", "i8", "bool"]] + sigs
    files = dict(zip(["callee.c", "caller.c", "callee.rs", "caller.rs"], emit(sigs)))
    for n, t in files.items():
        open(os.path.join(a.out, n), "w").write(t)
    gflags = ["-O2", "-S"] + (["-mabi=call0"] if a.abi == "call0" else [])
    rflags = ["--target", TARGET, "-Copt-level=3", "-Cpanic=abort", "-Awarnings", "--emit", "asm"]
    if a.abi == "windowed":
        rflags += ["-Ctarget-feature=+windowed"]
    mc = os.path.join(a.out, "libminicore.rlib")
    run([a.rustc, "--target", TARGET, "--crate-type", "rlib", "-Copt-level=3", "-Cpanic=abort", "-Awarnings",
         os.path.join(REPO, "tests", "auxiliary", "minicore.rs"), "-o", mc] + (["-Ctarget-feature=+windowed"] if a.abi == "windowed" else []))
    for side in ("callee", "caller"):
        run([GCC] + gflags + [os.path.join(a.out, f"{side}.c"), "-o", os.path.join(a.out, f"{side}.gcc.s")])
        run([a.rustc] + rflags + ["--extern", f"minicore={mc}", os.path.join(a.out, f"{side}.rs"), "-o", os.path.join(a.out, f"{side}.rust.s")])
    asm = {(side, cc): parse_asm(open(os.path.join(a.out, f"{side}.{cc}.s")).read())
           for side in ("callee", "caller") for cc in ("gcc", "rust")}
    win = a.abi == "windowed"
    stats = {"callee_vals": 0, "callee_unk": 0, "caller_slots": 0, "caller_unk": 0}
    mism = []
    for f, params in enumerate(sigs):
        sig = f"f{f}({', '.join(params)})"
        # callee side: where does each value come from?
        res = {}
        for cc in ("gcc", "rust"):
            lits, funcs = asm[("callee", cc)]
            outs, _, notes = trace(funcs[f"f{f}"], lits, win, "callee", None)
            res[cc] = ({k: fmt_src(v) for k, v in outs.items()}, notes)
        gk, rk = res["gcc"][0], res["rust"][0]
        for k in sorted(set(gk) | set(rk)):
            stats["callee_vals"] += 1
            gv, rv = gk.get(k, "missing"), rk.get(k, "missing")
            if "?" in (gv, rv): stats["callee_unk"] += 1
            if gv != rv:
                mism.append((sig, f"callee {k}: gcc={gv} rust={rv}"))
        for n in res["rust"][1]:
            if "misaligned" in n: mism.append((sig, f"callee rust {n}"))
        # caller side: what is in each argument slot?
        snaps = {}
        for cc in ("gcc", "rust"):
            lits, funcs = asm[("caller", cc)]
            _, snap, notes = trace(funcs[f"g{f}"], lits, win, "caller", f"f{f}")
            snaps[cc] = snap
        if not snaps["gcc"] or not snaps["rust"]:
            mism.append((sig, f"caller: could not find call (gcc={bool(snaps['gcc'])}, rust={bool(snaps['rust'])})"))
            continue
        used_regs = set()
        for cc in ("gcc", "rust"):
            lits, funcs = asm[("callee", cc)]
            outs, _, _ = trace(funcs[f"f{f}"], lits, win, "callee", None)
            used_regs |= {v[1][1] for v in outs.values() if v[0] == "v" and v[1][0] == "r"}
            if cc == "gcc":
                area = max([v[1][1] + 4 for v in outs.values() if v[0] == "v" and v[1][0] == "s"] + [0])
        valid = {f"i{f}_{k}": valid_bytes(p) for k, p in enumerate(params)}
        def bytemap(s):
            d = {}
            for r, v in s["regs"].items():
                n = int(r[1:])
                if n in used_regs:
                    for i, b in enumerate(value_bytes(v, 4)):
                        d[(f"a{n}", i)] = b
            for off in sorted(s["stack"]):
                if off >= area:
                    continue  # caller scratch space above the outgoing argument area
                v, w = s["stack"][off]
                for i, b in enumerate(value_bytes(v, w // 8)):
                    d[("stk", off + i)] = b
            return d
        gb, rb = bytemap(snaps["gcc"]), bytemap(snaps["rust"])
        for loc in sorted(gb, key=str):
            g = gb[loc]
            if g == "?":
                stats["caller_unk"] += 1; continue
            if isinstance(g, tuple) and g[0] == "d" and g[2] not in valid.get(g[1], set()):
                continue  # padding / bytes outside the source object
            stats["caller_slots"] += 1
            r = rb.get(loc, "<unwritten>")
            if r == "?":
                stats["caller_unk"] += 1; continue
            if r != g:
                where = f"{loc[0]}+{loc[1]}" if loc[0] == "stk" else f"{loc[0]}.byte{loc[1]}"
                mism.append((sig, f"caller {where}: gcc={fmt_b(g)} rust={fmt_b(r)}"))
    print(f"abi={a.abi} signatures={len(sigs)} rustc={a.rustc}")
    print(f"callee values compared: {stats['callee_vals']} (untraceable: {stats['callee_unk']})")
    print(f"caller slots compared:  {stats['caller_slots']} (untraceable: {stats['caller_unk']})")
    bad_sigs = OrderedDict()
    for s, m in mism:
        bad_sigs.setdefault(s, []).append(m)
    print(f"MISMATCHING SIGNATURES: {len(bad_sigs)} / {len(sigs)}  (total mismatches {len(mism)})")
    for s, ms in list(bad_sigs.items())[: a.show]:
        print("  " + s)
        for m in ms[:6]:
            print("     " + m)
    open(os.path.join(a.out, "mismatches.txt"), "w").write(
        "\n".join(f"{s}\n    " + "\n    ".join(ms) for s, ms in bad_sigs.items()))

if __name__ == "__main__":
    main()
