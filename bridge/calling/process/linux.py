"""Linux process backend: process_vm_readv/writev for memory, ptrace for calls.

This is the verified path; every offset table in the project was recovered
against this build.
"""
import ctypes, os, re, struct

libc = ctypes.CDLL("libc.so.6", use_errno=True)


class iovec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]


libc.process_vm_readv.argtypes = [ctypes.c_int, ctypes.POINTER(iovec), ctypes.c_ulong,
                                  ctypes.POINTER(iovec), ctypes.c_ulong, ctypes.c_ulong]
libc.process_vm_readv.restype = ctypes.c_ssize_t
libc.process_vm_writev.argtypes = libc.process_vm_readv.argtypes
libc.process_vm_writev.restype = ctypes.c_ssize_t


def populated_ranges(fd, start, end):
    """Linux PAGEMAP_SCAN, flags=0: read metadata, never write-protect pages.

    Include resident and swapped pages, exclude the kernel's shared zero page.
    Unlike inferring emptiness from RSS or the file-page bit, PFNZERO identifies
    zero pages explicitly. ABI: linux/fs.h struct pm_scan_arg / page_region.
    """
    import fcntl
    present, swapped, zero = 1 << 3, 1 << 4, 1 << 5
    vec = (ctypes.c_uint64 * (4096 * 3))()
    out = []
    while start < end:
        arg = bytearray(struct.pack("<12Q", 96, 0, start, end, 0,
                                   ctypes.addressof(vec), 4096, 0,
                                   zero, zero, present | swapped, 0))
        n = fcntl.ioctl(fd, 0xC0606610, arg, True)
        walk_end = struct.unpack_from("<Q", arg, 32)[0]
        if not start < walk_end <= end or not 0 <= n <= 4096:
            raise OSError(22, "invalid PAGEMAP_SCAN result")
        for i in range(n):
            a, b = int(vec[3*i]), int(vec[3*i+1])
            if not start <= a < b <= walk_end:
                raise OSError(22, "invalid PAGEMAP_SCAN range")
            if out and out[-1][1] == a:
                out[-1] = (out[-1][0], b)
            else:
                out.append((a, b))
        start = walk_end
    return out


def find_pid(name="hoi4"):
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % p, "rb") as f:
                cl = f.read().split(b"\0")[0].decode("utf-8", "replace")
        except OSError:
            continue
        if cl.rsplit("/", 1)[-1] == name:
            return int(p)
    return None


class Backend:
    """Raw memory access. The typed helpers live in mem.Proc, not here."""

    def __init__(self, pid=None, exe_name="hoi4"):
        self.pid = pid or find_pid(exe_name)
        if not self.pid:
            raise RuntimeError("hoi4 process bulunamadi")
        self.maps = self._read_maps()
        self.image_base = self._image_base(exe_name)

    def _read_maps(self):
        out = []
        with open("/proc/%d/maps" % self.pid) as f:
            for line in f:
                m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (\S{4}) ([0-9a-f]+) \S+ \d+\s*(.*)", line)
                if not m:
                    continue
                out.append(dict(start=int(m.group(1), 16), end=int(m.group(2), 16),
                                perms=m.group(3), fileoff=int(m.group(4), 16),
                                path=m.group(5).strip()))
        return out

    def scan_regions(self):
        """Fresh scan ranges; direct reads are unchanged. Old kernels fall back."""
        maps = self._read_maps()
        out = []
        try:
            with open("/proc/%d/pagemap" % self.pid, "rb", buffering=0) as f:
                for m in maps:
                    if (m["path"] in ("", "[heap]") and "w" in m["perms"]
                            and m["end"] - m["start"] >= 256 << 20):
                        out.extend(dict(m, start=a, end=b) for a, b in
                                   populated_ranges(f.fileno(), m["start"], m["end"]))
                    else:
                        out.append(m)
        except OSError:
            return maps
        return out

    def _image_base(self, exe_name):
        cands = [m for m in self.maps if m["path"].endswith("/" + exe_name)]
        if not cands:
            raise RuntimeError("no hoi4 image mapping found")
        return min(m["start"] - m["fileoff"] for m in cands)

    MAX_READ = 256 << 20     # nothing legitimate is bigger; garbage sizes are

    def read(self, addr, size):
        # A size taken from a stale pointer can be billions: allocating the buffer for it
        # OOM-killed the harness at 39 GB RSS (and could take the desktop with it).
        if not 0 <= size <= self.MAX_READ:
            raise OSError(22, "refusing to read %d bytes at %#x" % (size, addr))
        buf = ctypes.create_string_buffer(size)
        # cast(buf, c_void_p) puts buf in its own _objects dictionary: a cycle
        # retains every large read buffer until cyclic GC runs (tens of GiB during
        # heap scans). buf stays alive in this frame; pass its address directly.
        l = iovec(ctypes.addressof(buf), size)
        r = iovec(ctypes.c_void_p(addr), size)
        n = libc.process_vm_readv(self.pid, ctypes.byref(l), 1, ctypes.byref(r), 1, 0)
        if n < 0:
            raise OSError(ctypes.get_errno(), "read %#x+%d" % (addr, size))
        return buf.raw[:n]

    def write(self, addr, data):
        buf = ctypes.create_string_buffer(data, len(data))
        l = iovec(ctypes.addressof(buf), len(data))
        r = iovec(ctypes.c_void_p(addr), len(data))
        n = libc.process_vm_writev(self.pid, ctypes.byref(l), 1, ctypes.byref(r), 1, 0)
        if n < 0:
            raise OSError(ctypes.get_errno(), "write %#x+%d" % (addr, len(data)))
        return n

    def regions(self):
        return self.maps


# ---------------------------------------------------------------- calls
import ctypes, os, signal, struct, time, errno

libc = ctypes.CDLL("libc.so.6", use_errno=True)
libc.ptrace.restype = ctypes.c_long
libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]

PTRACE_TRACEME=0; PTRACE_PEEKDATA=2; PTRACE_POKEDATA=5; PTRACE_CONT=7
PTRACE_KILL=8; PTRACE_SINGLESTEP=9; PTRACE_GETREGS=12; PTRACE_SETREGS=13
PTRACE_ATTACH=16; PTRACE_DETACH=17; PTRACE_SETOPTIONS=0x4200
PTRACE_SEIZE=0x4206; PTRACE_INTERRUPT=0x4207

REG_FIELDS = ["r15","r14","r13","r12","rbp","rbx","r11","r10","r9","r8","rax","rcx",
              "rdx","rsi","rdi","orig_rax","rip","cs","eflags","rsp","ss",
              "fs_base","gs_base","ds","es","fs","gs"]

class Regs(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in REG_FIELDS]
    def __repr__(self):
        return "rip=%#x rsp=%#x rax=%#x rdi=%#x" % (self.rip, self.rsp, self.rax, self.rdi)

def _ptrace(req, pid, addr=0, data=0):
    ctypes.set_errno(0)
    r = libc.ptrace(req, pid, ctypes.c_void_p(addr), ctypes.c_void_p(data))
    e = ctypes.get_errno()
    if r == -1 and e != 0:
        raise OSError(e, "ptrace(%d, %d): %s" % (req, pid, os.strerror(e)))
    return r


class CallError(Exception): pass


class Injector:
    """Stop every thread, hijack the main one to make the call, then restore it."""

    def __init__(self, proc, scratch_size=0x40000):
        self.p = proc
        self.pid = proc.pid
        self.scratch_size = scratch_size
        self.tids = []
        self.saved = None

    # ---- thread management ----
    def _all_tids(self):
        return sorted(int(t) for t in os.listdir("/proc/%d/task" % self.pid))

    # Blocking syscalls the main thread may be stopped in before it is hijacked. Stopped
    # anywhere else it can be halfway through malloc/free (glibc's tcache has no lock), a
    # container update or a command-queue push; the injected operator new, constructor
    # and post then corrupt the game's heap, and the game aborts (SIGABRT) a little later.
    # Two such aborts on 17.10 came while the game was running and orders were being
    # rewritten. Memory-management syscalls are left out: malloc makes them mid-update.
    SAFE_SYSCALLS = {0, 7, 16, 23, 35, 47, 61, 202, 230, 232, 270, 271, 281, 441}
    SAFE_TRIES = 400
    # A futex wait is the riskiest of them: the main thread may be waiting for one lock
    # while it holds another, and an injected function that takes the held one waits for
    # ever (25.10, n3e: a CanExecute for an advisor, 0.27 s on 168 earlier days, never
    # returned; the main thread stood in __futex_wait for good and the game was lost).
    # The first FUTEX_AVOID tries take only the other points (the frame loop's sleeps,
    # polls and GPU ioctls); a futex is taken after that.
    FUTEX = 202
    FUTEX_AVOID = 300

    def attach(self):
        """Stop every thread, with the main one at a safe point (see SAFE_SYSCALLS):
        when it is elsewhere, let it run on for a moment and stop it again."""
        import time as _time
        t0 = _time.time()
        for n in range(self.SAFE_TRIES + self.FUTEX_AVOID):
            self._stop_all()
            if self._main_at_safe_point(futex_ok=n >= self.FUTEX_AVOID):
                self._held_since = t0          # the game stutters from the first stop
                self._tries = n + 1
                return self.tids
            self.detach()
            time.sleep(0.002)
        raise CallError("the main thread never reached a safe point (%d tries)"
                        % (self.SAFE_TRIES + self.FUTEX_AVOID))

    def _main_at_safe_point(self, futex_ok=True):
        r = self.getregs(self.pid)
        n = ctypes.c_longlong(r.orig_rax).value
        if n == self.FUTEX and not futex_ok:
            return False
        return n in self.SAFE_SYSCALLS

    def _stop_all(self):
        self.tids = self._all_tids()
        ok = []
        for t in self.tids:
            try:
                _ptrace(PTRACE_SEIZE, t, 0, 0)
                _ptrace(PTRACE_INTERRUPT, t, 0, 0)
                os.waitpid(t, os.WUNTRACED | 0x40000000)  # __WALL
                ok.append(t)
            except OSError:
                pass
        self.tids = ok
        if self.pid not in self.tids:
            self.detach()
            raise CallError("ana thread durdurulamadi")
        return self.tids

    def detach(self):
        # how long the game stood still for us, and who held it (23.10: the user saw
        # freezes; a log line for every hold over half a second)
        since = getattr(self, "_held_since", None)
        self._held_since = None
        log = os.environ.get("HOI4_FREEZE_LOG")
        if since and log:
            import time as _time
            held = _time.time() - since
            if held > float(os.environ.get("HOI4_FREEZE_MIN", "0.15")):
                import traceback
                who = [f"{fr.name}" for fr in traceback.extract_stack()[:-1]
                       if "harness" in fr.filename or "calling" in fr.filename][-6:]
                try:
                    with open(log, "a") as fh:
                        rel = getattr(self, "_released", None)
                        fh.write("%s held %.2fs (%d stops)%s by %s\n" % (
                            _time.strftime("%H:%M:%S"), held, getattr(self, "_tries", 0),
                            " RELEASED after a %.1fs call" % rel if rel else "",
                            " > ".join(who)))
                except OSError:
                    pass
        self._released = None
        for t in list(self.tids):
            try: _ptrace(PTRACE_DETACH, t, 0, 0)
            except OSError: pass
        self.tids = []

    # ---- register ----
    def getregs(self, tid=None):
        r = Regs()
        _ptrace(PTRACE_GETREGS, tid or self.pid, 0, ctypes.addressof(r))
        return r

    def setregs(self, r, tid=None):
        _ptrace(PTRACE_SETREGS, tid or self.pid, 0, ctypes.addressof(r))

    def where(self):
        """Where is the main thread right now? Being in a syscall is relatively safe."""
        try:
            s = open("/proc/%d/task/%d/syscall" % (self.pid, self.pid)).read().split()
            return s[0]
        except OSError:
            return "?"

    # ---- a call that waits for a stopped thread ----
    CALL_GRACE = 60.0

    def _run_others(self, main):
        for t in self.tids:
            if t != main:
                try: _ptrace(PTRACE_CONT, t, 0, 0)
                except OSError: pass

    def _forward_signals(self, main):
        """A running seized thread that gets a signal stops for us: pass it on."""
        for t in self.tids:
            if t == main:
                continue
            try:
                wpid, st = os.waitpid(t, os.WNOHANG | 0x40000000)
            except ChildProcessError:
                continue
            if wpid == t and os.WIFSTOPPED(st):
                sig = os.WSTOPSIG(st) if (st >> 16) == 0 else 0
                try: _ptrace(PTRACE_CONT, t, 0, sig)
                except OSError: pass

    def _stop_others(self, main):
        ok = [main]
        for t in self.tids:
            if t == main:
                continue
            try:
                _ptrace(PTRACE_INTERRUPT, t, 0, 0)
                for _ in range(50):
                    wpid, st = os.waitpid(t, 0x40000000)
                    if not os.WIFSTOPPED(st) or (st >> 16) == 0x80:
                        break
                    # a signal arrived first: deliver it and stop again
                    _ptrace(PTRACE_CONT, t, 0, os.WSTOPSIG(st))
                    _ptrace(PTRACE_INTERRUPT, t, 0, 0)
                ok.append(t)
            except OSError:
                pass
        self.tids = ok

    # ---- calling ----
    def call(self, func_addr, args=(), scratch_writer=None, timeout=5.0, xmm_zero=True):
        """Call func_addr with args (6 register arguments, plus stack args if needed).
        scratch_writer(scratch_base) may write args into scratch and return updated
        args. Returns RAX."""
        main = self.pid
        try:
            old = self.getregs(main)
        except ProcessLookupError:
            # the game lives but its main thread was not in a ptrace stop (24.10: ESRCH
            # on GETREGS killed the harness while the game ran on): stop it all again,
            # once; a game that is gone raises as before
            if not os.path.exists("/proc/%d" % self.pid):
                raise
            self.detach()
            self.attach()
            old = self.getregs(main)
        saved = Regs.from_buffer_copy(old)

        # scratch sits well below the main thread's stack
        scratch = (old.rsp - 0x100000) & ~0xFFF
        if scratch_writer:
            args = scratch_writer(scratch) or args

        new = Regs.from_buffer_copy(old)
        argregs = ["rdi","rsi","rdx","rcx","r8","r9"]
        # every argument register is set, the unused ones to 0: a function that grew an
        # argument in a game update (1.19.4: CCreateDivisionTemplateCommand's optional
        # template, dereferenced when not NULL) then gets NULL, not the thread's leftovers
        for i, v in enumerate((tuple(args[:6]) + (0,) * 6)[:6]):
            setattr(new, argregs[i], ctypes.c_ulonglong(v & 0xFFFFFFFFFFFFFFFF).value)
        new.rax = 0
        # return address 0, so the function ends with SIGSEGV at rip=0
        # arguments 7 and beyond go on the stack, per the System V ABI:
        #   [rsp]=return address, [rsp+8]=arg7, [rsp+16]=arg8, ...
        stack_args = list(args[6:])
        sp = (scratch - 0x1000) & ~0xF
        if len(stack_args) % 2:          # rsp % 16 must be 8 at the call
            sp -= 8
        sp -= 8 * len(stack_args)
        sp -= 8
        buf = struct.pack("<Q", 0) + b"".join(
            struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF) for v in stack_args)
        self.p.write(sp, buf)
        new.rsp = sp
        new.rip = func_addr
        self.setregs(new, main)

        _ptrace(PTRACE_CONT, main, 0, 0)
        t0 = time.time()
        status = None
        released = None
        while True:
            try:
                wpid, status = os.waitpid(main, os.WNOHANG | 0x40000000)
            except ChildProcessError:
                wpid, status = 0, None
            if wpid == main:
                break
            if released is None and time.time() - t0 > timeout:
                # the function waits for a lock a stopped thread holds (25.10: the malloc
                # arena; giving up here left the main thread inside our function, and the
                # game crashed or hung for good): let the others run until it returns
                released = time.time()
                self._run_others(main)
            if released is not None:
                self._forward_signals(main)
                if time.time() - released > self.CALL_GRACE:
                    raise CallError("call hung %.0f s even with the game running (function "
                                    "%#x): the game is likely lost" % (
                                        time.time() - t0, func_addr))
            time.sleep(0.001)
        if released is not None:
            self._stop_others(main)
            self._released = time.time() - t0

        res = self.getregs(main)
        sig = os.WSTOPSIG(status) if os.WIFSTOPPED(status) else None
        rax = res.rax
        rip = res.rip
        self.setregs(saved, main)
        if sig != signal.SIGSEGV or rip != 0:
            raise CallError("unexpected state: sig=%s rip=%#x (rax=%#x)" % (sig, rip, rax))
        return rax
