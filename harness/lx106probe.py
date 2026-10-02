import glob, os, subprocess, time, zipfile
S = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not glob.glob(os.path.join(S, "lx106", "**", "xtensa-lx106-elf-gcc.exe"), recursive=True):
    zipfile.ZipFile(os.path.join(S, "lx106.zip")).extractall(os.path.join(S, "lx106"))
gcc = glob.glob(os.path.join(S, "lx106", "**", "bin", "xtensa-lx106-elf-gcc.exe"), recursive=True)[0]
print("gcc:", gcc)
print(subprocess.run([gcc, "--version"], capture_output=True, text=True).stdout.splitlines()[0])
D = os.path.join(S, "lx106probe"); os.makedirs(D, exist_ok=True)
open(os.path.join(D, "e.S"), "w").write("    .text\n    .global _start\n_start: nop\n")
subprocess.run([gcc, "-c", os.path.join(D, "e.S"), "-o", os.path.join(D, "e.o")], check=True)
subprocess.run([gcc, "-nostdlib", "-Wl,-Ttext=0x60000000", os.path.join(D, "e.o"), "-o", os.path.join(D, "e.elf")], check=True)
log = os.path.join(D, "q.log")
p = subprocess.Popen([r"D:\Software\qemu\qemu-system-xtensa.exe", "-M", "sim", "-cpu", "lx106", "-display", "none", "-monitor", "none",
                      "-serial", "none", "-kernel", os.path.join(D, "e.elf"), "-d", "in_asm,int", "-D", log])
time.sleep(3); p.kill()
print(open(log).read()[:600])
