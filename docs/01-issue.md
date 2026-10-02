# 1. The issue: esp-rs/rust#278

## Symptom

On an ESP32-S3 (`xtensa-esp32s3-none-elf`), Rust calls the C function
`esp_nn_max_pool_s8_esp32s3` from esp-nn. It takes 15 arguments, most of them `u16`:

```c
void esp_nn_max_pool_s8_esp32s3(const int8_t *input,
                                uint16_t input_wd, uint16_t input_ht,
                                int8_t *output,
                                uint16_t output_wd, uint16_t output_ht,
                                uint16_t stride_wd, uint16_t stride_ht,
                                uint16_t filter_wd, uint16_t filter_ht,
                                uint16_t pad_wd, uint16_t pad_ht,
                                int32_t activation_min, int32_t activation_max,
                                uint16_t channels);
```

The Xtensa ABI passes the first six argument words in `a2`..`a7`; the rest go on the stack.
The esp32s3 implementation of this function is hand-written assembly that loads every stack
argument with `l32i` (a full word). With Rust as the caller, every `u16` passed on the stack
arrives with garbage in its upper 16 bits, and the kernel produces nonsense (all outputs -128).
Declaring those parameters as `u32` in the Rust binding makes it work.

`repro/01_symptom.py` reproduces this on QEMU with three toolchains:

```
 # parameter      type      where  esp                               upstream                          fixed
 6 stride_wd      u16       sp+0   WRONG got=a5a54e55 exp=00004e55   WRONG got=a5a5dab2 exp=0000dab2   ok
 ...
14 channels       u16       sp+32  WRONG got=a5a5c514 exp=0000c514   WRONG got=a5a547fb exp=000047fb   ok
      esp: 7 of 15 arguments arrive wrong
 upstream: 7 of 15 arguments arrive wrong
    fixed: 0 of 15 arguments arrive wrong
```

The stack is filled with `0xA5` before the call: the low 16 bits are right, the upper half is
whatever was on the stack. See [results/repro/01_symptom.txt](../results/repro/01_symptom.txt).

## Correction to the issue text

The issue originally said that `channels` lands at offset 20 while the callee expects 32, i.e.
that the offsets of later arguments are shifted. That is not what happens: the offsets are right
(0, 4, 8, ... 32). Each 4-byte slot is only half written. The problem is the missing extension,
not misplaced arguments.

## What the ABI requires

Xtensa Instruction Set Architecture (ISA) Reference Manual, section 8.1.4 *Argument Passing*
(pp. 590-591):

> All arguments consist of an integral number of 4-byte words. Thus, the minimum argument size
> is one word. Integer values smaller than a word (that is, char and short) are stored in the
> least significant portion of the argument word, with the upper bits set to zero for unsigned
> values or sign-extended for signed values.

> If an argument requires 4-byte or less alignment, this requirement has no effect; all
> arguments have at least 4-byte alignment anyway.

> If the size of a structure is not an integral number of words, padding is inserted at one end
> of the structure. [...] A structure larger than a word is padded in the last bytes of the last
> argument word.

The rule does not distinguish between arguments in registers and arguments on the stack.
GCC (the ESP-IDF compiler) implements exactly this: every argument occupies
`(size + 3) / 4` words (`xtensa_function_arg_advance` in `gcc/config/xtensa/xtensa.cc`), and a
`u16` on the stack is zero-extended and stored with `s32i`
(see [results/repro/04_gcc_reference.txt](../results/repro/04_gcc_reference.txt)).

Next: [2. rustc](02-rustc.md)
