#!/usr/bin/env python3
"""Step 1 - the symptom from esp-rs/rust#278, reproduced on QEMU.

Rust calls esp_nn_max_pool_s8_esp32s3(15 arguments, mostly u16) implemented in C. The C
callee reads every argument as a full 32-bit word, the way esp-nn's hand-written assembly
does with l32i. The stack is filled with 0xA5 before each call, so a u16 whose upper half was
not written shows up as 0xa5a5xxxx.

Three toolchains:
  esp        the esp-rs toolchain the issue was found with (its own LLVM)
  upstream   upstream rustc + upstream LLVM (llc from llvm main)
  fixed      rustc with the ABI fix + LLVM with the byval fix
"""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import OUT, HARNESS, ESPNN_PARAMS, Report, config, run

R = Report("01_symptom")
PY = sys.executable

def params_status(qemu_out, path):
    bad = {}
    for l in open(qemu_out, encoding="utf-8", errors="replace"):
        if l.startswith("MISMATCH") and f"path={path}" in l:
            x = l.split()
            bad[x[3]] = (x[4].split("=")[1], x[5].split("=")[1])
    return bad

D = {n: os.path.join(OUT, "01_" + n) for n in ("esp", "upstream", "fixed")}
runs = [
    ("esp", [PY, os.path.join(HARNESS, "qemutest.py"), config.RUSTC_ESP, D["esp"], "--abi", "windowed", "--real-core", "--only-espnn"]),
    ("upstream", [PY, os.path.join(HARNESS, "qemu_matrix.py"), config.RUSTC_ORIG, config.LLC_BASE, "esp32s3", D["upstream"], "--only-espnn"]),
    ("fixed", [PY, os.path.join(HARNESS, "qemu_matrix.py"), config.RUSTC_FIXED, config.LLC_FIXED, "esp32s3", D["fixed"], "--only-espnn"]),
]
R("esp_nn_max_pool_s8_esp32s3: Rust caller -> C callee that reads every argument as a 32-bit word (QEMU, ESP32-S3 windowed ABI)")
R("stack poisoned with 0xA5 before the call; the first six arguments travel in a2..a7, the rest on the stack\n")
table = {}
for name, cmd in runs:
    run(cmd)
    q = os.path.join(D[name], "qemu.out")
    if not any(l.startswith("DONE") for l in open(q, encoding="utf-8", errors="replace")):
        raise SystemExit(f"{name}: the QEMU run did not finish, see {q}")
    table[name] = params_status(q, "rust->gcc")
R(f"{'#':>2} {'parameter':<15}{'type':<10}{'where':<7}" + "".join(f"{n:<34}" for n, _ in runs))
for k, (pname, rty, _) in enumerate(ESPNN_PARAMS):
    where = "a%d" % (k + 2) if k < 6 else f"sp+{(k - 6) * 4}"
    cells = []
    for n, _ in runs:
        b = table[n].get(f"p{k}")
        cells.append("ok" if b is None else f"WRONG got={b[0]} exp={b[1]}")
    R(f"{k:>2} {pname:<15}{rty:<10}{where:<7}" + "".join(f"{c:<34}" for c in cells))
R("")
for n, _ in runs:
    R(f"{n:>9}: {len(table[n])} of 15 arguments arrive wrong")
R("\nThe wrong values keep the right low 16 bits and have 0xa5a5 (the poison) in the upper half:")
R("the caller stored only two bytes (s16i) into a four-byte argument slot.")
R.save()
