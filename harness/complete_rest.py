#!/usr/bin/env python3
"""Finish every missing / stale / failed item of the Xtensa test matrix, with retries.

Items:
  sweep/*_call0_*    generic-CPU call0 IR sweep (must be re-run with the call0 __mulsi3 fix)
  even/*_call0_*     base-vs-B runtime comparison on all even-size cases for call0 at O0 and with IR va_arg
  perfn/*.json       per-function asm/verifier comparison (base vs B)
An item is complete only when its result validates; failures are retried immediately (max 3).
"""
import concurrent.futures as cf, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
import config
L = os.path.dirname(config.LLC_FIXED)
PY = sys.executable
LOG = os.path.join(config.RUNS, "complete_rest.log")

def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    open(LOG, "a", encoding="utf-8").write(line + "\n")

def load(p):
    try:
        return json.load(open(p, encoding="utf-8-sig"))
    except Exception:
        return None

def has_fix(d):
    p = os.path.join(d, "libc.c")
    return os.path.exists(p) and "__mulsi3" in open(p).read()

items = []
# ---- generic call0 sweep
for i in range(20):
    for v, opt, va in [("B", o, False) for o in ("O0", "O1", "O2", "O3")] + [("base", "O0", False), ("base", "O2", False), ("B", "O2", True), ("base", "O2", True)]:
        name = f"{v}_call0_{opt}_{i}" + ("_irva" if va else "")
        items.append(dict(kind="sweep", name=name, v=v, opt=opt, i=i, va=va))
# ---- even-case comparison for call0 at O0 and with IR va_arg
for k in range(8):
    for v in ("base", "B"):
        items.append(dict(kind="even", name=f"{v}_call0_O0_even{k}", v=v, opt="O0", k=k, va=False))
        items.append(dict(kind="even", name=f"{v}_call0_O2_even{k}_irva", v=v, opt="O2", k=k, va=True))
# ---- per-function comparisons
for i in range(20):
    for mcpu in ("esp32s3", "esp8266", "generic"):
        for opt in ("O0", "O2"):
            items.append(dict(kind="perfn", tag=f"ir_{mcpu}_{opt}_{i}", src=f"sweep/B_esp32s3_O2_{i}", mcpu=mcpu, opt=opt))
    items.append(dict(kind="perfn", tag=f"irva_esp32s3_O2_{i}", src=f"sweep/B_esp32s3_O2_{i}_irva", mcpu="esp32s3", opt="O2"))
    for chip, mcpu in (("esp32s3", "esp32s3"), ("esp8266real", "esp8266")):
        for opt in ("O0", "O2"):
            items.append(dict(kind="perfn", tag=f"clang_{mcpu}_{opt}_{i}", src=f"csweep/B_{chip}_{opt}_{i}", mcpu=mcpu, opt=opt))

def result_path(it):
    if it["kind"] == "sweep": return os.path.join(config.RUNS, "sweep", it["name"] + ".json")
    if it["kind"] == "even": return os.path.join(config.RUNS, "even", it["name"] + ".json")
    return os.path.join(config.RUNS, "perfn", it["tag"] + ".json")

def valid(it):
    r = load(result_path(it))
    if r is None:
        return False, "no/invalid json"
    if it["kind"] == "perfn":
        return (len(r) > 0, "empty") if isinstance(r, dict) else (False, "bad json")
    if "error" in r:
        return False, "error: " + r["error"][:200].replace("\n", " ")
    d = result_path(it)[:-5]
    if not has_fix(d):
        return False, "stale (built without the call0 __mulsi3 fix)"
    if it["v"] == "B" and not r.get("done"):
        return False, "B run did not reach DONE (exception=%s timeout=%s)" % (r.get("exception"), r.get("timeout"))
    if it["v"] == "base" and not (r.get("done") or r.get("exception")):
        return False, "base run neither finished nor stopped at the expected hardware exception"
    return True, "ok"

def run(it):
    if it["kind"] in ("sweep", "even"):
        cmd = [PY, os.path.join(HERE, "irsweep.py"), os.path.join(L, f"llc-{it['v']}.exe"), "call0", result_path(it)[:-5], "--opt", it["opt"]]
        cmd += ["--chunk", f"{it['i']}/20"] if it["kind"] == "sweep" else ["--cases", os.path.join(config.RUNS, "evenlists", f"even_{it['k']}.txt")]
        if it["va"]: cmd.append("--ir-va-arg")
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (p.stdout.strip().splitlines() or [""])[-1]
        os.makedirs(os.path.dirname(result_path(it)), exist_ok=True)
        open(result_path(it), "w", encoding="utf-8").write(out if out.startswith("{") else json.dumps({"error": (p.stdout + p.stderr)[-2000:]}))
    else:
        src = os.path.join(config.RUNS, it["src"], "ir.ll")
        if not os.path.exists(src):
            return
        os.makedirs(os.path.join(config.RUNS, "perfn"), exist_ok=True)
        subprocess.run([PY, os.path.join(HERE, "perfn.py"), src, os.path.join(L, "llc-base.exe"), os.path.join(L, "llc-B.exe"),
                        it["mcpu"], it["opt"], result_path(it)], capture_output=True, text=True)

def work(it):
    ident = it.get("name") or it.get("tag")
    ok, why = valid(it)
    if ok:
        return ident, True, "already complete"
    for attempt in range(1, 4):
        run(it)
        ok, why = valid(it)
        if ok:
            return ident, True, f"completed (attempt {attempt})"
        log(f"RETRY {ident}: attempt {attempt} failed: {why}")
    return ident, False, why

todo = [it for it in items if not valid(it)[0]]
log(f"items total={len(items)} already complete={len(items) - len(todo)} to run={len(todo)}")
failed = []
with cf.ThreadPoolExecutor(max_workers=int(os.environ.get("JOBS", "4"))) as ex:
    for n, (ident, ok, msg) in enumerate(ex.map(work, todo), 1):
        if not ok:
            failed.append((ident, msg))
        if n % 10 == 0 or not ok:
            log(f"progress {n}/{len(todo)}  last={ident} {msg}")
missing = [(it.get("name") or it.get("tag"), valid(it)[1]) for it in items if not valid(it)[0]]
log(f"FINAL: items={len(items)} incomplete={len(missing)}")
for m in missing:
    log(f"  INCOMPLETE {m[0]}: {m[1]}")
