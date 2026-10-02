"""Shared reading of sweep results (used by run_all.py and summarize.py).

A sweep run directory holds qemu.out; every failing check is a line
  MISMATCH path=<path> c<local case, hex> <field> got=<x> exp=<y>
Run names: <B|base>_<chip>_<opt>_<chunk>[_irva] for whole-sweep chunks.
"""
import collections, glob, hashlib, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

RUNS = config.RUNS
RUN_RE = re.compile(r"(B|base)_(\w+?)_(O\d)(?:_(\d+))?(_irva)?$")

def load(p):
    try:
        return json.load(open(p, encoding="utf-8-sig"))
    except Exception:
        return None

def mismatches(run_dir):
    """[(path, local_case, field, got, exp)]"""
    out = []
    q = os.path.join(run_dir, "qemu.out")
    if os.path.exists(q):
        for l in open(q, encoding="utf-8", errors="replace"):
            if l.startswith("MISMATCH"):
                x = l.split()
                out.append((x[1].split("=")[1], int(x[2][1:], 16), x[3], x[4].split("=")[1], x[5].split("=")[1]))
    return out

def config_of(rest):
    """'esp32s3_O2_even3_irva' -> ('esp32s3', 'O2', True)"""
    m = re.match(r"(\w+?)_(O\d)_", rest + "_")
    return m.group(1), m.group(2), "_irva" in rest

# ------------------------------------------------------------------ the three sweep families
class Family:
    def __init__(self, key, dirname, cases, l2g, odd_of):
        self.key, self.dirname, self.cases, self.l2g, self.odd_of = key, dirname, cases, l2g, odd_of

_fams = {}
def family(key):
    if key in _fams:
        return _fams[key]
    if key in ("v1", "clang"):
        import irsweep as W1
        cases = W1.cases()
        def l2g(run_dir, r):
            ci, cn = map(int, (r.get("chunk") or "0/1").split("/"))
            return collections.defaultdict(lambda: None, {i: ci + i * cn for i in range(len(cases))})
        odd = lambda c: any(isinstance(x, tuple) and x[1] % 4 and x[1] > 4 for x in c[0])
        f = Family(key, "sweep" if key == "v1" else "csweep", cases, l2g, odd)
    else:
        import irsweep2 as W2
        cases = W2.all_cases()
        def l2g(run_dir, r):
            idx = load(os.path.join(run_dir, "case_index.json")) or []
            return collections.defaultdict(lambda: None, dict(enumerate(idx)))
        f = Family(key, "sweep2", cases, l2g, W2.is_odd_case)
    _fams[key] = f
    return f

def failing_unchanged(key):
    """{(chip, opt, irva): sorted global case indices} of cases the fix must not change that fail
    with the fixed LLVM (GCC self-inconsistent cases, which fail on c->c, excluded)"""
    f = family(key)
    res = collections.defaultdict(set)
    for p in glob.glob(os.path.join(RUNS, f.dirname, "B_*.json")):
        m = RUN_RE.match(os.path.basename(p)[:-5])
        if not m:
            continue
        r = load(p) or {}
        g = f.l2g(p[:-5], r)
        per = collections.defaultdict(set)
        for path, lc, *_ in mismatches(p[:-5]):
            per[g[lc]].add(path)
        for gi, paths in per.items():
            if gi is not None and "c->c" not in paths and not f.odd_of(f.cases[gi]):
                res[(m.group(2), m.group(3), bool(m.group(5)))].add(gi)
    return {k: sorted(v) for k, v in res.items()}

CONFIRM_CHUNK = 150

def confirm_lists(key):
    """write runs/confirmlists/<key>_<chip>_<opt>[_irva]_c<k>_<hash>.txt; return [(rest, chip, opt, irva, path)]"""
    d = os.path.join(RUNS, "confirmlists"); os.makedirs(d, exist_ok=True)
    out = []
    for (chip, opt, irva), gis in sorted(failing_unchanged(key).items()):
        for k in range(0, len(gis), CONFIRM_CHUNK):
            part = " ".join(map(str, gis[k:k + CONFIRM_CHUNK]))
            rest = f"{chip}_{opt}{'_irva' if irva else ''}_c{k // CONFIRM_CHUNK}_{hashlib.sha1(part.encode()).hexdigest()[:8]}"
            p = os.path.join(d, f"{key}_{rest}.txt")
            if not os.path.exists(p):
                open(p, "w").write(part)
            out.append((rest, chip, opt, irva, p))
    return out
