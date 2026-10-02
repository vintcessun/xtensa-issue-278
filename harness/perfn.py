#!/usr/bin/env python3
"""Per-function comparison of two llc builds on a sweep IR module.

The module is split into one module per function (declarations and globals kept), and each
function is compiled by both llcs with -verify-machineinstrs. Reports, per function: verifier
status for each llc and whether the generated assembly is byte-identical.

usage: perfn.py IR.ll LLC_BASE LLC_NEW MCPU OPT OUT.json
"""
import json, re, subprocess, sys, tempfile, os

ir, llc_a, llc_b, mcpu, opt, out = sys.argv[1:7]
text = open(ir, encoding="utf-8").read()
blocks = re.findall(r"^define .*?^}\n", text, re.M | re.S)
header = re.sub(r"^define .*?^}\n", "", text, flags=re.M | re.S)
res = {}
with tempfile.TemporaryDirectory() as td:
    for blk in blocks:
        name = re.match(r"define [^@]*@(\w+)", blk).group(1)
        path = os.path.join(td, name + ".ll")
        open(path, "w", encoding="utf-8").write(header + blk)
        entry = {}
        for tag, llc in (("base", llc_a), ("new", llc_b)):
            p = subprocess.run([llc, f"-{opt}", f"-mcpu={mcpu}", "-verify-machineinstrs", path, "-o", "-"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            errs = sorted(set(re.findall(r"\*\*\* Bad machine code: (.*?) \*\*\*", p.stderr)))
            entry[tag + "_ok"] = p.returncode == 0
            entry[tag + "_errs"] = errs
            entry[tag + "_asm"] = re.sub(r"^\s*\.file.*$", "", p.stdout, flags=re.M) if p.returncode == 0 else None
        if entry["base_asm"] is None or entry["new_asm"] is None:
            # verifier aborted: compare without the verifier
            for tag, llc in (("base", llc_a), ("new", llc_b)):
                p = subprocess.run([llc, f"-{opt}", f"-mcpu={mcpu}", path, "-o", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
                entry[tag + "_asm"] = re.sub(r"^\s*\.file.*$", "", p.stdout, flags=re.M)
        entry["asm_identical"] = entry["base_asm"] == entry["new_asm"]
        del entry["base_asm"], entry["new_asm"]
        res[name] = entry
json.dump(res, open(out, "w"), indent=0)
print(len(res), "functions;", sum(1 for e in res.values() if not e["asm_identical"]), "differ;",
      sum(1 for e in res.values() if not e["base_ok"]), "base-verifier-fail;", sum(1 for e in res.values() if not e["new_ok"]), "new-verifier-fail")
