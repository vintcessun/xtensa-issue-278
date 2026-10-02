import glob, os, re, subprocess, sys
import config
BIN = sys.argv[1]; DIR = sys.argv[2]
tools = {"llc", "FileCheck", "not", "count", "llvm-mc", "llvm-objdump", "opt", "llvm-readobj"}
passed, failed, skipped = [], [], []
for f in sorted(glob.glob(os.path.join(DIR, "**", "*.*"), recursive=True)):
    if not f.endswith((".ll", ".mir", ".s")): continue
    text = open(f, encoding="utf-8", errors="replace").read()
    runs, cur = [], ""
    for line in text.splitlines():
        m = re.search(r"(?:;|#)\s*RUN:\s?(.*)$", line)
        if not m: continue
        part = m.group(1).rstrip()
        if part.endswith("\\"): cur += part[:-1] + " "; continue
        runs.append(cur + part); cur = ""
    if not runs or re.search(r"(;|#)\s*(REQUIRES|UNSUPPORTED):", text):
        skipped.append(f); continue
    ok = True
    for r in runs:
        r = r.replace("%s", f.replace("\\", "/")).replace("%t", os.path.join(os.environ.get("TEMP", "."), "minilit.tmp").replace("\\", "/"))
        def sub(m):
            t = m.group(0)
            return f'"{BIN}/{t}.exe"' if t in tools else t
        r = re.sub(r"(?<![\w./-])(llc|FileCheck|not|count|llvm-mc|llvm-objdump|opt|llvm-readobj)(?![\w.-])", sub, r)
        p = subprocess.run([config.BASH, "-o", "pipefail", "-c", r], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode:
            ok = False; failed.append((f, r, p.stderr[-1500:])); break
    if ok: passed.append(f)
print(f"passed {len(passed)}, failed {len(failed)}, skipped(REQUIRES/no RUN) {len(skipped)}")
for f, r, e in failed:
    print("FAIL", os.path.basename(f)); print("   ", r[:200]); print("   ", e.strip().splitlines()[0] if e.strip() else "")
