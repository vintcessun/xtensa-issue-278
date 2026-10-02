#!/usr/bin/env python3
"""Real-world corpus comparison for the LLVM fix.

  fetch   shallow-clone the C projects (idempotent)
  ir      C -> LLVM IR with esp-clang; Rust (core, alloc, compiler_builtins and a set of
          no_std crates) -> LLVM IR with the rustc under test, via -Zbuild-std
  compare every module: llc (upstream) vs llc (fixed), function by function; every function
          whose code changes must involve a byval aggregate whose size is not a multiple of 4
          (sizes are measured by llc itself), and the verifier must not report anything new.

  split   modules that upstream llc cannot compile at all (with or without the fix) are split
          with llvm-split so the parts that do compile are compared too

usage: python corpus.py fetch|ir|split|compare|all
"""
import glob, json, os, re, shutil, subprocess, sys, zipfile, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "harness"))
import config

WORK = os.path.join(config.RUNS, "corpus")
SRC = os.path.join(WORK, "src")
IR = os.path.join(WORK, "ir")
OUT = os.path.join(WORK, "compare")
NEWLIB_INC = os.path.join(os.path.dirname(config.ESP_GCC_BIN), "xtensa-esp-elf", "include")

C_PROJECTS = {
    # name: (git url, ref or None, source globs, extra flags)
    "esp-nn":   ("https://github.com/espressif/esp-nn", None, ["src/**/*_ansi.c", "src/**/*_opt.c", "src/common/*.c"], ["-Iinclude", "-Isrc/common"]),
    "mbedtls":  ("https://github.com/Mbed-TLS/mbedtls", "v3.6.2", ["library/*.c"], ["-Iinclude", "-Ilibrary"]),
    "zlib":     ("https://github.com/madler/zlib", None, ["*.c"], ["-DZ_SOLO", "-DNO_GZIP"]),
    "cjson":    ("https://github.com/DaveGamble/cJSON", None, ["cJSON.c", "cJSON_Utils.c"], []),
    "littlefs": ("https://github.com/littlefs-project/littlefs", None, ["lfs.c", "lfs_util.c"], ["-DLFS_NO_DEBUG", "-DLFS_NO_WARN", "-DLFS_NO_ERROR", "-DLFS_NO_ASSERT"]),
    "lua":      ("https://github.com/lua/lua", "v5.4.7", ["l*.c"], ["-DLUA_USE_C89"]),
    "lwip":     ("https://github.com/lwip-tcpip/lwip", "STABLE-2_2_0_RELEASE", ["src/core/*.c", "src/core/ipv4/*.c", "src/core/ipv6/*.c", "src/api/err.c"],
                 ["-Isrc/include", "-I" + os.path.join(HERE, "lwip-port")]),
}
# Files left out because they do not apply to a freestanding Xtensa build:
#   lua: programs/test builds; zlib: gz* need stdio, compress/uncompr are not part of Z_SOLO;
#   mbedtls: OS entropy, sockets, timing, platform glue; esp-nn: RISC-V (PIE) implementations.
C_EXCLUDE = {"lua": {"lua.c", "luac.c", "onelua.c", "ltests.c"},
             "zlib": {"example.c", "minigzip.c", "gzlib.c", "gzread.c", "gzwrite.c", "gzclose.c", "compress.c", "uncompr.c"},
             "mbedtls": {"entropy_poll.c", "net_sockets.c", "platform_util.c", "timing.c"}}
C_EXCLUDE_RE = {"esp-nn": re.compile(r"riscv", re.I)}
SQLITE_ZIP = "https://www.sqlite.org/2024/sqlite-amalgamation-3460100.zip"
RUST_CRATES = ['heapless = "0.8"', 'postcard = { version = "1", default-features = false }', 'serde = { version = "1", default-features = false, features = ["derive"] }',
               'micromath = "2"', 'libm = "0.2"', 'embedded-hal = "1"', 'embedded-io = "0.6"', 'nb = "1"', 'bitflags = "2"',
               'crc = "3"', 'hashbrown = { version = "0.15", default-features = false, features = ["default-hasher"] }', 'memchr = { version = "2", default-features = false }',
               'fixed = "1"', 'embedded-graphics = "0.8"', 'nom = { version = "7", default-features = false }']

def sh(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if p.returncode:
        raise RuntimeError(f"FAILED: {' '.join(map(str, cmd))}\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}")
    return p

def fetch():
    os.makedirs(SRC, exist_ok=True)
    for name, (url, ref, _, _) in C_PROJECTS.items():
        d = os.path.join(SRC, name)
        if os.path.exists(os.path.join(d, ".git")):
            continue
        cmd = ["git", "clone", "--depth", "1"] + (["-b", ref] if ref else []) + [url, d]
        sh(cmd)
        print("fetched", name)
    d = os.path.join(SRC, "sqlite")
    if not os.path.exists(d):
        z = os.path.join(SRC, "sqlite.zip")
        urllib.request.urlretrieve(SQLITE_ZIP, z)
        with zipfile.ZipFile(z) as f:
            f.extractall(d)
        print("fetched sqlite")

def c_ir():
    res = {}
    for name, (_, _, globs, flags) in C_PROJECTS.items():
        base = os.path.join(SRC, name)
        files = sorted({f for g in globs for f in glob.glob(os.path.join(base, g), recursive=True)})
        files = [f for f in files if os.path.basename(f) not in C_EXCLUDE.get(name, set())
                 and not (name in C_EXCLUDE_RE and C_EXCLUDE_RE[name].search(os.path.basename(f)))]
        res[name] = compile_c(name, base, files, flags)
    sq = glob.glob(os.path.join(SRC, "sqlite", "*", "sqlite3.c"))
    res["sqlite"] = compile_c("sqlite", os.path.dirname(sq[0]), sq, ["-DSQLITE_OS_OTHER=1", "-DSQLITE_THREADSAFE=0", "-DSQLITE_OMIT_LOAD_EXTENSION",
                                                                   "-DSQLITE_OMIT_WAL", "-DSQLITE_OMIT_SHARED_CACHE"])
    return res

def compile_c(name, base, files, flags):
    out = os.path.join(IR, "c", name); os.makedirs(out, exist_ok=True)
    ok, fail = 0, []
    for f in files:
        dst = os.path.join(out, re.sub(r"[\\/:]", "_", os.path.relpath(f, base)) + ".ll")
        if os.path.exists(dst):
            ok += 1; continue
        p = subprocess.run([config.ESP_CLANG, "--target=xtensa-esp-elf", "-mcpu=esp32s3", "-O2", "-S", "-emit-llvm", "-ffreestanding",
                            "-w", "-isystem", NEWLIB_INC] + flags + [f, "-o", dst], cwd=base, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode:
            fail.append((os.path.relpath(f, base), p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "?"))
        else:
            ok += 1
    print(f"{name}: {ok} files -> IR, {len(fail)} failed")
    for f in fail[:5]:
        print("   ", f)
    return dict(ok=ok, failed=fail)

def rust_ir():
    crate = os.path.join(WORK, "rustcorpus"); os.makedirs(os.path.join(crate, "src"), exist_ok=True)
    open(os.path.join(crate, "Cargo.toml"), "w").write(
        '[package]\nname = "rustcorpus"\nversion = "0.0.0"\nedition = "2021"\n[lib]\npath = "src/lib.rs"\n[dependencies]\n' + "\n".join(RUST_CRATES) +
        '\n[profile.release]\npanic = "abort"\nopt-level = 3\ncodegen-units = 1\n')
    open(os.path.join(crate, "src", "lib.rs"), "w").write(
        "#![no_std]\nextern crate alloc;\n" + "".join(f"pub use {c.split(' ')[0].replace('-', '_')};\n" for c in RUST_CRATES))
    cargo = os.path.join(os.path.dirname(config.RUSTC_ESP), "cargo.exe")
    env = dict(os.environ, RUSTC=config.RUSTC_FIXED, RUSTFLAGS="--emit=llvm-ir,link -Ccodegen-units=1 -Awarnings",
               CARGO_TARGET_DIR=config.CARGO_TARGET_DIR + "c", RUSTC_BOOTSTRAP="1")
    sh([cargo, "build", "--release", "-Zbuild-std=core,alloc", "--target", "xtensa-esp32s3-none-elf", "--manifest-path", os.path.join(crate, "Cargo.toml")], env=env)
    out = os.path.join(IR, "rust"); os.makedirs(out, exist_ok=True)
    n = 0
    for f in glob.glob(os.path.join(config.CARGO_TARGET_DIR + "c", "xtensa-esp32s3-none-elf", "release", "deps", "*.ll")):
        n += 1
        if not os.path.exists(os.path.join(WORK, "unsplit", os.path.basename(f))):  # already replaced by its split parts
            shutil.copy(f, out)
    print(f"rust: {n} crate modules -> IR")
    return dict(modules=n)

# --------------------------------------------------------------------------- modules upstream llc cannot compile
# A few Rust modules (core, compiler_builtins, libm, micromath) stop upstream llc with or without the
# fix (Cannot select ConstantPool, register scavenger). Split them with llvm-split so that every
# part that does compile is still compared; parts that still crash are split once more, and what
# crashes after that is recorded with its function count. The unsplit module stays out of IR/.
SPLIT_PARTS = 64

def llc_ok(ll):
    return all(subprocess.run([llc, "-O2", f"-mcpu={m}", ll, "-o", os.devnull], capture_output=True).returncode == 0
               for llc in (config.LLC_BASE, config.LLC_FIXED) for m in ("esp32s3", "esp8266"))

def split_crashing():
    tool = lambda n: os.path.join(config.LLVM_BUILD, "bin", n + ".exe")
    held = os.path.join(WORK, "unsplit"); os.makedirs(held, exist_ok=True)
    rpath = os.path.join(WORK, "split.json")
    report = json.load(open(rpath)) if os.path.exists(rpath) else {}
    for ll in sorted(glob.glob(os.path.join(IR, "rust", "*.ll")) + glob.glob(os.path.join(IR, "c", "**", "*.ll"), recursive=True)):
        if llc_ok(ll):
            continue
        name = os.path.splitext(os.path.basename(ll))[0]
        dest = os.path.join(IR, "split", name); os.makedirs(dest, exist_ok=True)
        entry = {"functions": len(re.findall(r"^define ", open(ll, encoding="utf-8", errors="replace").read(), re.M)), "still_crashing": []}
        todo = [(ll, os.path.join(dest, "p"))]
        # --preserve-locals keeps a local function with its callers, so a crashing group of local
        # functions cannot be split further that way; the last level externalizes them instead
        levels = [["--preserve-locals"], ["--preserve-locals"], []]
        for level, flags in enumerate(levels):
            nxt = []
            for src, prefix in todo:
                # llvm-split writes bitcode whatever -S says; the comparison reads the IR as text
                sh([tool("llvm-split")] + flags + ["--round-robin", f"-j={SPLIT_PARTS}", src, "-o", prefix])
                for bc in sorted(p for p in glob.glob(prefix + "[0-9]*") if not p.endswith(".ll")):
                    sh([tool("llvm-dis"), bc, "-o", bc + ".ll"]); os.remove(bc)
                for part in sorted(glob.glob(prefix + "[0-9]*.ll")):
                    if llc_ok(part):
                        continue
                    kept = os.path.join(held, name + "__" + os.path.basename(part))
                    os.replace(part, kept)
                    if level + 1 < len(levels):
                        nxt.append((kept, part[:-3] + "_"))
                    else:
                        entry["still_crashing"].append({"part": os.path.basename(part), "functions": len(re.findall(r"^define ", open(kept, encoding="utf-8", errors="replace").read(), re.M))})
            todo = nxt
        os.replace(ll, os.path.join(held, os.path.basename(ll)))
        report[name] = entry
        print(name, json.dumps(entry))
    json.dump(report, open(rpath, "w"), indent=1)

# --------------------------------------------------------------------------- comparison
# [^\n]* (not .*): with re.S a greedy .* would swallow the rest of the file
FUNC_RE = re.compile(r"^([\w.$]+):[ \t]*(?:#[^\n]*)?\n(.*?)^\.Lfunc_end\d+:", re.M | re.S)

def asm_funcs(llc, ll, mcpu):
    p = subprocess.run([llc, "-O2", f"-mcpu={mcpu}", ll, "-o", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        return None, p.stderr.strip().splitlines()[0] if p.stderr.strip() else "llc failed"
    funcs = dict(FUNC_RE.findall(p.stdout))
    funcs["<whole module>"] = p.stdout
    return funcs, None

def byval_types(ir_text):
    """function name -> set of byval types used in its signature or at its call sites"""
    res = {}
    for m in re.finditer(r"^define [^@]*@\"?([\w.$]+)\"?\((.*?)\)[^{]*\{(.*?)^\}", ir_text, re.M | re.S):
        name, params, body = m.groups()
        tys = set(re.findall(r"byval\((.*?)\)(?= |,|\))", params + body))
        if tys:
            res[name] = tys
    return res

def type_sizes(ir_text, types, llc):
    """measure alloc sizes of IR types with llc: one global per type, read back .size"""
    named = "\n".join(re.findall(r"^%[\w.\"$]+ = type .*$", ir_text, re.M))
    body = [named, 'target datalayout = "e-m:e-p:32:32-i8:8:32-i16:16:32-i64:64-n32"', 'target triple = "xtensa"']
    tl = sorted(types)
    for i, t in enumerate(tl):
        body.append(f"@szprobe{i} = global {t} zeroinitializer")
    tmp = os.path.join(OUT, "_probe.ll"); open(tmp, "w", encoding="utf-8").write("\n".join(body) + "\n")
    p = subprocess.run([llc, tmp, "-o", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    sizes = {int(i): int(s) for i, s in re.findall(r"\.size\s+szprobe(\d+),\s*(\d+)", p.stdout)}
    return {t: sizes.get(i) for i, t in enumerate(tl)}

def verify(llc, ll, mcpu):
    p = subprocess.run([llc, "-O2", f"-mcpu={mcpu}", "-verify-machineinstrs", ll, "-o", os.devnull], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode == 0, sorted(set(re.findall(r"- function:\s+(\S+)", p.stderr)))

def compare():
    os.makedirs(OUT, exist_ok=True)
    summary = {}
    for ll in sorted(glob.glob(os.path.join(IR, "**", "*.ll"), recursive=True)):
        key = os.path.relpath(ll, IR)
        rpath = os.path.join(OUT, re.sub(r"[\\/]", "__", key) + ".json")
        if os.path.exists(rpath):
            summary[key] = json.load(open(rpath)); continue
        text = open(ll, encoding="utf-8", errors="replace").read()
        bv = byval_types(text)
        sizes = type_sizes(text, {t for s in bv.values() for t in s}, config.LLC_FIXED) if bv else {}
        entry = {"functions": 0, "byval_functions": len(bv), "odd_byval_functions": 0}
        oddf = {f for f, ts in bv.items() if any(sizes.get(t) is not None and sizes[t] % 4 and sizes[t] > 4 for t in ts)}
        entry["odd_byval_functions"] = len(oddf)
        entry["unmeasured_types"] = sorted(t for t, s in sizes.items() if s is None)
        for mcpu in ("esp32s3", "esp8266"):
            a, ea = asm_funcs(config.LLC_BASE, ll, mcpu)
            b, eb = asm_funcs(config.LLC_FIXED, ll, mcpu)
            if a is None or b is None:
                entry[mcpu] = {"llc_error_base": ea, "llc_error_fixed": eb}
                continue
            defines = len(re.findall(r"^define ", text, re.M))
            split_ok = len(b) - 1 == defines and len(a) - 1 == defines
            module_differs = a["<whole module>"] != b["<whole module>"]
            diff = sorted(f for f in set(a) | set(b) if f != "<whole module>" and a.get(f) != b.get(f))
            if not split_ok or (module_differs and not diff):
                entry.setdefault("anomalies", []).append(f"{mcpu}: split_ok={split_ok} defines={defines} funcs={len(b) - 1} module_differs={module_differs} func_diffs={len(diff)}")
            vb, vbad_b = verify(config.LLC_BASE, ll, mcpu)
            vf, vbad_f = verify(config.LLC_FIXED, ll, mcpu)
            entry["functions"] = max(entry["functions"], len(b) - 1)
            entry[mcpu] = {"differ": diff, "differ_without_odd_byval": [f for f in diff if f not in oddf],
                           "verifier_base_ok": vb, "verifier_fixed_ok": vf, "verifier_base_first_bad": vbad_b, "verifier_fixed_first_bad": vbad_f}
        json.dump(entry, open(rpath, "w"), indent=1)
        summary[key] = entry
    json.dump(summary, open(os.path.join(OUT, "summary.json"), "w"), indent=1)
    tot = dict(modules=len(summary), functions=0, byval_functions=0, odd_byval_functions=0)
    for mcpu in ("esp32s3", "esp8266"):
        tot[mcpu] = dict(differ=0, differ_without_odd_byval=0, llc_errors=0, verifier_new_in_fixed=0, verifier_fixed_by_fix=0, verifier_both_fail=0)
    for k, e in summary.items():
        for f in ("functions", "byval_functions", "odd_byval_functions"):
            tot[f] += e[f]
        for mcpu in ("esp32s3", "esp8266"):
            m = e.get(mcpu, {}); t = tot[mcpu]
            if "differ" not in m:
                t["llc_errors"] += 1; continue
            t["differ"] += len(m["differ"]); t["differ_without_odd_byval"] += len(m["differ_without_odd_byval"])
            if m["verifier_base_ok"] and not m["verifier_fixed_ok"]: t["verifier_new_in_fixed"] += 1
            if not m["verifier_base_ok"] and m["verifier_fixed_ok"]: t["verifier_fixed_by_fix"] += 1
            if not m["verifier_base_ok"] and not m["verifier_fixed_ok"]: t["verifier_both_fail"] += 1
    json.dump(tot, open(os.path.join(OUT, "totals.json"), "w"), indent=1)
    print(json.dumps(tot, indent=1))

if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step in ("fetch", "all"): fetch()
    if step in ("ir", "all"):
        json.dump({"c": c_ir(), "rust": rust_ir()}, open(os.path.join(WORK, "ir_build.json"), "w"), indent=1, default=str)
    if step in ("split", "all"): split_crashing()
    if step in ("compare", "all"): compare()

