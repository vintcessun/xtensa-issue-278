"""Helpers shared by the repro steps."""
import os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, "harness")
sys.path.insert(0, HARNESS)
import config  # noqa: E402

OUT = os.path.join(config.RUNS, "repro")
RESULTS = os.path.join(ROOT, "results", "repro")
os.makedirs(OUT, exist_ok=True)
os.makedirs(RESULTS, exist_ok=True)

# esp_nn_max_pool_s8_esp32s3 from esp-rs/rust#278
ESPNN_PARAMS = [("input", "*const i8", "const int8_t *"), ("input_wd", "u16", "uint16_t"), ("input_ht", "u16", "uint16_t"),
                ("output", "*mut i8", "int8_t *"), ("output_wd", "u16", "uint16_t"), ("output_ht", "u16", "uint16_t"),
                ("stride_wd", "u16", "uint16_t"), ("stride_ht", "u16", "uint16_t"), ("filter_wd", "u16", "uint16_t"),
                ("filter_ht", "u16", "uint16_t"), ("pad_wd", "u16", "uint16_t"), ("pad_ht", "u16", "uint16_t"),
                ("activation_min", "i32", "int32_t"), ("activation_max", "i32", "int32_t"), ("channels", "u16", "uint16_t")]

def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if p.returncode:
        raise RuntimeError(f"FAILED: {' '.join(map(str, cmd))}\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}")
    return p

class Report:
    """prints and records the output of one repro step"""
    def __init__(self, name):
        self.path = os.path.join(RESULTS, name + ".txt")
        self.lines = []
    def __call__(self, *parts):
        s = " ".join(str(p) for p in parts)
        print(s, flush=True)
        self.lines.append(s)
    def save(self):
        open(self.path, "w", encoding="utf-8").write("\n".join(self.lines) + "\n")
        print(f"\n[saved to {os.path.relpath(self.path, ROOT)}]")

def function_asm(asm, name):
    m = re.search(rf"^{re.escape(name)}:.*?\n(.*?)^\.Lfunc_end", asm, re.M | re.S)
    if not m:
        m = re.search(rf"^{re.escape(name)}:.*?\n(.*?)^\s*\.size\s+{re.escape(name)}", asm, re.M | re.S)
    return [l.rstrip() for l in (m.group(1) if m else "").splitlines() if l.strip() and not l.strip().startswith(".cfi")]
