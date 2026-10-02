#!/usr/bin/env python3
"""QEMU run-time ABI matrix for the Xtensa stack-argument fixes.

One run = (rustc build, llc build, chip config, seed). Rust code is compiled by rustc to
LLVM IR only; the given llc turns it into an object, so different LLVM fixes can be
compared without rebuilding rustc. GCC for the matching chip compiles the C side.

Scenarios per run:
  plain   : random signatures, gcc->rust, rust->gcc (widened callee), gcc->gcc control
  varargs : fixed args (incl. odd-size structs pushed to the stack) followed by varargs,
            gcc->rust (Rust `...` callee, i.e. llvm.va_start + rustc's va_arg),
            rust->gcc (Rust caller of a C variadic callee), gcc->gcc control
Also: every Rust IR file is run through `llc -verify-machineinstrs`, and the object is
checked to really use the windowed (entry/retw) or call0 ABI.
"""
import argparse, json, os, random, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qemutest as Q
from difftest import STRUCTS, SCALARS, gen_sigs, REPO

GCCBIN = Q.GCCBIN
CHIPS = {
    "esp32":   dict(target="xtensa-esp32-none-elf",   gcc="xtensa-esp32-elf-gcc.exe",   abi="windowed", cpu=None, fpu=True),
    "esp32s2": dict(target="xtensa-esp32s2-none-elf", gcc="xtensa-esp32s2-elf-gcc.exe", abi="windowed", cpu=None, fpu=False),
    "esp32s3": dict(target="xtensa-esp32s3-none-elf", gcc="xtensa-esp32s3-elf-gcc.exe", abi="windowed", cpu=None, fpu=True),
    # call0 ABI: same IR, generic CPU (no windowed option); GCC told to use call0
    "call0":   dict(target="xtensa-esp32s3-none-elf", gcc="xtensa-esp32s3-elf-gcc.exe", abi="call0", cpu="generic", fpu=True),
    # esp8266 (lx106) is call0-only; no upstream Rust target or GCC here, so: esp32 IR retargeted to
    # -mcpu=esp8266, and esp32 GCC in call0 mode as the ABI reference
    "esp8266": dict(target="xtensa-esp32-none-elf",   gcc="xtensa-esp32-elf-gcc.exe",   abi="call0", cpu="esp8266", fpu=True),
}
VA_TYPES = ["i32", "i64", "f64"]

def va_sigs(rng, n):
    fixed_cases = [
        (["u32"] * 6 + ["S6"], ["i32", "i32", "i64", "f64"]),
        (["u32"] * 6 + ["S5"], ["i32"]),
        (["u32"] * 6 + ["S7"], ["f64", "i32"]),
        (["u32"] * 5 + ["S6"], ["i32", "i64"]),
        (["u32"] * 5 + ["S5"], ["i32", "i32"]),
        (["u32"] * 4 + ["B28"], ["i32"]),
        (["u32"] * 6 + ["u16", "S6"], ["i32", "f64"]),
        (["u32"] * 6, ["i32", "i32", "i32"]),
        (["u32"], ["i32"] * 8),
        (["u32"] * 3, ["i64", "i32", "f64", "i32"]),
        (["u16"], ["i32", "i64"]),
        (["S6"], ["i32"] * 7),
        (["S5", "S7"], ["i32", "i64", "i32"]),
    ]
    structs = list(STRUCTS)
    small = ["u8", "i8", "u16", "i16", "bool"]
    out = list(fixed_cases)
    for _ in range(n):
        fixed = []
        for _ in range(rng.randint(1, 9)):
            r = rng.random()
            fixed.append(rng.choice(structs) if r < 0.35 else rng.choice(small) if r < 0.6 else rng.choice(["u32", "i32", "u64", "f64", "f32"]))
        out.append((fixed, [rng.choice(VA_TYPES) for _ in range(rng.randint(1, 7))]))
    return out

def va_expected(t, s):
    if t == "i32": return [("", f"(uint32_t){s}")]
    return [("lo", f"(uint32_t){s}"), ("hi", f"(uint32_t)({s} >> 32)")]

def emit_va(vsigs, rng):
    """returns (harness_add, harness_main_calls, ccaller_add, rcallee_add, rcaller_extern, rcaller_add)"""
    H, calls, CC, RC, RX, RR = [], [], [], [], [], []
    H.append("#include <stdarg.h>")
    for f, (fixed, var) in enumerate(vsigs):
        real_c = ", ".join(f"{Q.ctype(p)} p{k}" for k, p in enumerate(fixed))
        wide_c = ", ".join(f"{'uint32_t' if p in Q.SMALL else Q.ctype(p)} p{k}" for k, p in enumerate(fixed))
        real_r = ", ".join(f"p{k}: {Q.rtype(p)}" for k, p in enumerate(fixed))
        fs = [f"sf{f}_{k}" for k in range(len(fixed))]
        vs = [f"sv{f}_{j}" for j in range(len(var))]
        for k, p in enumerate(fixed):
            H.append(f"{Q.src_ctype(p)} {fs[k]} = {Q.rand_src(rng, p)};")
            CC.append(f"extern {Q.src_ctype(p)} {fs[k]};")
            RX.append(f"    static {fs[k]}: {'u64' if Q.src_ctype(p) == 'uint64_t' else ('u32' if Q.src_ctype(p) == 'uint32_t' else p)};")
        for j, t in enumerate(var):
            ct = "uint32_t" if t == "i32" else "uint64_t"
            H.append(f"{ct} {vs[j]} = {Q.rand_src(rng, {'i32': 'u32', 'i64': 'u64', 'f64': 'f64'}[t])};")
            CC.append(f"extern {ct} {vs[j]};")
            RX.append(f"    static {vs[j]}: {'u32' if t == 'i32' else 'u64'};")
        checks, wbody, rbody = [], [], []
        for k, p in enumerate(fixed):
            for suf, cx, rx, ex in Q.outputs(p):
                o, c = f"of{f}_{k}{suf}", f"cf{f}_{k}{suf}"
                H.append(f"uint32_t {o}; uint32_t {c};")
                RC.append(f'extern "C" {{ static mut {o}: u32; }}')
                wbody.append(f"{c} = {('p%d' % k) if p in Q.SMALL else cx.replace('x', 'p%d' % k)};")
                rbody.append(f"{o} = {rx.replace('x', 'p%d' % k)};")
                e = ex.replace("s.", fs[k] + ".") if p in STRUCTS else ex.replace("s", fs[k])
                checks.append((o, c, e, f"p{k}{suf}"))
        wbody.append("va_list ap;"); wbody.append(f"va_start(ap, p{len(fixed) - 1});")
        rbody_va = []
        for j, t in enumerate(var):
            o, c = f"ov{f}_{j}", f"cv{f}_{j}"
            if t == "i32":
                H.append(f"uint32_t {o}; uint32_t {c};")
                RC.append(f'extern "C" {{ static mut {o}: u32; }}')
                wbody.append(f"{c} = (uint32_t)va_arg(ap, int32_t);")
                rbody_va.append(f"{o} = ap.next_arg::<i32>() as u32;")
                checks.append((o, c, f"(uint32_t){vs[j]}", f"va{j}"))
            else:
                for half in ("lo", "hi"):
                    H.append(f"uint32_t {o}{half}; uint32_t {c}{half};")
                    RC.append(f'extern "C" {{ static mut {o}{half}: u32; }}')
                if t == "i64":
                    wbody.append(f"{{ uint64_t t = va_arg(ap, uint64_t); {c}lo = (uint32_t)t; {c}hi = (uint32_t)(t >> 32); }}")
                    rbody_va.append(f"{{ let w = transmute::<i64, W>(ap.next_arg::<i64>()); {o}lo = w.lo; {o}hi = w.hi; }}")
                else:
                    wbody.append(f"{{ UD t = {{ .d = va_arg(ap, double) }}; {c}lo = (uint32_t)t.u; {c}hi = (uint32_t)(t.u >> 32); }}")
                    rbody_va.append(f"{{ let w = transmute::<f64, W>(ap.next_arg::<f64>()); {o}lo = w.lo; {o}hi = w.hi; }}")
                checks.append((o + "lo", c + "lo", f"(uint32_t){vs[j]}", f"va{j}lo"))
                checks.append((o + "hi", c + "hi", f"(uint32_t)({vs[j]} >> 32)", f"va{j}hi"))
        wbody.append("va_end(ap);")
        H.append(f"void wv{f}({wide_c}, ...) {{ {' '.join(wbody)} }}")
        H.append(f"void rv{f}({real_c}, ...); void gv{f}(void); void gcv{f}(void);")
        c_va = []
        for j, t in enumerate(var):
            c_va.append(f"(int32_t){vs[j]}" if t == "i32" else f"(int64_t){vs[j]}" if t == "i64" else f"((UD){{.u={vs[j]}}}).d")
        c_fixed = [Q.c_arg_from_src(p, fs[k]) for k, p in enumerate(fixed)]
        CC.append(f"void wv{f}({real_c}, ...);")
        CC.append(f"void gcv{f}(void) {{ wv{f}({', '.join(c_fixed + c_va)}); }}")
        RC.append(f'#[no_mangle] pub unsafe extern "C" fn rv{f}({real_r}, mut ap: ...) {{ {" ".join(rbody + rbody_va)} }}')
        RX.append(f"    fn wv{f}({real_r}, ...);")
        r_va = [f"{vs[j]} as i32" if t == "i32" else f"{vs[j]} as i64" if t == "i64" else f"transmute::<u64, f64>({vs[j]})" for j, t in enumerate(var)]
        RR.append(f'#[no_mangle] pub unsafe extern "C" fn gv{f}() {{ wv{f}({", ".join([Q.r_arg_from_src(p, fs[k]) for k, p in enumerate(fixed)] + r_va)}); }}')
        lines = [f"static void tv{f}(void) {{", f"  poison(); rv{f}({', '.join(c_fixed + c_va)});"]
        lines += [f'  chk("va:gcc->rust", 0x100 + {f}, "{w}", {o}, {e});' for o, c, e, w in checks]
        lines += [f"  {c} = 0x5A5A5A5A;" for o, c, e, w in checks]
        lines.append(f"  poison(); gv{f}();")
        lines += [f'  chk("va:rust->gcc", 0x100 + {f}, "{w}", {c}, {e});' for o, c, e, w in checks]
        lines += [f"  {c} = 0x5A5A5A5A;" for o, c, e, w in checks]
        lines.append(f"  poison(); gcv{f}();")
        lines += [f'  chk("va:gcc->gcc(control)", 0x100 + {f}, "{w}", {c}, {e});' for o, c, e, w in checks]
        lines.append("}")
        H += lines
        calls.append(f"  tv{f}();")
    return H, calls, CC, RC, RX, RR

def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if p.returncode:
        raise RuntimeError(f"FAILED ({p.returncode}): {' '.join(cmd)}\n{p.stdout[-2500:]}\n{p.stderr[-2500:]}")
    return p

def retarget_ir(path, cpu):
    s = open(path, encoding="utf-8").read()
    s = re.sub(r'"target-cpu"="[^"]*"', f'"target-cpu"="{cpu}"', s)
    s = re.sub(r'\s*"target-features"="[^"]*"', "", s)
    open(path, "w", encoding="utf-8", newline="\n").write(s)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rustc"); ap.add_argument("llc"); ap.add_argument("chip", choices=list(CHIPS)); ap.add_argument("out")
    ap.add_argument("--n", type=int, default=120); ap.add_argument("--nva", type=int, default=60)
    ap.add_argument("--seed", type=int, default=278); ap.add_argument("--json")
    ap.add_argument("--ropt", default="3"); ap.add_argument("--lopt", default="O2")
    ap.add_argument("--no-odd", action="store_true", help="drop every signature containing S5/S6/S7")
    ap.add_argument("--compare-llc", help="also compile the Rust IR with this llc and compare the asm")
    a = ap.parse_args()
    ch = CHIPS[a.chip]
    os.makedirs(a.out, exist_ok=True)
    P = lambda n: os.path.join(a.out, n)
    rng = random.Random(a.seed)
    sigs = gen_sigs(rng, a.n)
    sigs = [["u32"] * 5 + ["S6", "u16"], ["u32"] * 5 + ["S5", "i8", "bool"], ["u32"] * 3 + ["i64", "u16", "u32"],
            ["u32"] * 6 + ["u16", "i16", "u8", "i8", "bool"], ["u16", "i16", "u8", "i8", "bool", "u16", "i8", "i16"],
            ["u32", "u16", "u16", "u32", "u16", "u16", "u16", "u16", "u16", "u16", "u16", "u16", "i32", "i32", "u16"],
            ["u32"] * 6 + ["S6"], ["u32"] * 6 + ["S5"], ["u32"] * 6 + ["S7"], ["u32"] * 5 + ["S6"]] + sigs  # odd byval last
    vs = va_sigs(rng, a.nva)
    # twins: every signature containing S5/S6/S7 gets a copy with those replaced by S8.
    # Under a correct fix both occupy two words, so all other arguments land in the same place.
    twin = lambda xs: ["S8" if x in ("S5", "S6", "S7") else x for x in xs]
    odd_sigs = [s for s in sigs if {"S5", "S6", "S7"} & set(s)]
    odd_vs = [v for v in vs if {"S5", "S6", "S7"} & set(v[0])]
    sigs = odd_sigs + [twin(s) for s in odd_sigs]
    vs = odd_vs + [(twin(v[0]), v[1]) for v in odd_vs]
    res_pairs = dict(plain=len(odd_sigs), va=len(odd_vs))
    if a.no_odd:
        odd = {"S5", "S6", "S7"}
        sigs = [s for s in sigs if not odd & set(s)]
        vs = [v for v in vs if not odd & set(v[0])]
    harness, ccaller, rcallee, rcaller = Q.emit(sigs, rng)
    H, calls, CC, RC, RX, RR = emit_va(vs, rng)
    harness = harness.replace("int main(void) {", "\n".join(H) + "\nint main(void) {\n" + "\n".join(calls), 1)
    ccaller += "\n".join(CC) + "\n"
    rcallee = rcallee.replace("#![feature(no_core, lang_items)]", "#![feature(no_core, lang_items, c_variadic)]") + "\n".join(RC) + "\n"
    rcaller = rcaller.replace("#![feature(no_core, lang_items)]", "#![feature(no_core, lang_items, c_variadic)]")
    rcaller = rcaller.replace('extern "C" {', 'extern "C" {\n' + "\n".join(RX), 1) + "\n".join(RR) + "\n"
    for n, t in (("harness.c", harness), ("ccaller.c", ccaller), ("rcallee.rs", rcallee), ("rcaller.rs", rcaller), ("libc.c", Q.LIBC), ("link.ld", Q.LDSCRIPT)):
        open(P(n), "w").write(t)
    start = Q.START[ch["abi"]] + Q.VECTORS
    if not ch["fpu"]:
        start = start.replace("    movi a2, 1           /* enable coprocessor 0 (FPU) */\n    wsr a2, cpenable\n", "")
    open(P("start.S"), "w").write(start)
    gcc = os.path.join(GCCBIN, ch["gcc"])
    gabi = ["-mabi=call0"] if ch["abi"] == "call0" else []
    res = dict(pairs=res_pairs, chip=a.chip, seed=a.seed, rustc=a.rustc, llc=a.llc, sigs=len(sigs), vasigs=len(vs))
    try:
        for src in ("harness.c", "ccaller.c", "libc.c"):
            run([gcc, "-O2", "-ffreestanding", "-fno-builtin", "-fno-tree-loop-distribute-patterns", "-mtext-section-literals", "-mlongcalls", "-c"] + gabi + [P(src), "-o", P(src + ".o")])
        run([gcc] + gabi + ["-c", P("start.S"), "-o", P("start.o")])
        mc = P("libminicore.rlib")
        run([a.rustc, "--target", ch["target"], "--crate-type", "rlib", f"-Copt-level={a.ropt}", "-Cpanic=abort", "-Awarnings",
             os.path.join(REPO, "tests", "auxiliary", "minicore.rs"), "-o", mc])
        verify_bad, abi_ok = [], True
        for src in ("rcallee.rs", "rcaller.rs"):
            ll = P(src + ".ll")
            run([a.rustc, "--target", ch["target"], f"-Copt-level={a.ropt}", "-Cpanic=abort", "-Awarnings", "--extern", f"minicore={mc}",
                 "--emit", "llvm-ir", P(src), "-o", ll])
            if ch["cpu"]:
                retarget_ir(ll, ch["cpu"])
            run([a.llc, f"-{a.lopt}", "-filetype=obj", ll, "-o", P(src + ".o")])
            asm = run([a.llc, f"-{a.lopt}", ll, "-o", "-"]).stdout
            if a.compare_llc:
                other = run([a.compare_llc, f"-{a.lopt}", ll, "-o", "-"]).stdout
                res.setdefault("asm_identical_to_compare_llc", True)
                if other != asm:
                    res["asm_identical_to_compare_llc"] = False
                    fa = dict(re.findall(r"^(\w+):\s*#.*?\n(.*?)^\.Lfunc_end", asm, re.M | re.S))
                    fb = dict(re.findall(r"^(\w+):\s*#.*?\n(.*?)^\.Lfunc_end", other, re.M | re.S))
                    res.setdefault("asm_diff_functions", []).extend(sorted(k for k in set(fa) | set(fb) if fa.get(k) != fb.get(k)))
            has_entry = re.search(r"^\s+entry\s+a1,", asm, re.M) is not None
            abi_ok &= has_entry == (ch["abi"] == "windowed")
            v = subprocess.run([a.llc, f"-{a.lopt}", "-verify-machineinstrs", ll, "-o", os.devnull], capture_output=True, text=True, encoding="utf-8", errors="replace")
            verify_bad += sorted(set(re.findall(r"- function:\s+(\S+)", v.stderr)))
            if v.returncode and not verify_bad:
                verify_bad.append(f"<llc exit {v.returncode}>")
        res["abi_ok"] = abi_ok
        res["verifier_bad_functions"] = verify_bad
        run([gcc, "-nostdlib", "-nostartfiles", "-T", P("link.ld"), "-Wl,--no-relax", "-Wl,--gc-sections",
             P("start.o"), P("harness.c.o"), P("ccaller.c.o"), P("rcallee.rs.o"), P("rcaller.rs.o"), P("libc.c.o"), "-o", P("test.elf")]
            + gabi + ["-lgcc"])
        try:
            p = subprocess.run([Q.QEMU, "-M", "sim", "-cpu", "de233_fpu", "-display", "none", "-monitor", "none", "-serial", "none",
                                "-semihosting", "-kernel", P("test.elf")], capture_output=True, text=True, timeout=180,
                               encoding="utf-8", errors="replace")
            out = p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            out = "TIMEOUT"
        open(P("qemu.out"), "w", encoding="utf-8").write(out)
        lines = out.splitlines()
        done = [l for l in lines if l.startswith("DONE")]
        mism = [l for l in lines if l.startswith("MISMATCH")]
        res["done"] = bool(done)
        res["exception"] = "EXCEPTION" in out
        if done:
            c, fl = done[0].split()[1:3]
            res["checks"] = int(c.split("=")[1], 16); res["fails"] = int(fl.split("=")[1], 16)
        by_path = {}
        for l in mism:
            path = l.split()[1].split("=")[1]; fid = int(l.split()[2][1:], 16)
            sig = (f"va{fid - 0x100}: " + " , ".join(" ".join(x) for x in vs[fid - 0x100])) if fid >= 0x100 else f"f{fid}: " + " ".join(sigs[fid])
            by_path.setdefault(path, {}).setdefault(sig, []).append(" ".join(l.split()[3:]))
        res["mismatches"] = {k: {s: v[:4] for s, v in list(d.items())[:6]} for k, d in by_path.items()}
        res["mismatch_sig_counts"] = {k: len(d) for k, d in by_path.items()}
    except Exception as e:
        res["error"] = str(e)[:3000]
    print(json.dumps(res))
    if a.json:
        with open(a.json, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(res) + "\n")

if __name__ == "__main__":
    main()




