"""Find CCountryTag::GetCountry in a new build: the country-scoped trigger getters all
start (after the frame prologue) with `lea rdi,[rsi+0x8]; call GetCountry`, so the most common call target wins."""
import collections, os, re, subprocess, sys
bridge = sys.argv[1]
sys.path[:0] = [os.path.join(bridge, "calling"), os.path.join(bridge, "research")]
import config, rtti
from elf import ELF
e = ELF(config.binary_path()); m = rtti.build(); rel, _ = rtti.all_relocs(e)
targets = collections.Counter(); n = 0
for k, vps in m["vtables"].items():
    if not re.fullmatch(r"\d+C[A-Za-z0-9_]*Trigger", k):
        continue
    for slot in (0xC0, 0xB8, 0x50):
        f = rel.get(vps[0] + slot)
        if not f:
            continue
        out = subprocess.run(["objdump", "-d", "-M", "intel", "--start-address=%#x" % f,
                              "--stop-address=%#x" % (f + 0x40), config.binary_path()],
                             capture_output=True, text=True).stdout
        ins = [l.split("\t")[-1].strip() for l in out.splitlines()[7:]]
        hit = [i for i in range(min(8, len(ins) - 1))
               if ins[i].startswith("lea    rdi,[rsi+0x8]") and ins[i + 1].startswith("call")]
        if hit:
            targets[int(ins[hit[0] + 1].split()[1], 16)] += 1; n += 1
            break
print("getters with the pattern:", n)
for t, c in targets.most_common(5):
    print(hex(t), c)
