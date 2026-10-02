#!/usr/bin/env python3
"""Compare odd-struct signatures with their S8 twins (twin/*.json + run dirs)."""
import glob, json, os, re
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
import config
T = os.path.join(config.RUNS, "twin")

def parse(run_dir):
    fails = defaultdict(set)
    for l in open(os.path.join(run_dir, "qemu.out"), encoding="utf-8", errors="replace"):
        if l.startswith("MISMATCH"):
            p = l.split()
            fails[int(p[2][1:], 16)].add((p[1].split("=")[1], p[3]))
    return fails

def odd_param_idx(run_dir, fid):
    src = open(os.path.join(run_dir, "rcallee.rs"), encoding="utf-8").read()
    pat = rf"fn r{fid}\(([^)]*)\)" if fid < 0x100 else rf"fn rv{fid - 0x100}\((.*?), mut ap"
    m = re.search(pat, src)
    types = re.findall(r":\s*(\w+)", m.group(1))
    return {k for k, t in enumerate(types) if t in ("S5", "S6", "S7")}, types

def strip(fs, skip):
    return {(path, w) for path, w in fs if not any(re.fullmatch(rf"p{k}[a-z]*", w) for k in skip)}

tot = defaultdict(int)
for jp in sorted(glob.glob(os.path.join(T, "*.json"))):
    r = json.load(open(jp, encoding="utf-8-sig"))
    d = jp[:-5]
    name = os.path.basename(d)
    if not r.get("done"):
        print(f"{name}: NOT DONE exc={r.get('exception')} err={str(r.get('error'))[:200]}")
        tot["not_done"] += 1
        continue
    P, V = r["pairs"]["plain"], r["pairs"]["va"]
    F = parse(d)
    plain_bad = []
    for i in range(P):
        skip, types = odd_param_idx(d, i)
        if F.get(i):
            plain_bad.append((i, " ".join(types), sorted(F[i])[:4]))
        if F.get(i + P):
            plain_bad.append((i + P, "(twin)", sorted(F[i + P])[:4]))
    va_same = va_diff = va_ctl = va_clean = 0
    diffs = []
    for i in range(V):
        a, b = 0x100 + i, 0x100 + i + V
        skip, types = odd_param_idx(d, a)
        fa, fb = strip(F.get(a, set()), skip), strip(F.get(b, set()), skip)
        if any(p.startswith("va:gcc->gcc") for p, _ in F.get(a, set()) | F.get(b, set())):
            va_ctl += 1          # GCC disagrees with itself: no reference, excluded
            continue
        if not fa and not fb:
            va_clean += 1
        if fa == fb:
            va_same += 1
        else:
            va_diff += 1
            diffs.append((" ".join(types), sorted(fa - fb)[:4], sorted(fb - fa)[:4]))
    print(f"{name}: checks={r.get('checks')} fails={r.get('fails')} verifier_bad={len(r.get('verifier_bad_functions', []))} abi_ok={r.get('abi_ok')} | "
          f"plain odd pairs={P}, failing={len(plain_bad)} | va odd pairs={V}: identical-to-twin={va_same} (of which fully clean={va_clean}), "
          f"DIFFERENT={va_diff}, excluded(GCC inconsistent)={va_ctl}")
    for x in plain_bad[:4]:
        print("     PLAIN FAIL:", x)
    for x in diffs[:4]:
        print("     VA DIFF:", x)
    tot["runs"] += 1; tot["plain_pairs"] += P; tot["plain_bad"] += len(plain_bad)
    tot["va_pairs"] += V; tot["va_same"] += va_same; tot["va_diff"] += va_diff; tot["va_ctl"] += va_ctl; tot["va_clean"] += va_clean
print("TOTAL", dict(tot))
