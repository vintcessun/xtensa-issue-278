#!/usr/bin/env python3
"""Step 2 - what rustc hands to LLVM.

The same extern "C" declaration and call are compiled to LLVM IR by each rustc. In the IR the
arguments that no longer fit in a2..a7 show how rustc's Xtensa ABI code
(compiler/rustc_target/src/callconv/xtensa.rs, classify_arg_ty) passes them:

  upstream / esp:  ptr byval([2 x i8]) align 2   - two bytes of memory, no signedness, no extension
  fixed:           i16 noundef zeroext            - a value LLVM widens to a full word
"""
import os, re, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import OUT, ESPNN_PARAMS, Report, config, run

R = Report("02_rustc_abi")
params = ", ".join(f"{n}: {t}" for n, t, _ in ESPNN_PARAMS)
args = ", ".join({"*const i8": "0 as *const i8", "*mut i8": "0 as *mut i8", "i32": "x as i32", "u16": "x as u16"}[t] for _, t, _ in ESPNN_PARAMS)
BODY = f"""
extern "C" {{
    fn esp_nn_max_pool_s8_esp32s3({params});
}}
#[no_mangle]
pub unsafe extern "C" fn call_max_pool(x: u32) {{
    esp_nn_max_pool_s8_esp32s3({args});
}}
"""
MINICORE = "#![feature(no_core, lang_items)]\n#![no_core]\n#![crate_type = \"lib\"]\nextern crate minicore;\nuse minicore::*;\n"

def emit_minicore(rustc, tag):
    d = os.path.join(OUT, "02_" + tag); os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "lib.rs"), "w").write(MINICORE + BODY)
    mc = os.path.join(d, "libminicore.rlib")
    run([rustc, "--target", "xtensa-esp32s3-none-elf", "--crate-type", "rlib", "-Copt-level=3", "-Cpanic=abort", "-Awarnings", config.MINICORE, "-o", mc])
    run([rustc, "--target", "xtensa-esp32s3-none-elf", "-Copt-level=3", "-Cpanic=abort", "-Awarnings", "--extern", f"minicore={mc}",
         "--emit", "llvm-ir", os.path.join(d, "lib.rs"), "-o", os.path.join(d, "lib.ll")])
    return os.path.join(d, "lib.ll")

def emit_esp():
    """the esp-rs toolchain has no prebuilt core for Xtensa: build it with -Zbuild-std"""
    d = os.path.join(OUT, "02_esp"); os.makedirs(os.path.join(d, "src"), exist_ok=True)
    open(os.path.join(d, "Cargo.toml"), "w").write('[package]\nname = "espnn_abi"\nversion = "0.0.0"\nedition = "2021"\n[lib]\npath = "src/lib.rs"\n'
                                                   '[profile.release]\npanic = "abort"\nopt-level = 3\ncodegen-units = 1\n')
    open(os.path.join(d, "src", "lib.rs"), "w").write("#![no_std]\n" + BODY +
                                                      "#[panic_handler] fn ph(_: &core::panic::PanicInfo) -> ! { loop {} }\n")
    cargo = os.path.join(os.path.dirname(config.RUSTC_ESP), "cargo.exe")
    tgt = config.CARGO_TARGET_DIR + "r2"
    env = dict(os.environ, RUSTC=config.RUSTC_ESP, RUSTFLAGS="--emit=llvm-ir,link -Awarnings", CARGO_TARGET_DIR=tgt)
    run([cargo, "build", "--release", "-Zbuild-std=core", "--target", "xtensa-esp32s3-none-elf", "--manifest-path", os.path.join(d, "Cargo.toml")], env=env)
    ll = [f for f in os.listdir(os.path.join(tgt, "xtensa-esp32s3-none-elf", "release", "deps")) if f.startswith("espnn_abi") and f.endswith(".ll")][0]
    shutil.copy(os.path.join(tgt, "xtensa-esp32s3-none-elf", "release", "deps", ll), os.path.join(d, "lib.ll"))
    return os.path.join(d, "lib.ll")

def declared_params(ll):
    text = open(ll, encoding="utf-8").read()
    m = re.search(r"^declare [^@]*@esp_nn_max_pool_s8_esp32s3\((.*)\)[^(]*$", text, re.M)
    parts, depth, cur = [], 0, ""
    for ch in m.group(1):
        if ch == "," and depth == 0:
            parts.append(cur.strip()); cur = ""; continue
        depth += ch in "([" ; depth -= ch in ")]"
        cur += ch
    parts.append(cur.strip())
    return parts

irs = {"esp": emit_esp(), "upstream": emit_minicore(config.RUSTC_ORIG, "upstream"), "fixed": emit_minicore(config.RUSTC_FIXED, "fixed")}
R("LLVM IR declaration of esp_nn_max_pool_s8_esp32s3 as produced by each rustc (xtensa-esp32s3-none-elf, opt-level=3)\n")
decl = {k: declared_params(v) for k, v in irs.items()}
NOISE = re.compile(r" captures\(\w+\)| dereferenceable\(\d+\)| noalias| nofree")
def short(s):
    return NOISE.sub("", s)
R(f"{'#':>2} {'parameter':<15}{'Rust':<10}" + "".join(f"{k:<38}" for k in irs))
for i, (n, t, _) in enumerate(ESPNN_PARAMS):
    R(f"{i:>2} {n:<15}{t:<10}" + "".join(f"{short(decl[k][i]):<38}" for k in irs))
R("\nIR files:")
for k, v in irs.items():
    R(f"  {k:<9} {os.path.relpath(v, config.RUNS)}")
R("\nArguments #6.. (the 7th onwards) no longer fit in a2..a7. upstream/esp rustc passes every one of them as")
R("byval memory (PassMode::Indirect on_stack) - for u16 that is 2 bytes with no signedness and no extension.")
R("rustc with the fix keeps integers of 32 bits or less as values (zeroext/signext) even on the stack.")
R.save()

