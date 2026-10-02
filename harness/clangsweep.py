#!/usr/bin/env python3
"""Same sweep as irsweep.py, but the LLVM side is C compiled by esp-clang to LLVM IR
(clang's own Xtensa ABI lowering and va_arg expansion), then turned into an object by the
llc under test. The GCC side, harness, sources and checks are identical to irsweep.py.

usage: clangsweep.py CLANG LLC CHIP OUTDIR [--opt O2] [--chunk I/N]
"""
import argparse, json, os, random, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import irsweep as W
import qemutest as Q

def c_side(case_list):
    shapes_used = {(p[1], p[2]) for c, _ in case_list for p in c if W.is_s(p)}
    out = [Q.C_PRELUDE, "#include <stdarg.h>", "#include <stddef.h>", W.c_struct_defs(shapes_used),
           "uint32_t csum(const void *p, int n);", "extern volatile uint32_t dyn_n;"]
    for ci, (params, var) in enumerate(case_list):
        ctype = lambda p: W.sname(p[1], p[2]) if W.is_s(p) else W.SC[p][1]
        srct = lambda p: W.sname(p[1], p[2]) if W.is_s(p) else ("uint64_t" if W.SC[p][3] == 64 else "uint32_t")
        srcs = [f"s{ci}_{k}" for k in range(len(params))]
        vsrc = [f"v{ci}_{j}" for j in range(len(var or []))]
        for k, p in enumerate(params):
            out.append(f"extern {srct(p)} {srcs[k]};")
        for j, t in enumerate(var or []):
            out.append(f"extern {'uint32_t' if t == 'i32' else 'uint64_t'} {vsrc[j]};")
        words = []
        for k, p in enumerate(params):
            if W.is_s(p) or p not in ("i64", "f64"): words.append(f"p{k}")
            else: words += [f"p{k}lo", f"p{k}hi"]
        for j, t in enumerate(var or []):
            words += [f"va{j}"] if t == "i32" else [f"va{j}lo", f"va{j}hi"]
        out.append(" ".join(f"extern volatile uint32_t o{ci}_{w};" for w in words))
        proto = ", ".join(f"{ctype(p)} p{k}" for k, p in enumerate(params)) + (", ..." if var else "")
        out.append(f"void w{ci}({proto});")
        body = []
        for k, p in enumerate(params):
            if W.is_s(p): body.append(f"o{ci}_p{k} = csum(&p{k}, {p[1]});")
            elif p in ("i64", "f64"):
                body.append(f"{{ union {{ {W.SC[p][1]} x; uint64_t u; }} t = {{ .x = p{k} }}; o{ci}_p{k}lo = (uint32_t)t.u; o{ci}_p{k}hi = (uint32_t)(t.u >> 32); }}")
            elif p == "f32": body.append(f"o{ci}_p{k} = ((UF){{.f = p{k}}}).u;")
            elif p == "ptr": body.append(f"o{ci}_p{k} = (uint32_t)(size_t)p{k};")
            else: body.append(f"o{ci}_p{k} = p{k};")
        if var:
            body.append(f"va_list ap; va_start(ap, p{len(params) - 1});")
            for j, t in enumerate(var):
                if t == "i32": body.append(f"o{ci}_va{j} = (uint32_t)va_arg(ap, int32_t);")
                elif t == "i64": body.append(f"{{ uint64_t t = va_arg(ap, uint64_t); o{ci}_va{j}lo = (uint32_t)t; o{ci}_va{j}hi = (uint32_t)(t >> 32); }}")
                else: body.append(f"{{ UD t = {{ .d = va_arg(ap, double) }}; o{ci}_va{j}lo = (uint32_t)t.u; o{ci}_va{j}hi = (uint32_t)(t.u >> 32); }}")
            body.append("va_end(ap);")
        out.append(f"void ircallee{ci}({proto}) {{ {' '.join(body)} }}")
        def c_arg(p, s):
            if W.is_s(p): return s
            if p == "f32": return f"((UF){{.u = {s}}}).f"
            if p == "f64": return f"((UD){{.u = {s}}}).d"
            if p == "ptr": return f"(void *)(size_t){s}"
            if p == "b": return f"(bool)({s} & 1)"
            return f"({W.SC[p][1]}){s}"
        c_va = [(f"(int32_t){vsrc[j]}" if t == "i32" else f"(int64_t){vsrc[j]}" if t == "i64" else f"((UD){{.u = {vsrc[j]}}}).d") for j, t in enumerate(var or [])]
        args = ", ".join([c_arg(p, srcs[k]) for k, p in enumerate(params)] + c_va)
        out.append(f"void ircaller{ci}(void) {{ w{ci}({args}); }}")
        out.append(f"void irdyn{ci}(void) {{ volatile char *d = __builtin_alloca(dyn_n); d[0] = 1; "
                   f"w{ci}({args}); w{ci}({args}); w{ci}({args}); d[0] = 2; }}")
    return "\n".join(out) + "\n"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clang"); ap.add_argument("llc"); ap.add_argument("chip", choices=["esp32", "esp32s2", "esp32s3", "esp8266real"]); ap.add_argument("out")
    ap.add_argument("--opt", default="O2"); ap.add_argument("--chunk", default="0/1"); ap.add_argument("--seed", type=int, default=278)
    ap.add_argument("--cases", help="file with global case indices to run instead of a chunk")
    a = ap.parse_args()
    ci, cn = map(int, a.chunk.split("/"))
    sel = W.cases()[ci::cn]
    if a.cases:
        sel = [W.cases()[int(x)] for x in open(a.cases).read().split()]
    ch = W.CHIPS[a.chip]
    os.makedirs(a.out, exist_ok=True)
    P = lambda n: os.path.join(a.out, n)
    rng = random.Random(a.seed * 1000 + ci)
    h, cc, _ = W.build(sel, rng, False)          # identical harness/sources/GCC side
    real8266 = ch["gcc"] == "LX106"
    for n, tx in (("harness.c", h), ("ccaller.c", cc), ("clangside.c", c_side(sel)), ("libc.c", Q.LIBC),
                  ("link.ld", W.LX106_LD if real8266 else Q.LDSCRIPT)):
        open(P(n), "w", newline="\n").write(tx)
    start = W.LX106_START if real8266 else Q.START[ch["abi"]] + Q.VECTORS
    if not ch["fpu"] and not real8266:
        start = start.replace("    movi a2, 1           /* enable coprocessor 0 (FPU) */\n    wsr a2, cpenable\n", "")
    open(P("start.S"), "w").write(start)
    gcc = W.LX106_GCC if real8266 else os.path.join(W.GCCBIN, ch["gcc"])
    res = dict(chip=a.chip, opt=a.opt, chunk=a.chunk, llc=a.llc, cases=len(sel), frontend="esp-clang")
    try:
        for src in ("harness.c", "ccaller.c", "libc.c"):
            W.run([gcc, "-O2", "-ffreestanding", "-fno-builtin", "-fno-tree-loop-distribute-patterns", "-mtext-section-literals", "-mlongcalls", "-c",
                   P(src), "-o", P(src + ".o")])
        W.run([gcc, "-c", P("start.S"), "-o", P("start.o")])
        W.run([a.clang, "--target=xtensa-esp-elf", f"-mcpu={ch['mcpu']}", f"-{a.opt}", "-ffreestanding", "-fno-builtin",
               "-S", "-emit-llvm", P("clangside.c"), "-o", P("ir.ll")])
        llc = [a.llc, f"-{a.opt}", f"-mcpu={ch['mcpu']}", P("ir.ll")]
        W.run(llc + ["-filetype=obj", "-o", P("ir.o")])
        asm = W.run(llc + ["-o", "-"]).stdout
        res["abi_ok"] = (re.search(r"^\s+entry\s+a1,", asm, re.M) is not None) == (ch["abi"] == "windowed")
        v = subprocess.run(llc + ["-verify-machineinstrs", "-o", os.devnull], capture_output=True, text=True, encoding="utf-8", errors="replace")
        res["verifier_bad"] = sorted(set(re.findall(r"- function:\s+(\S+)", v.stderr)))
        W.run([gcc, "-nostdlib", "-nostartfiles", "-T", P("link.ld"), "-Wl,--no-relax", "-Wl,--gc-sections",
               P("start.o"), P("harness.c.o"), P("ccaller.c.o"), P("ir.o"), P("libc.c.o"), "-o", P("test.elf"), "-lgcc"])
        try:
            p = subprocess.run([Q.QEMU, "-M", "sim", "-cpu", ch.get("qemu_cpu", "de233_fpu"), "-display", "none", "-monitor", "none",
                                "-serial", "none", "-semihosting", "-kernel", P("test.elf")], capture_output=True, text=True, timeout=300,
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
        per = {}
        for l in out.splitlines():
            if l.startswith("MISMATCH"):
                pp = l.split(); per.setdefault(pp[1].split("=")[1], set()).add(int(pp[2][1:], 16))
        res["fail_cases"] = {k: sorted(v) for k, v in per.items()}
        res["case_sigs"] = {str(i): repr(sel[i]) for vs in per.values() for i in vs}
    except Exception as e:
        res["error"] = str(e)[:3000]
    print(json.dumps(res))

if __name__ == "__main__":
    main()
