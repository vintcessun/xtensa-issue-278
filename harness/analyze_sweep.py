#!/usr/bin/env python3
"""Summarise sweep/*.json (irsweep) and csweep/*.json (clangsweep)."""
import glob, json, os, re, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import irsweep as W

HERE = os.path.dirname(os.path.abspath(__file__))
ALL = W.cases()
sub = sys.argv[1] if len(sys.argv) > 1 else "sweep"

groups = defaultdict(list)
import config
for p in glob.glob(os.path.join(config.RUNS, sub, "*.json")):
    name = os.path.basename(p)[:-5]
    try:
        r = json.load(open(p, encoding="utf-8-sig"))
    except Exception as e:
        r = {"error": f"unreadable json: {e}"}
    m = re.match(r"(base|B)_(\w+?)_(O\d)_(\d+)(_irva)?$", name)
    if not m:
        continue
    v, chip, opt, i, irva = m.groups()
    r["_i"] = int(i)
    groups[(v, chip, opt, bool(irva))].append(r)

def glob_idx(r, local):
    ci, cn = map(int, r["chunk"].split("/"))
    return ci + local * cn

def fn_case(r, fn):
    m = re.match(r"(ircallee|ircaller|irdyn)(\d+)$", fn)
    return (m.group(1), glob_idx(r, int(m.group(2)))) if m else (fn, None)

print(f"{'variant':<6}{'chip':<12}{'opt':<4}{'irva':<5}{'chunks':>7}{'done':>5}{'exc':>4}{'err':>4}{'checks':>9}{'fails':>7}  abi  fails-by-path")
verif = {}
failsets = {}
for key in sorted(groups):
    rs = groups[key]
    n = len(rs)
    done = sum(1 for r in rs if r.get("done"))
    exc = sum(1 for r in rs if r.get("exception") or r.get("timeout"))
    err = sum(1 for r in rs if "error" in r)
    checks = sum(r.get("checks", 0) for r in rs)
    fails = sum(r.get("fails", 0) for r in rs)
    abi = all(r.get("abi_ok", True) for r in rs if "error" not in r)
    byp = defaultdict(set)
    for r in rs:
        for path, locs in (r.get("fail_cases") or {}).items():
            for l in locs:
                byp[path].add(glob_idx(r, l))
    failsets[key] = byp
    vs = set()
    for r in rs:
        for fn in r.get("verifier_bad", []):
            vs.add(fn_case(r, fn))
    verif[key] = vs
    print(f"{key[0]:<6}{key[1]:<12}{key[2]:<4}{str(key[3])[0]:<5}{n:>7}{done:>5}{exc:>4}{err:>4}{checks:>9}{fails:>7}  {'ok' if abi else 'BAD'}  "
          + ", ".join(f"{p}:{len(c)}" for p, c in sorted(byp.items())))
    for r in rs:
        if "error" in r:
            print("      ERROR chunk", r.get("chunk", r["_i"]), r["error"][:300].replace("\n", " | "))

print("\n== verifier: functions failing -verify-machineinstrs")
for key in sorted(verif):
    if key[0] != "B":
        continue
    bkey = ("base",) + key[1:]
    b, base = verif[key], verif.get(bkey)
    kinds = defaultdict(int)
    for kind, _ in b:
        kinds[kind] += 1
    line = f"  B {key[1]:<12}{key[2]} irva={key[3]}: {len(b)} functions {dict(kinds)}"
    if base is not None:
        new = b - base
        line += f" | base: {len(base)} | in B but not in base: {len(new)}"
        for kind, gi in sorted(new, key=str)[:5]:
            line += f"\n      NEW {kind} case {gi}: {ALL[gi] if gi is not None else ''}"
    print(line)

INDEX = {repr(c): i for i, c in enumerate(ALL)}
def twin(gi):
    """same case with every odd-size struct rounded up to a whole number of words (align 1)"""
    params, var = ALL[gi]
    # Under the fix an odd struct is laid out like the next word-multiple size with alignment 4
    # (byval alignment is raised to 4 anyway); prefer the align-1 twin, fall back to align 4.
    for a in (1, 4):
        tp = [("S", (p[1] + 3) // 4 * 4, a) if isinstance(p, tuple) and p[1] % 4 else p for p in params]
        if repr((tp, var)) in INDEX:
            return INDEX[repr((tp, var))]
    return None

print("\n== B failures explained? (odd struct: identical failure on its rounded twin; otherwise: same failure in base)")
for key in sorted(failsets):
    if key[0] != "B":
        continue
    cases = defaultdict(set)
    for path, gis in failsets[key].items():
        for gi in gis:
            cases[gi].add(path)
    bkey = ("base",) + key[1:]
    basecases = defaultdict(set)
    for path, gis in failsets.get(bkey, {}).items():
        for gi in gis:
            basecases[gi].add(path)
    ctl = {gi for gi, ps in cases.items() if "c->c" in ps}
    odd_ok = odd_bad = even_ok = even_unknown = 0
    unexplained = []
    for gi, ps in cases.items():
        if gi in ctl:
            continue
        has_odd = any(isinstance(p, tuple) and p[1] % 4 for p in ALL[gi][0])
        if has_odd:
            tj = twin(gi)
            if tj is not None and cases.get(tj) == ps:
                odd_ok += 1
            else:
                odd_bad += 1; unexplained.append((gi, ps, tj, cases.get(tj)))
        else:
            if bkey in failsets and basecases.get(gi) == ps:
                even_ok += 1
            else:
                even_unknown += 1; unexplained.append((gi, ps, "base:", basecases.get(gi)))
    print(f"  B {key[1]:<12}{key[2]} irva={key[3]}: failing={len(cases)} | GCC-inconsistent(excluded)={len(ctl)} | "
          f"odd-struct same-as-twin={odd_ok} | odd NOT same={odd_bad} | even same-as-base={even_ok} | even unverified={even_unknown}")
    for u in unexplained[:5]:
        print("      UNEXPLAINED:", u[0], sorted(u[1]), ALL[u[0]], "| twin/base:", u[2], u[3])

print("\n== B runtime failures (case -> paths)")
for key in sorted(failsets):
    if key[0] != "B" or not failsets[key]:
        continue
    cases = defaultdict(set)
    for path, gis in failsets[key].items():
        for gi in gis:
            cases[gi].add(path)
    print(f"  B {key[1]} {key[2]} irva={key[3]}: {len(cases)} failing cases")
    for gi in sorted(cases)[:8]:
        print(f"      case {gi}: {sorted(cases[gi])}  {ALL[gi]}")
