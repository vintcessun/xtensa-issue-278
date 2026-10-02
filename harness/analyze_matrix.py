#!/usr/bin/env python3
"""Summarise the QEMU matrix (matrix/*.json + run dirs)."""
import glob, json, os, re, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
import config
M = os.path.join(config.RUNS, "matrix")
ODD = {"S5", "S6", "S7"}

runs = []
for p in sorted(glob.glob(os.path.join(M, "*.json"))):
    try:
        r = json.load(open(p, encoding="utf-8-sig"))
    except Exception as e:
        r = {"error": f"bad json {e}"}
    r["name"] = os.path.basename(p)[:-5]
    r["dir"] = os.path.join(M, r["name"])
    runs.append(r)

def fn_types(run_dir):
    """function name -> set of param type names, from the generated Rust sources"""
    m = {}
    for f in ("rcallee.rs", "rcaller.rs"):
        path = os.path.join(run_dir, f)
        if not os.path.exists(path):
            continue
        for name, params in re.findall(r"fn (\w+)\(([^)]*)\)", open(path, encoding="utf-8").read()):
            m[name] = set(re.findall(r":\s*(\w+)", params))
    # callers g<f>/gv<f> take no params: use the callee they call (w<f>/wv<f>)
    for name in list(m):
        if name.startswith("w"):
            m["g" + name[1:]] = m[name]
    return m

def sig_text(s):
    return re.sub(r"^(va|f)\d+: ", r"\1: ", s)

_sig_cache = {}
def sig_names(run_dir):
    """id -> canonical signature text, rebuilt from the generated Rust callee source"""
    if run_dir in _sig_cache:
        return _sig_cache[run_dir]
    src = open(os.path.join(run_dir, "rcallee.rs"), encoding="utf-8").read()
    m = {}
    for f, params in re.findall(r"fn r(\d+)\(([^)]*)\)", src):
        m[int(f)] = "f: " + " ".join(re.findall(r":\s*(\w+)", params))
    for f, params, body in re.findall(r"fn rv(\d+)\((.*?), mut ap: \.\.\.\) \{(.*?)\}\s*$", src, re.M):
        fixed = " ".join(re.findall(r":\s*(\w+)", params))
        var = " ".join(re.findall(r"next_arg::<(\w+)>", body))
        m[0x100 + int(f)] = f"va: {fixed} , {var}"
    _sig_cache[run_dir] = m
    return m

def failures(r):
    out = set()
    q = os.path.join(r["dir"], "qemu.out")
    if not os.path.exists(q):
        return out
    names = sig_names(r["dir"])
    for l in open(q, encoding="utf-8", errors="replace"):
        if l.startswith("MISMATCH"):
            parts = l.split()
            path = parts[1].split("=")[1]
            fid = int(parts[2][1:], 16)
            out.add((path, names.get(fid, f"?{fid}")))
    return out

print(f"{'run':<22} {'done':<5} {'exc':<5} {'checks':>6} {'fails':>5}  abi  verifier-bad  asm-vs-compare")
for r in runs:
    if "error" in r and not r.get("done"):
        print(f"{r['name']:<22} ERROR {r['error'][:150]}")
        continue
    asm = ""
    if "asm_identical_to_compare_llc" in r:
        if r["asm_identical_to_compare_llc"]:
            asm = "identical"
        else:
            types = fn_types(r["dir"])
            diff = r.get("asm_diff_functions", [])
            non_odd = [f for f in diff if not (types.get(f, set()) & ODD)]
            asm = f"{len(diff)} fns differ, {len(non_odd)} without odd struct" + (f" {non_odd[:6]}" if non_odd else "")
    vb = r.get("verifier_bad_functions", [])
    print(f"{r['name']:<22} {str(r.get('done')):<5} {str(r.get('exception')):<5} {str(r.get('checks','-')):>6} {str(r.get('fails','-')):>5}  "
          f"{'ok' if r.get('abi_ok') else 'BAD'}  {len(vb):>3} {vb[:4]}  {asm}")

# failure comparison: B-based runs vs original baseline
by = defaultdict(dict)
for r in runs:
    by[(r.get("chip"), r.get("seed"))][r.get("group")] = r
print("\n== failures of fixed-rustc + llc-B (R1) not present in baselines R3 (orig+base) / R6 (fixed+base) ==")
new_total = 0
for key, g in sorted(by.items(), key=lambda kv: str(kv[0])):
    if "R1" not in g:
        continue
    f1 = failures(g["R1"])
    base = set()
    for b in ("R3", "R6"):
        if b in g:
            base |= failures(g[b])
    # R6 only exists for seed 1; R3 exists for all seeds
    new = {x for x in f1 if x not in base}
    new_odd = {x for x in new if ODD & set(re.findall(r"\w+", x[1]))}
    new_plain = new - new_odd
    new_total += len(new_plain)
    print(f"  {key}: R1 failing sigs={len(f1)}  not-in-baseline={len(new)} (with odd struct: {len(new_odd)}, without: {len(new_plain)})")
    for x in sorted(new_plain)[:5]:
        print("      NEW (no odd struct):", x)
print(f"total new failures on signatures without odd structs: {new_total}")

print("\n== all distinct failing (path, signature) across R1 runs ==")
agg = defaultdict(set)
for r in runs:
    if r.get("group") == "R1":
        for x in failures(r):
            agg[x].add(f"{r['chip']}/s{r['seed']}")
for (path, s), where in sorted(agg.items()):
    print(f"  {path:<14} {s:<70} {len(where)} runs")
