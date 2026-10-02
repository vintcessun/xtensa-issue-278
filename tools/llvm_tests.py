#!/usr/bin/env python3
"""LLVM-side tests.

  lit     the new test fails with upstream source and passes with the fix; every Xtensa lit test
          passes with the fix                                       -> results/llvm-lit.json
  check   complete check-llvm (llvm/test) in LLVM_CHECK_BUILD; every failure is re-run with the
          upstream source to tell pre-existing failures from new ones -> results/check-llvm.json

The llvm-project working tree is switched to the upstream file only for the time of a rebuild
and restored byte for byte afterwards (verified by hash).
"""
import hashlib, json, os, re, subprocess, sys, xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "harness"))
import config

SRC_FILE = os.path.join(config.LLVM_SRC, "llvm", "lib", "Target", "Xtensa", "XtensaISelLowering.cpp")
NEW_TEST = "llvm/test/CodeGen/Xtensa/calling-conv-stack-align.ll"
XTENSA_DIRS = ["llvm/test/CodeGen/Xtensa", "llvm/test/MC/Xtensa", "llvm/test/DebugInfo/Xtensa", "llvm/test/Transforms/AtomicExpand/Xtensa"]
RESULTS = os.path.join(ROOT, "results")

def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()

def ninja(build, targets, jobs=8):
    # a batch file avoids cmd.exe's quoting rules for the Visual Studio environment script
    bat = os.path.join(config.RUNS, "_ninja.bat")
    os.makedirs(config.RUNS, exist_ok=True)
    open(bat, "w").write(f'@echo off\r\ncall "{config.VCVARS}" >nul\r\nninja -C "{build}" -j{jobs} {" ".join(targets)}\r\nexit /b %errorlevel%\r\n')
    p = subprocess.run([bat], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise RuntimeError("build failed:\n" + p.stdout[-3000:] + p.stderr[-2000:])

def lit(build, paths, xunit=None, jobs=8):
    cmd = [sys.executable, os.path.join(build, "bin", "llvm-lit.py"), "-sv", f"-j{jobs}"] + (["--xunit-xml-output", xunit] if xunit else []) + paths
    p = subprocess.run(cmd, cwd=config.LLVM_SRC, capture_output=True, text=True, encoding="utf-8", errors="replace")
    total = re.search(r"Total Discovered Tests: (\d+)", p.stdout)
    failed = sorted(set(re.findall(r"^\s+(LLVM(?:-Unit)? :: \S+)", p.stdout, re.M)))
    return dict(returncode=p.returncode, total=int(total.group(1)) if total else None, failed=failed,
                summary=[l.strip() for l in p.stdout.splitlines() if re.match(r"\s*(Passed|Failed|Unsupported|Expectedly|Unexpectedly|Excluded|Skipped)", l)])

class UpstreamSource:
    """temporarily replace the fixed source by the upstream one (git HEAD), then restore it"""
    def __enter__(self):
        self.saved = open(SRC_FILE, "rb").read()
        self.hash = sha(SRC_FILE)
        up = subprocess.run(["git", "show", f"HEAD:llvm/lib/Target/Xtensa/XtensaISelLowering.cpp"], cwd=config.LLVM_SRC, capture_output=True).stdout
        open(SRC_FILE, "wb").write(up)
        os.utime(SRC_FILE, None)
        return self
    def __exit__(self, *exc):
        open(SRC_FILE, "wb").write(self.saved)
        os.utime(SRC_FILE, None)
        assert sha(SRC_FILE) == self.hash, "working tree not restored"

def cmd_lit():
    res = {}
    with UpstreamSource():
        ninja(config.LLVM_BUILD, ["llc"])
        res["new_test_with_upstream_source"] = lit(config.LLVM_BUILD, [NEW_TEST])
    ninja(config.LLVM_BUILD, ["llc", "FileCheck", "not", "count", "llvm-mc", "llvm-objdump", "llvm-readobj", "llvm-readelf", "opt"])
    res["new_test_with_fix"] = lit(config.LLVM_BUILD, [NEW_TEST])
    res["all_xtensa_with_fix"] = lit(config.LLVM_BUILD, XTENSA_DIRS)
    res["source_sha256"] = sha(SRC_FILE)
    ok = (res["new_test_with_upstream_source"]["failed"] == ["LLVM :: CodeGen/Xtensa/calling-conv-stack-align.ll"]
          and not res["new_test_with_fix"]["failed"] and not res["all_xtensa_with_fix"]["failed"])
    res["ok"] = ok
    json.dump(res, open(os.path.join(RESULTS, "llvm-lit.json"), "w"), indent=1)
    print(json.dumps({k: (v if k != "all_xtensa_with_fix" else v["summary"]) for k, v in res.items()}, indent=1))

def cmd_check():
    b = config.LLVM_CHECK_BUILD
    ninja(b, ["llvm-test-depends"])
    xunit = os.path.join(b, "check-llvm-fixed.xml")
    fixed = lit(b, ["llvm/test"], xunit=xunit)
    res = {"fixed": fixed, "source_sha256": sha(SRC_FILE)}
    rerun = [t.split(" :: ", 1)[1] for t in fixed["failed"] if t.startswith("LLVM :: ")]
    if fixed["failed"]:
        with UpstreamSource():
            ninja(b, ["llvm-test-depends"])
            res["upstream_rerun_of_failures"] = lit(b, [f"llvm/test/{t}" for t in rerun]) if rerun else None
        ninja(b, ["llvm-test-depends"])
        up = set(res["upstream_rerun_of_failures"]["failed"]) if res.get("upstream_rerun_of_failures") else set()
        res["new_failures"] = [t for t in fixed["failed"] if t.startswith("LLVM :: ") and t not in up]
        res["unit_failures"] = [t for t in fixed["failed"] if not t.startswith("LLVM :: ")]
    else:
        res["new_failures"] = []
    json.dump(res, open(os.path.join(RESULTS, "check-llvm.json"), "w"), indent=1)
    print(json.dumps({"total": fixed["total"], "failed": len(fixed["failed"]), "new_failures": res["new_failures"], "summary": fixed["summary"]}, indent=1))

if __name__ == "__main__":
    os.makedirs(RESULTS, exist_ok=True)
    {"lit": cmd_lit, "check": cmd_check}[sys.argv[1]]()
