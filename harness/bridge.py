"""The only door to the game.

Imports the ported Clausewitz-MCP (bridge/) and wraps it in a gateway with ONE worker
thread: every read and every write goes through it, in order. Models may think in
parallel; the game is touched by one caller at a time, because an injected call stops
all of the game's threads and two at once would corrupt it.
"""
import concurrent.futures
import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.environ.get("LECHONK_BRIDGE", os.path.join(ROOT, "bridge"))
for sub in ("calling", "research"):
    path = os.path.join(BRIDGE, sub)
    if path not in sys.path:
        sys.path.insert(0, path)

CO_STATES = 0x460          # CCountry -> CPdxArray<CState*>, owned states


def module(name):
    return importlib.import_module(name)


def game_dir():
    return module("config").game_dir()


def arr(p, obj, off):
    return module("game")._arr(p, obj, off)


class Gateway:
    """Serialises all access to the running game on a single thread. Urgent calls (the
    pause on stop) go ahead of the queued ones: on 17.10 a stop waited behind the
    briefings in the queue and the game ran on for days."""

    def __init__(self, pid=None):
        import itertools
        import queue
        import threading
        self._q, self._n = queue.PriorityQueue(), itertools.count()
        threading.Thread(target=self._work, name="game", daemon=True).start()
        self.g = None
        self.g = self._put(1, lambda _g: self._open(pid)).result()

    def _work(self):
        while True:
            _, _, fut, fn, args, kw = self._q.get()
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(self.g, *args, **kw))
            except BaseException as e:
                fut.set_exception(e)

    def _put(self, prio, fn, *args, **kw):
        fut = concurrent.futures.Future()
        self._q.put((prio, next(self._n), fut, fn, args, kw))
        return fut

    @staticmethod
    def _open(pid):
        g = module("game").Game(pid)    # refuses a binary the offsets were not made for
        Gateway._calibrate_clock(g)
        # the object index is cached on disk by pid, and a loaded save keeps the pid:
        # i50 found the doctrine status of the old game and crashed
        g.oi.build(force=True)
        g._load_tags()
        return g

    @staticmethod
    def _calibrate_clock(g):
        # the cached date address outlives a loaded save and then reads a frozen date
        # (i48, 21.10: 1939.11.07.08 read while the game ran to Vichy; nobody woke)
        sess = module("session")
        import time
        def paused():
            ok, err = sess.pause(g, True)
            if not ok or not sess.is_paused(g):
                raise RuntimeError("cannot pause for clock discovery: %s" % err)
        def tick():
            # The timer is enforced by the game, even if Python is delayed. Never
            # let a memory scan or an interrupted startup run the clock unbounded.
            try:
                # 1.19.4 handler 0x1c654c0 sets the timer (+0x908), speed 5 and
                # unpauses itself. Its SResult.ok stays false on success; verify
                # the actual clock movement after it auto-pauses instead.
                g.pause_in_hours(1)
                deadline = time.monotonic() + 10
                while not sess.is_paused(g) or g.hours_until_pause > 0:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("clock calibration did not auto-pause")
                    time.sleep(0.05)
            finally:
                paused()
        paused()
        try:
            g.find_live_date_address(advance=tick)
        finally:
            paused()

    def run(self, fn, *args, **kw):
        """Run fn(g, *args) on the game thread and wait for the result."""
        return self._put(1, fn, *args, **kw).result()

    def urgent(self, fn, *args, **kw):
        """run(), ahead of everything queued."""
        return self._put(0, fn, *args, **kw).result()

    def submit(self, fn, *args, **kw):
        """Same, as a concurrent.futures.Future (for asyncio.wrap_future)."""
        return self._put(1, fn, *args, **kw)

    def close(self):
        pass
