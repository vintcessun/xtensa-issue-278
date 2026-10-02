#!/usr/bin/env python3
"""Step 4 - the reference: what the ABI says and what GCC (the ESP-IDF compiler) does."""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import OUT, ESPNN_PARAMS, Report, config, run, function_asm

R = Report("04_gcc_reference")
R("Xtensa Instruction Set Architecture (ISA) Reference Manual, 8.1.4 Argument Passing (pp. 590-591):")
for q in [
    "  \"All arguments consist of an integral number of 4-byte words. Thus, the minimum argument size is one word.",
    "   Integer values smaller than a word (that is, char and short) are stored in the least significant portion",
    "   of the argument word, with the upper bits set to zero for unsigned values or sign-extended for signed values.\"",
    "  \"...all arguments have at least 4-byte alignment anyway.\"",
    "  \"If the size of a structure is not an integral number of words, padding is inserted at one end of the",
    "   structure. ... A structure larger than a word is padded in the last bytes of the last argument word\"",
    "  \"...the caller stores the seventh argument word (after the first six words in registers) at [sp + 0],",
    "   the eighth word at [sp + 4], and so on.\"",
    "",
    "GCC (gcc/config/xtensa/xtensa.cc) counts every argument in whole words:",
    "  words = (arg.promoted_size_in_bytes () + UNITS_PER_WORD - 1) / UNITS_PER_WORD;",
    ""]:
    R(q)

d = os.path.join(OUT, "04_gcc"); os.makedirs(d, exist_ok=True)
proto = ", ".join(c for _, _, c in ESPNN_PARAMS)
args = ", ".join({"const int8_t *": "0", "int8_t *": "0", "int32_t": "(int32_t)x", "uint16_t": "(uint16_t)x"}[c] for _, _, c in ESPNN_PARAMS)
open(os.path.join(d, "ref.c"), "w").write(f"""#include <stdint.h>
void esp_nn_max_pool_s8_esp32s3({proto});
void call_max_pool(uint32_t x) {{ esp_nn_max_pool_s8_esp32s3({args}); }}

typedef struct {{ uint16_t a, b, c; }} S6;              /* 6 bytes: occupies 2 words */
void use_s6_then_u16(uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, S6, uint16_t);
void call_s6(const S6 *s, uint32_t y) {{ use_s6_then_u16(0, 0, 0, 0, 0, *s, (uint16_t)y); }}
uint32_t read_s6_then_u16(uint32_t a, uint32_t b, uint32_t c, uint32_t d, uint32_t e, S6 s, uint16_t y) {{ return y; }}
""")
gcc = os.path.join(config.ESP_GCC_BIN, "xtensa-esp32s3-elf-gcc.exe")
for abi, flags in (("windowed", []), ("call0", ["-mabi=call0"])):
    asm = run([gcc, "-O2", "-S", "-o", "-"] + flags + [os.path.join(d, "ref.c")]).stdout
    R(f"GCC {abi} ABI:")
    body = function_asm(asm, "call_max_pool")
    stores = {}
    for l in body:
        m = re.search(r"\b(s8i|s16i|s32i)(?:\.n)?\s+a\d+,\s*(?:a1|sp),\s*(\d+)", l)
        if m and int(m.group(2)) <= 32:
            stores[int(m.group(2))] = m.group(1)
    R("  esp_nn_max_pool_s8_esp32s3 stack arguments: " + " ".join(f"+{o}:{stores[o]}" for o in sorted(stores)))
    R("  zero-extension of the u16 values: " + "; ".join(l.strip() for l in body if re.search(r"\bextui\b", l)))
    R("  6-byte struct then u16, caller:  " + "; ".join(l.strip() for l in function_asm(asm, "call_s6") if re.search(r"s32i\S*\s+a\d+,\s*(a1|sp),\s*8\b|extui", l)))
    R("  6-byte struct then u16, callee:  " + "; ".join(l.strip() for l in function_asm(asm, "read_s6_then_u16") if re.search(r"l16ui|l32i", l)))
    R("")
R("GCC: every stack argument is a full word (s32i of an extended value); the u16 after a 6-byte struct is at +8.")
R.save()
