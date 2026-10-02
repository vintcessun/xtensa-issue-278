#!/usr/bin/env python3
"""Run (or resume) the whole test suite for esp-rs/rust#278.

Every stage expands into items; an item is done only when its result file validates.
Finished items are skipped, failed ones are retried immediately (up to 3 times), and the
run ends with a list of anything still incomplete (which must be empty).

usage: python run_all.py [--jobs N] [--stages a,b,...] [--list]
"""
import argparse, concurrent.futures as cf, json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.abspath(__file__))
H = os.path.join(ROOT, "harness")
sys.path.insert(0, H)
import config

PY = sys.executable
RUNS = config.RUNS
LOG = os.path.join(RUNS, "run_all.log")
N2 = 60          # chunks of the extended sweep

def log(msg):
    os.makedirs(RUNS, exist_ok=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    open(LOG, "a", encoding="utf-8").write(line + "\n")

def load(p):
    try:
        return json.load(open(p, encoding="utf-8-sig"))
    except Exception:
        return None

def llc(v):
    return {"base": config.LLC_BASE, "B": config.LLC_FIXED, "A": config.LLC_A, "AB": config.LLC_AB}[v]

# --------------------------------------------------------------------------- stage: extended sweep (irsweep2)
def v2_lists():
    """case-index lists for the base-vs-fixed comparison (cases the fix must not change)"""
    import irsweep2 as S
    allc = S.all_cases()
    unaffected = [i for i, c in enumerate(allc) if not S.is_odd_case(c)]
    d = os.path.join(RUNS, "v2lists"); os.makedirs(d, exist_ok=True)
    K = 24
    for k in range(K):
        p = os.path.join(d, f"u_{k}.txt")
        if not os.path.exists(p):
            open(p, "w").write(" ".join(map(str, unaffected[k::K])))
    return K

def stage_v2():
    items = []
    for chip in ("esp32", "esp32s2", "esp32s3", "call0", "esp8266", "esp8266real"):
        opts = ("O0", "O1", "O2", "O3") if chip == "esp32s3" else ("O0", "O2")
        for opt in opts:
            for i in range(N2):
                name = f"B_{chip}_{opt}_{i}"
                items.append(dict(stage="v2", name=name, out=os.path.join(RUNS, "sweep2", name), must_finish=True,
                                  cmd=[PY, os.path.join(H, "irsweep2.py"), llc("B"), chip, os.path.join(RUNS, "sweep2", name), "--opt", opt, "--chunk", f"{i}/{N2}"]))
    K = v2_lists()
    for chip, opts in (("esp32s3", ("O0", "O2")), ("esp8266real", ("O2",)), ("call0", ("O2",))):
        for opt in opts:
            for k in range(K):
                for v in ("base", "B"):
                    name = f"{v}_{chip}_{opt}_u{k}"
                    items.append(dict(stage="v2cmp", name=name, out=os.path.join(RUNS, "sweep2cmp", name), must_finish=True,
                                      cmd=[PY, os.path.join(H, "irsweep2.py"), llc(v), chip, os.path.join(RUNS, "sweep2cmp", name), "--opt", opt,
                                           "--cases", os.path.join(RUNS, "v2lists", f"u_{k}.txt")]))
    return items

def stage_perfn2():
    items = []
    for i in range(N2):
        src = os.path.join(RUNS, "sweep2", f"B_esp32s3_O2_{i}", "ir.ll")
        for mcpu, opt in (("esp32s3", "O2"), ("esp32s3", "O0"), ("esp8266", "O2")):
            tag = f"v2_{mcpu}_{opt}_{i}"
            out = os.path.join(RUNS, "perfn2", tag + ".json")
            items.append(dict(stage="perfn2", name=tag, out=out, kind="json", needs=src,
                              cmd=[PY, os.path.join(H, "perfn.py"), src, llc("base"), llc("B"), mcpu, opt, out]))
    return items

# --------------------------------------------------------------------------- earlier stages (Rust-level and first sweeps)
CHIPS5 = ("esp32", "esp32s2", "esp32s3", "call0", "esp8266")
KNOWN_LLC_CRASH = "Cannot scavenge register without an emergency spill slot"   # pre-existing, see docs/06

def q(script):
    return os.path.join(H, script)

def stage_rust():
    """Rust caller/callee against GCC: random signatures, varargs, twins, several opt levels"""
    items = []
    groups = {"R1": ("fixed", "B", ["--compare-llc", llc("base")]), "R2": ("fixed", "AB", ["--compare-llc", llc("B")]),
              "R3": ("orig", "base", ["--no-odd"]), "R4": ("orig", "B", ["--compare-llc", llc("base")])}
    rc = {"fixed": config.RUSTC_FIXED, "orig": config.RUSTC_ORIG}
    for chip in CHIPS5:
        for g, (r, v, extra) in groups.items():
            for seed in (1, 2, 3):
                name = f"{g}_{chip}_s{seed}"
                items.append(dict(stage="rust", name=name, out=os.path.join(RUNS, "matrix", name),
                                  cmd=[PY, q("qemu_matrix.py"), rc[r], llc(v), chip, os.path.join(RUNS, "matrix", name), "--seed", str(seed), "--n", "120", "--nva", "60"] + extra))
        for g, (r, v, extra) in {"R5": ("fixed", "A", []), "R6": ("fixed", "base", ["--no-odd"])}.items():
            name = f"{g}_{chip}_s1"
            items.append(dict(stage="rust", name=name, out=os.path.join(RUNS, "matrix", name),
                              cmd=[PY, q("qemu_matrix.py"), rc[r], llc(v), chip, os.path.join(RUNS, "matrix", name), "--seed", "1", "--n", "120", "--nva", "60"] + extra))
        for seed in (11, 12):
            name = f"B_{chip}_s{seed}"
            items.append(dict(stage="rust", name=name, out=os.path.join(RUNS, "twin", name), must_finish=True,
                              cmd=[PY, q("qemu_twin.py"), config.RUSTC_FIXED, llc("B"), chip, os.path.join(RUNS, "twin", name), "--seed", str(seed), "--n", "300", "--nva", "150"]))
        for ropt, lopt in (("0", "O0"), ("0", "O2"), ("3", "O0")):
            name = f"B_{chip}_r{ropt}_l{lopt}_s21"
            items.append(dict(stage="rust", name=name, out=os.path.join(RUNS, "twin", name), must_finish=True,
                              cmd=[PY, q("qemu_twin.py"), config.RUSTC_FIXED, llc("B"), chip, os.path.join(RUNS, "twin", name), "--seed", "21",
                                   "--n", "300", "--nva", "150", "--ropt", ropt, "--lopt", lopt]))
    return items

def even_lists_v1():
    import irsweep as W
    d = os.path.join(RUNS, "evenlists"); os.makedirs(d, exist_ok=True)
    ALL = W.cases()
    even = [i for i, (p, v) in enumerate(ALL) if not any(isinstance(x, tuple) and x[1] % 4 for x in p)]
    for k in range(8):
        f = os.path.join(d, f"even_{k}.txt")
        if not os.path.exists(f):
            open(f, "w").write(" ".join(map(str, even[k::8])))
    # cases whose only non-word-multiple aggregates are 1-3 bytes: upstream already rounds those up,
    # so the fix must not change them either
    small = [i for i, (p, v) in enumerate(ALL)
             if any(isinstance(x, tuple) and x[1] % 4 for x in p) and not any(isinstance(x, tuple) and x[1] % 4 and x[1] > 4 for x in p)]
    for k in range(4):
        f = os.path.join(d, f"small_{k}.txt")
        if not os.path.exists(f):
            open(f, "w").write(" ".join(map(str, small[k::4])))

def stage_v1():
    """first systematic IR sweep (1968 cases), base-vs-fixed on unchanged cases, esp-clang sweep"""
    even_lists_v1()
    items = []
    chips = CHIPS5 + ("esp8266real",)
    for chip in chips:
        for opt in ("O0", "O1", "O2", "O3"):
            for i in range(20):
                name = f"B_{chip}_{opt}_{i}"
                items.append(dict(stage="v1", name=name, out=os.path.join(RUNS, "sweep", name), must_finish=True,
                                  cmd=[PY, q("irsweep.py"), llc("B"), chip, os.path.join(RUNS, "sweep", name), "--opt", opt, "--chunk", f"{i}/20"]))
        for opt in ("O0", "O2"):
            for i in range(20):
                name = f"base_{chip}_{opt}_{i}"
                items.append(dict(stage="v1", name=name, out=os.path.join(RUNS, "sweep", name),
                                  cmd=[PY, q("irsweep.py"), llc("base"), chip, os.path.join(RUNS, "sweep", name), "--opt", opt, "--chunk", f"{i}/20"]))
    for chip in ("esp32s3", "call0"):
        for v in ("B", "base"):
            for i in range(20):
                name = f"{v}_{chip}_O2_{i}_irva"
                items.append(dict(stage="v1", name=name, out=os.path.join(RUNS, "sweep", name), must_finish=(v == "B"),
                                  cmd=[PY, q("irsweep.py"), llc(v), chip, os.path.join(RUNS, "sweep", name), "--opt", "O2", "--chunk", f"{i}/20", "--ir-va-arg"]))
    for chip, opt, va in (("esp32s3", "O2", False), ("esp8266real", "O2", False), ("call0", "O2", False), ("call0", "O0", False), ("call0", "O2", True)):
        for k in range(8):
            for v in ("base", "B"):
                name = f"{v}_{chip}_{opt}_even{k}" + ("_irva" if va else "")
                items.append(dict(stage="v1", name=name, out=os.path.join(RUNS, "even", name), must_finish=True,
                                  cmd=[PY, q("irsweep.py"), llc(v), chip, os.path.join(RUNS, "even", name), "--opt", opt,
                                       "--cases", os.path.join(RUNS, "evenlists", f"even_{k}.txt")] + (["--ir-va-arg"] if va else [])))
    for chip in ("esp32", "esp32s2", "esp32s3", "esp8266real"):
        for v in ("B", "base"):
            for opt in ("O0", "O2"):
                for i in range(20):
                    name = f"{v}_{chip}_{opt}_{i}"
                    known = KNOWN_LLC_CRASH if (chip == "esp8266real" and opt == "O0") else None
                    items.append(dict(stage="v1", name=name, out=os.path.join(RUNS, "csweep", name), must_finish=(v == "B" and not known), known_error=known,
                                      cmd=[PY, q("clangsweep.py"), config.ESP_CLANG, llc(v), chip, os.path.join(RUNS, "csweep", name), "--opt", opt, "--chunk", f"{i}/20"]))
    for chip in ("esp32s3", "esp8266real"):
        for v in ("base", "B"):
            for k in range(8):
                name = f"{v}_{chip}_O2_even{k}"
                items.append(dict(stage="v1", name=name, out=os.path.join(RUNS, "ceven", name), must_finish=True,
                                  cmd=[PY, q("clangsweep.py"), config.ESP_CLANG, llc(v), chip, os.path.join(RUNS, "ceven", name), "--opt", "O2",
                                       "--cases", os.path.join(RUNS, "evenlists", f"even_{k}.txt")]))
    for k in range(4):
        for v in ("base", "B"):
            for chip in ("esp32s3", "esp8266real", "call0"):
                name = f"{v}_{chip}_O2_small{k}"
                items.append(dict(stage="v1", name=name, out=os.path.join(RUNS, "evensmall", name), must_finish=True,
                                  cmd=[PY, q("irsweep.py"), llc(v), chip, os.path.join(RUNS, "evensmall", name), "--opt", "O2",
                                       "--cases", os.path.join(RUNS, "evenlists", f"small_{k}.txt")]))
            for chip in ("esp32s3", "call0"):          # LLVM's own va_arg lowering
                name = f"{v}_{chip}_O2_small{k}_irva"
                items.append(dict(stage="v1", name=name, out=os.path.join(RUNS, "evensmall", name), must_finish=True,
                                  cmd=[PY, q("irsweep.py"), llc(v), chip, os.path.join(RUNS, "evensmall", name), "--opt", "O2", "--ir-va-arg",
                                       "--cases", os.path.join(RUNS, "evenlists", f"small_{k}.txt")]))
            for chip in ("esp32s3", "esp8266real"):
                name = f"{v}_{chip}_O2_small{k}"
                items.append(dict(stage="v1", name=name, out=os.path.join(RUNS, "cevensmall", name), must_finish=True,
                                  cmd=[PY, q("clangsweep.py"), config.ESP_CLANG, llc(v), chip, os.path.join(RUNS, "cevensmall", name), "--opt", "O2",
                                       "--cases", os.path.join(RUNS, "evenlists", f"small_{k}.txt")]))
    for k in range(8):
        for v in ("base", "B"):
            name = f"{v}_esp32s3_O2_even{k}_irva"
            items.append(dict(stage="v1", name=name, out=os.path.join(RUNS, "even", name), must_finish=True,
                              cmd=[PY, q("irsweep.py"), llc(v), "esp32s3", os.path.join(RUNS, "even", name), "--opt", "O2", "--ir-va-arg",
                                   "--cases", os.path.join(RUNS, "evenlists", f"even_{k}.txt")]))
    for i in range(20):
        for mcpu in ("esp32s3", "esp8266", "generic"):
            for opt in ("O0", "O2"):
                tag = f"ir_{mcpu}_{opt}_{i}"; out = os.path.join(RUNS, "perfn", tag + ".json")
                src = os.path.join(RUNS, "sweep", f"B_esp32s3_O2_{i}", "ir.ll")
                items.append(dict(stage="v1", name=tag, out=out, kind="json", needs=src, cmd=[PY, q("perfn.py"), src, llc("base"), llc("B"), mcpu, opt, out]))
        tag = f"irva_esp32s3_O2_{i}"; out = os.path.join(RUNS, "perfn", tag + ".json"); src = os.path.join(RUNS, "sweep", f"B_esp32s3_O2_{i}_irva", "ir.ll")
        items.append(dict(stage="v1", name=tag, out=out, kind="json", needs=src, cmd=[PY, q("perfn.py"), src, llc("base"), llc("B"), "esp32s3", "O2", out]))
        for chip, mcpu in (("esp32s3", "esp32s3"), ("esp8266real", "esp8266")):
            for opt in ("O0", "O2"):
                tag = f"clang_{mcpu}_{opt}_{i}"; out = os.path.join(RUNS, "perfn", tag + ".json"); src = os.path.join(RUNS, "csweep", f"B_{chip}_{opt}_{i}", "ir.ll")
                items.append(dict(stage="v1", name=tag, out=out, kind="json", needs=src, cmd=[PY, q("perfn.py"), src, llc("base"), llc("B"), mcpu, opt, out]))
    return items

def stage_confirm():
    """every case the fix must not change that fails with the fixed LLVM is run again with both
    LLVM builds in the same configuration; the results must be identical field by field"""
    import sweepdata
    items = []
    for key in ("v1", "clang", "v2"):
        for rest, chip, opt, irva, lst in sweepdata.confirm_lists(key):
            for v in ("base", "B"):
                name = f"{v}_{rest}"; out = os.path.join(RUNS, f"confirm_{key}", name)
                if key == "clang":
                    cmd = [PY, q("clangsweep.py"), config.ESP_CLANG, llc(v), chip, out, "--opt", opt, "--cases", lst]
                else:
                    cmd = [PY, q("irsweep.py" if key == "v1" else "irsweep2.py"), llc(v), chip, out, "--opt", opt, "--cases", lst] + (["--ir-va-arg"] if irva else [])
                items.append(dict(stage="confirm", name=f"{key}/{name}", out=out, must_finish=True, cmd=cmd))
    return items

def stage_repro():
    items = []
    for step in ("01_symptom", "02_rustc_abi", "03_llvm_lowering", "04_gcc_reference"):
        out = os.path.join(ROOT, "results", "repro", step + ".txt")
        items.append(dict(stage="repro", name=step, out=out, kind="text", cmd=[PY, os.path.join(ROOT, "repro", step + ".py")]))
    return items

def stage_corpus():
    out = os.path.join(RUNS, "corpus", "compare", "totals.json")
    return [dict(stage="corpus", name="corpus", out=out, kind="json", cmd=[PY, os.path.join(ROOT, "corpus", "corpus.py"), "all"])]

def stage_lit():
    out = os.path.join(ROOT, "results", "llvm-lit.json")
    return [dict(stage="lit", name="llvm-lit", out=out, kind="json", cmd=[PY, os.path.join(ROOT, "tools", "llvm_tests.py"), "lit"])]

def stage_checkllvm():
    out = os.path.join(ROOT, "results", "check-llvm.json")
    return [dict(stage="checkllvm", name="check-llvm", out=out, kind="json", cmd=[PY, os.path.join(ROOT, "tools", "llvm_tests.py"), "check"])]

STAGES = {"repro": stage_repro, "lit": stage_lit, "rust": stage_rust, "v1": stage_v1, "v2": stage_v2, "perfn2": stage_perfn2,
          "confirm": stage_confirm, "corpus": stage_corpus, "checkllvm": stage_checkllvm}

# --------------------------------------------------------------------------- generic item handling
def valid(it):
    if it.get("kind") == "json":
        r = load(it["out"])
        return (isinstance(r, dict) and len(r) > 0), "missing/empty result"
    if it.get("kind") == "text":
        return (os.path.exists(it["out"]) and os.path.getsize(it["out"]) > 0), "missing output"
    r = load(it["out"] + ".json")
    if r is None:
        return False, "no/invalid json"
    if "error" in r:
        if it.get("known_error") and it["known_error"] in r["error"]:
            return True, "known pre-existing LLVM error"
        return False, "error: " + r["error"][:300].replace("\n", " | ")
    if it.get("must_finish") and not r.get("done"):
        return False, f"run did not reach DONE (exception={r.get('exception')}, timeout={r.get('timeout')})"
    if not (r.get("done") or r.get("exception")):
        return False, "run neither finished nor stopped at a hardware exception"
    return True, "ok"

def run(it):
    if it.get("needs") and not os.path.exists(it["needs"]):
        return
    os.makedirs(os.path.dirname(it["out"]), exist_ok=True)
    p = subprocess.run(it["cmd"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if it.get("kind") not in ("json", "text"):
        last = (p.stdout.strip().splitlines() or [""])[-1]
        open(it["out"] + ".json", "w", encoding="utf-8").write(last if last.startswith("{") else json.dumps({"error": (p.stdout + p.stderr)[-3000:]}))

def work(it):
    ok, why = valid(it)
    if ok:
        return it["name"], True, "already complete"
    for attempt in range(1, 4):
        run(it)
        ok, why = valid(it)
        if ok:
            return it["name"], True, f"completed (attempt {attempt})"
        log(f"RETRY {it['stage']}/{it['name']}: attempt {attempt} failed: {why}")
    return it["name"], False, why

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=4); ap.add_argument("--stages", default=",".join(STAGES)); ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    for st in a.stages.split(","):
        items = STAGES[st]()
        todo = [it for it in items if not valid(it)[0]]
        log(f"stage {st}: items={len(items)} complete={len(items) - len(todo)} to run={len(todo)}")
        if a.list:
            continue
        with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
            for n, (name, ok, msg) in enumerate(ex.map(work, todo), 1):
                if n % 20 == 0 or not ok:
                    log(f"  {st} progress {n}/{len(todo)} last={name} {msg}")
        missing = [(it["name"], valid(it)[1]) for it in items if not valid(it)[0]]
        log(f"stage {st}: FINAL items={len(items)} incomplete={len(missing)}")
        for m in missing:
            log(f"  INCOMPLETE {st}/{m[0]}: {m[1]}")

if __name__ == "__main__":
    main()
