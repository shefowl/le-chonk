"""The in-game panel (mod/le_chonk, built by mod/build.py) and the harness.

Into the harness: the panel's buttons set country flags and bump the variable lc_dirty.
The harness reads lc_dirty every few seconds through the console; when it moved, it reads
the flags:
- lc_off_<agent> and lc_<theatres|fleets>_<tagged|none> go into roster.json, which the run
  applies as before;
- lc_mute_<agent>, lc_feed_important: the feed's filter;
- lc_agent (the agent window open, an index of mod/build.py STAFF), lc_agent_log (the
  window shows its reports, not the chat);
- lc_send: the player wrote to the agent of the open window (the input box);
- lc_report: the player asks that agent for a report;
- lc_answer_yes / lc_answer_no: the answer to the question shown by event lechonk.1.
When roster.json changes by hand it is pushed into the game (the mod's lc_set_* effects).
Without the mod every console call here fails quietly and roster.json alone rules.

Out to the game: every text the panel shows is a marker in the mod's localisation
(human defaults with zero-width palette signatures, fixed byte widths in mod/build.py).
update_loc does not re-read
a key (23.10: "Didn't find key in tag file", ours too), so the markers are found once in
the game's memory and overwritten in place, padded with spaces to their width.
"""
import importlib.util
import hashlib
import json
import os
import re
import threading
import time

from . import roster

MOD = os.path.join(roster.ROOT, "mod", "le_chonk")
POLL = 0.25                    # seconds between reads of lc_dirty (its own thread)
RECHECK = 60.0                 # seconds between looks for the mod when it was absent


def _build():
    """mod/build.py's tables (STAFF, CHAT, BADGE, N_FEED ...), so the two never drift."""
    spec = importlib.util.spec_from_file_location(
        "lc_build", os.path.join(roster.ROOT, "mod", "build.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


BUILD = _build()
STAFF = [k for k, _, _, _ in BUILD.STAFF]


def _yes(msg):
    return bool(re.search(r"Result = yes", msg or ""))


def _number(msg):
    # "lc_dirty = 3" or, when the value is a tag's index, "lc_test = Holland (7)"
    m = re.search(r"\((-?\d+(?:\.\d+)?)\)\s*$", (msg or "").strip()) or \
        re.search(r"=\s*(-?\d+(?:\.\d+)?)", msg or "")
    return float(m.group(1)) if m else None


# Characters the game's fonts lack in the English game (24.10, tested in the panel: they
# show as "?"), and what stands for them. Drawn fine: Latin-1, Polish, Czech, Hungarian,
# Turkish, Cyrillic, en and em dashes, curly quotes, guillemets, ° ± × £ ¥. Not drawn:
# CJK and Greek (only the game's own CJK fonts would have them).
SUBST = {"\u2026": "...", "\u2022": "-", "\u2116": "No.", "\u2192": "->", "\u2190": "<-",
         "\u20ac": "EUR", "\u0219": "\u015f", "\u021b": "\u0163", "\u0218": "\u015e",
         "\u021a": "\u0162", "\u2212": "-", "\u2248": "~", "\u2264": "<=", "\u2265": ">="}


CJK = ("ja", "zh", "ko")
LANG = {"lang": None, "at": 0.0}


def _lang():
    """The game's language, looked up again every 30 s (the player may restart the game
    in another one)."""
    if time.time() - LANG["at"] > 30:
        from .chat import game_language
        LANG.update(lang=game_language(), at=time.time())
    return LANG["lang"]


def _cjk_fold(ch):
    """In a CJK game the fonts have the CJK script and ASCII only (24.10, Japanese: Polish,
    Cyrillic, Greek and guillemets came out as "?"): Latin letters lose their marks."""
    o = ord(ch)
    if o < 128 or 0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF or 0xFF00 <= o <= 0xFFEF:
        return ch
    import unicodedata
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if ord(c) < 128)
    return base or {"\u00ab": '"', "\u00bb": '"', "\u201e": '"', "\u201c": '"',
                    "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u2013": "-",
                    "\u2014": "-", "\u0142": "l", "\u0141": "L", "\u00df": "ss",
                    "\u00f8": "o", "\u00d8": "O"}.get(ch, ch)


MULTILINE = ("PL_TEXT", "FEED", "CHAT", "DR_TEXT", "PL_IDEA", "PL_WAR_1", "PL_WAR_2", "PL_WAR_3")
# Compatibility for an already running previous mod: the engine dropped the
# signature at the end of these oversized values. Only their ENTIRE exact initial
# 6144-byte copy authorises discovery, then the usual frozen hash check applies.
NATIVE_LOC_LIMIT = 6144
TRUNCATED_READERS = {"PL_TEXT": 24000, "CHAT": 12000, "FEED": 9000, "DR_TEXT": 9000}


def marker_widths(key):
    return {BUILD.MARKERS.get(key)} | ({NATIVE_LOC_LIMIT} if key in TRUNCATED_READERS else set())


def _pad_native(data, width):
    """Palette escapes occupy bytes, but no glyphs or blank scrollable rows."""
    remaining = width - len(data)
    if remaining < 0:
        raise ValueError('text exceeds buffer')
    return data + b' ' * (remaining % 2) + b'\x11W' * (remaining // 2)


def _fit_lines(text, width):
    """A text of several lines: each line that ends in a multi-byte character gets the
    0x11! tail too (as _fit), then the whole is fitted."""
    lines = []
    for ln in (text or "").split("\n"):
        ln = "".join(SUBST.get(ch, ch) for ch in ln).rstrip()
        if _lang() in CJK:
            ln = "".join(_cjk_fold(ch) for ch in ln)
        if ln and ord(ln[-1]) > 127:
            ln += "\x11!"
        lines.append(ln)
    b = "\n".join(lines).encode("utf-8")
    if len(b) > width:
        b = b[:width]
        while b and b[-1] != 0x0A:     # cut at the last whole line
            b = b[:-1]
    return _pad_native(b, width)


def _fit(text, width):
    """UTF-8 bytes of text, cut on a character boundary, padded with zero-width palette escapes.
    The game drops the last character of a text that ends in a multi-byte one (24.10:
    "спокойно" showed as "спокойн"), so such a text ends in the colour reset 0x11 "!"
    (ASCII, drawn as nothing in every font; a no-break space showed as "?" in Japanese)."""
    text = "".join(SUBST.get(ch, ch) for ch in (text or "").replace("\n", " ")).rstrip()
    if _lang() in CJK:
        text = "".join(_cjk_fold(ch) for ch in text)
    while text:
        t = text + "\x11!" if ord(text[-1]) > 127 else text
        b = t.encode("utf-8")
        if len(b) <= width:
            return _pad_native(b, width)
        # too long: cut by what does not fit, at least one character
        text = text[:max(0, len(text) - max(1, (len(b) - width) // 4))].rstrip()
    return _pad_native(b"", width)


def marker_buffers(blob):
    """Discover only complete exact initial buffers, with legacy compatibility.

    The palette tail identifies a candidate; matching the entire locale value
    establishes its boundary. Hash proof is still rechecked while threads freeze.
    """
    signatures = {BUILD.marker_signature(key).encode(): key for key in BUILD.MARKERS}
    pattern = re.compile(rb"\x11H\x11C\x11Y(?:\x11[WYRGBCH!]){11}\x11!")
    for match in pattern.finditer(blob):
        key = signatures.get(match.group())
        if key is None:
            continue
        start = match.end() - BUILD.MARKERS[key]
        if start < 0:
            continue
        candidate = blob[start:match.end()]
        if any(candidate == BUILD.marker(key, lang).encode('utf-8')
               for lang in ('english', 'russian')):
            yield key, start, candidate
    for match in re.finditer(rb"LCM_([A-Z0-9_]+?)_(\.+)", blob):
        key = match.group(1).decode()
        if len(match.group()) == BUILD.MARKERS.get(key):
            yield key, match.start(), match.group()
    for key, old_width in TRUNCATED_READERS.items():
        for lang in ('english', 'russian'):
            initial = BUILD.marker(key, lang, width=old_width).encode()[:NATIVE_LOC_LIMIT]
            offset = blob.find(initial)
            while offset >= 0:
                yield key, offset, initial
                offset = blob.find(initial, offset + len(initial))


class ModUI:
    def __init__(self, gw, log=print):
        self.gw = gw
        self.log = log
        self.present = None        # the mod answered (None: not looked yet)
        self.looked = 0.0
        self.polled = 0.0
        self.dirty = None          # lc_dirty as last read
        self.ours_mtime = None     # roster.json time of our own last write
        self.pushed_mtime = None   # roster.json time last pushed into the game
        self.shown = {}            # marker name -> text written
        self.at = None             # marker name -> [(address, width)]
        self.expected = {}         # address -> hash of bytes we last observed/wrote
        self.online = False
        self.closed = False
        self.activity_fn = None
        self.activity_sync_at = 0.0
        self.activity_cache = {}
        self.feedback = {}
        self.needs_paint_ack = False
        self.on_message = None     # fn(agent, text): the player wrote in the agent window
        self.on_report = None      # fn(agent): the player asked the agent for a report
        self.setup_process = None
        self.settings_fn = None    # fn() -> {setting: value} in force (the SETTINGS tab)
        self.muted = set()         # agents whose reports the feed hides
        self.important = False     # the feed shows only what matters
        self.agent = None          # the agent of the open window
        self.agent_log = False     # the window shows the agent's reports, not the chat
        self.changed = False       # the panel was clicked: write its texts now
        self.feed_page = 0         # pages back from the newest (OLDER / NEWER)
        self.part_counts = {}
        self.part_sync_at = {}
        self.plan_page = 0
        self.chat_page = 0
        self.directive_page = 0
        self.question = None       # (on_yes, on_no) while a question is asked
        self.tag = None            # the player's country
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ console
    def _con(self, cmd):
        try:
            r = self.gw.run(lambda g: g.console(cmd))
            return r.ok, r.message or ""
        except Exception as e:
            return False, str(e)

    # The console runs on whatever the player has selected (23.10: a general, and every
    # flag read "no"), so each command names the country.
    def _tag(self):
        if self.tag is None:
            try:
                self.tag = self.gw.run(lambda g: g.player_tag())
            except Exception:
                return None
        return self.tag

    def _flag(self, name):
        return _yes(self._con("eval_trigger %s = { has_country_flag = %s }"
                              % (self._tag(), name))[1])

    def _var(self, name):
        return _number(self._con("get_var %s.%s" % (self._tag(), name))[1])

    def set_page(self, var, page):
        """Put a page counter of the panel back in range."""
        self._con("set_var %s.%s %d" % (self._tag(), var, int(page)))
        setattr(self, {"lc_feed_page": "feed_page", "lc_chat_page": "chat_page",
                       "lc_plan_page": "plan_page", "lc_directive_page": "directive_page"}[var], int(page))

    def reader_bounds(self, kind, total):
        """The native scrollbar handles a part; page buttons only cross buffers."""
        if kind not in ('plan', 'feed', 'chat', 'directive'):
            raise ValueError('unknown reader')
        last = max(0, total - 1)
        with self.lock:
            now = time.monotonic()
            # A save can roll back UI variables without changing this process.
            expired = now - self.part_sync_at.get(kind, float('-inf')) >= 30
            if self.present and (self.part_counts.get(kind) != last or expired):
                ok, _ = self._con('set_var %s.lc_%s_last %d' % (self._tag(), kind, last))
                if ok:
                    self.part_counts[kind] = last
                    self.part_sync_at[kind] = now

    def _effect(self, name):
        return self._con("effect %s %s" % (self._tag(), name))

    def connect(self):
        with self.lock:
            if self.closed or not self.present or self.online:
                return
            ok, msg = self._effect('lc_ui_connect')
            if ok and 'Invalid' not in msg:
                self.online = True
                self.activity_cache = {key: 0 for key in BUILD.CHAT}
                self.activity_sync_at = time.monotonic()
                self.needs_paint_ack = True
                self.changed = True

    def disconnect(self):
        with self.lock:
            self.closed = True
            was_online, self.online = self.online, False
            try:
                if was_online:
                    self._effect('lc_ui_disconnect')
                    ru = _lang() == 'ru'
                    self.write({'HEAD': 'Штаб не подключён' if ru else 'Headquarters offline',
                                'AG_ST': 'Штаб не подключён' if ru else 'Headquarters offline',
                                'SENT': ''})
            except Exception as error:
                self.log('in-game panel: disconnect failed: %s' % type(error).__name__)
            finally:
                self.online = False
                self.activity_cache.clear()

    def sync_activity(self):
        with self.lock:
            if not self.online:
                return
            now = time.monotonic()
            if now - self.activity_sync_at >= 30:
                # Country UI variables are saved; Python caches are not restored by a reload.
                self._effect('lc_ui_keepalive')
                self.activity_cache.clear()
                self.activity_sync_at = now
            active = self.activity_fn() if self.activity_fn else {}
            for key in BUILD.CHAT:
                what = active.get(key, ('',))[0]
                value = 2 if what == 'writing a report' else 1 if what else 0
                if self.activity_cache.get(key) != value:
                    ok, _ = self._con('set_var %s.lc_busy_%s %d' % (self._tag(), key, value))
                    if ok:
                        self.activity_cache[key] = value

    def request_feedback(self, key, text):
        self.feedback[key] = text
        if key == self.agent:
            self.write({'SENT': text})
        self.changed = True

    def finish_request(self, key, kind, error=None):
        with self.lock:
            ru = _lang() == 'ru'
            if error:
                text = ('Запрос не выполнен (%s). Попробуй ещё раз.' if ru else
                        'Request failed (%s). Try again.') % error
            elif kind == 'writing a report':
                text = 'Доклад получен.' if ru else 'Report received.'
            else:
                text = 'Ответ получен.' if ru else 'Reply received.'
            text = ('\x11R' if error else '\x11G') + text + '\x11!'
            self.request_feedback(key, text)
            self.sync_activity()

    def _look(self):
        ok, msg = self._effect("lc_ping")
        present = ok and "Invalid" not in msg
        if present != self.present:
            self.log("in-game panel: %s" % ("the mod answers" if present else
                                            "no mod (roster.json alone)"))
        self.present = present
        self.looked = time.time()

    # ------------------------------------------------------------------ the loop
    def poll(self):
        with self.lock:
            self._poll()

    def _poll(self):
        now = time.time()
        if self.present is None or (not self.present and now - self.looked > RECHECK):
            self._look()
        if self.present and not self.closed:
            self.connect()
            self.sync_activity()
        if self.closed or not self.present or now - self.polled < POLL:
            return
        self.polled = now
        self._configuration_request()
        n = self._var("lc_dirty")
        if n is None:
            return
        try:
            mt = os.path.getmtime(roster.PATH)
        except OSError:
            mt = None
        if self.dirty is None:
            # a new session: the game starts from roster.json
            self.dirty = n
            self.push()
            self._filter()         # the panel as the player left it
            self._window()
            self.requests()        # a Send or an answer clicked while the harness was away
            return
        if n != self.dirty:
            if n < self.dirty:
                self.part_counts.clear()
                self.part_sync_at.clear()
                self.online = False
                self.shown.clear()
                self.at = None
                self.connect()
                self.sync_activity()
            # one click since the last look: lc_what says what it changed; more: all
            what = int(self._var("lc_what") or 0) if n - self.dirty == 1 else 0
            self.dirty = n
            self.pull(what)
        elif mt is not None and mt not in (self.ours_mtime, self.pushed_mtime):
            self.push()

    def _configuration_request(self):
        """Consume a latched request even when other clicks coalesce lc_what."""
        if not self._flag('lc_setup_requested'):
            return
        self._effect('lc_setup_consumed')
        if self.setup_process is not None and self.setup_process.poll() is None:
            return
        import subprocess
        import sys
        try:
            self.setup_process = subprocess.Popen(
                [sys.executable, os.path.join(roster.ROOT, 'tools', 'configure_model.py')],
                cwd=roster.ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True)
            self.log('API setup opened in a separate window; restart harness after saving.')
        except OSError:
            self.log('Cannot open API setup. Run python3 tools/configure_model.py in a desktop terminal.')

    def pull(self, what=0):
        """The panel was clicked: what changed (mod/build.py WHAT; 0: all) is read; the
        switches go into roster.json, Send and Report to the run."""
        W = BUILD.WHAT
        self.changed = True
        self.needs_paint_ack = True
        if what == W['connect']:
            self.online = False
            self.connect()
            self.sync_activity()
            self._window()
            return
        if what == W["feed_page"]:
            self.feed_page = max(0, int(self._var("lc_feed_page") or 0))
            return
        if what == W["plan_page"]:
            self.plan_page = max(0, int(self._var("lc_plan_page") or 0))
            return
        if what == W["directive_page"]:
            self.directive_page = max(0, int(self._var("lc_directive_page") or 0))
            return
        if what == W["window"]:
            self._window()
            return
        if what == W["filter"]:
            self._filter()
            return
        if what == W["settings"]:
            self._settings()
            return
        if what in (W["send"], W["answer"]):
            if what == W["send"]:
                self.agent_log, self.chat_page = False, 0    # the click set them so
            self.requests()
            return
        self._roster()
        if what == W["roster"]:
            return
        self._settings()
        self._filter()
        self._window()
        self.requests()

    def _filter(self):
        self.muted = {k for k in STAFF if self._flag("lc_mute_%s" % k)}
        self.important = self._flag("lc_feed_important")
        self.feed_page = max(0, int(self._var("lc_feed_page") or 0))
        self.plan_page = max(0, int(self._var("lc_plan_page") or 0))

    def _window(self):
        i = self._var("lc_agent")
        self.agent = STAFF[int(i)] if i is not None and 0 <= int(i) < len(STAFF) else None
        self.agent_log = self._flag("lc_agent_log")
        self.chat_page = max(0, int(self._var("lc_chat_page") or 0))
        self.directive_page = max(0, int(self._var("lc_directive_page") or 0))

    def directive_texts(self, directives, language):
        """Read every directive through bounded native pages; compact rows are previews."""
        from .ui_text import reader_pages
        text = '\n\n'.join('%d. %s' % (i, value) for i, value in enumerate(directives, 1))
        text = text or ('Постоянных директив нет.' if language == 'ru' else 'No standing directives.')
        pages = reader_pages(text, 756, BUILD.MARKERS['DR_TEXT'], language, max_lines=16)
        self.reader_bounds('directive', len(pages))
        page = min(self.directive_page, len(pages) - 1)
        if page != self.directive_page:
            self.set_page('lc_directive_page', page)
        return {'DR_TEXT': pages[page], 'DR_PAGE':
                ('Часть %d / %d' if language == 'ru' else
                 'Part %d / %d') % (page + 1, len(pages))}

    def _roster(self):
        d = dict(roster.get())
        for k in roster.AGENTS:
            d[k] = "off" if self._flag("lc_off_%s" % k) else "on"
        for k in ("theatres", "fleets"):
            d[k] = ("tagged" if self._flag("lc_%s_tagged" % k) else
                    "none" if self._flag("lc_%s_none" % k) else "all")
        d["ask_war"] = "on" if self._flag("lc_ask_war") else "off"
        self._save(d)

    def _settings(self):
        """The SETTINGS tab: what differs from the settings in force goes into roster.json."""
        if not self.settings_fn:
            return
        cur = self.settings_fn()
        d = dict(roster.get())
        for k, lo, hi, _, _ in roster.SETTINGS:
            n = self._var("lc_s_" + k)
            if n is not None and lo <= int(n) <= hi and int(n) != cur.get(k):
                d[k] = int(n)
        for a in roster.PAUSE_AGENTS:
            n = self._var("lc_pm_" + a)
            if n is not None and 0 <= int(n) < len(roster.PAUSE_MODES):
                m = roster.PAUSE_MODES[int(n)]
                if m != cur.get("pause_" + a):
                    d["pause_" + a] = m
        self._save(d)

    def _save(self, d):
        if d != roster.get():
            tmp = roster.PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, roster.PATH)
            self.ours_mtime = os.path.getmtime(roster.PATH)
            self.log("in-game panel: %s" % roster.describe())

    def requests(self):
        with self.lock:
            self._requests()

    def _requests(self):
        """A queued click is not delivery; admission and completion have explicit feedback."""
        ru = _lang() == 'ru'
        for flag, callback in (('lc_send', self.on_message), ('lc_report', self.on_report)):
            if not self._flag(flag):
                continue
            index = self._var('lc_request_agent')
            to = STAFF[int(index)] if index is not None and 0 <= int(index) < len(STAFF) else self.agent
            self._effect('lc_clear_' + flag[3:])
            if not self.online or callback is None:
                self.request_feedback(to, 'Штаб не подключён. Запусти харнесс.' if ru else
                                      'Headquarters offline. Start the campaign harness.')
                continue
            if to not in BUILD.CHAT:
                self.request_feedback(to, 'Открой окно агента.' if ru else 'Open an agent window first.')
                continue
            text = self.read_input() if flag == 'lc_send' else None
            if flag == 'lc_send' and not text:
                self.request_feedback(to, ('Не удалось прочитать поле. Попробуй ещё раз.' if ru else
                                          'Could not read the input. Try again.') if text is None else
                                      ('Введи сообщение перед отправкой.' if ru else
                                       'The message is empty. Type something first.'))
                continue
            try:
                accepted = callback(to, text) if flag == 'lc_send' else callback(to)
                if accepted is False:
                    self.request_feedback(to, 'Агент уже отвечает. Дождись результата.' if ru else
                                          'The agent is already responding. Wait for the result.')
                else:
                    self.request_feedback(to, ('Сообщение принято. Ожидается ответ.' if ru else
                                              'Message accepted. Waiting for a reply.') if flag == 'lc_send' else
                                          ('Доклад запрошен. Ожидается ответ.' if ru else
                                           'Report requested. Waiting for the agent.'))
            except Exception as error:
                self.finish_request(to, '', type(error).__name__)
            self.sync_activity()
        for flag, yes in (("lc_answer_yes", True), ("lc_answer_no", False)):
            if self._flag(flag):
                self._effect("lc_clear_%s" % flag[3:])
                q, self.question = self.question, None
                self.log("in-game panel: the player answered %s" % ("yes" if yes else "no"))
                if q:
                    (q[0] if yes else q[1])()

    def push(self):
        """roster.json into the game's flags."""
        d = roster.get()
        for k in roster.AGENTS:
            self._effect("lc_set_%s_%s" % ("off" if d[k] == "off" else "on", k))
        for k in ("theatres", "fleets"):
            self._effect("lc_set_%s_%s" % (k, d[k]))
        self._effect("lc_set_ask_war_%s" % d.get("ask_war", "off"))
        if self.settings_fn:
            for k, v in self.settings_fn().items():
                if k.startswith("pause_"):
                    self._con("set_var %s.lc_pm_%s %d" % (self._tag(), k[6:],
                                                          roster.PAUSE_MODES.index(v)))
                else:
                    self._con("set_var %s.lc_s_%s %d" % (self._tag(), k, v))
        try:
            self.pushed_mtime = os.path.getmtime(roster.PATH)
        except OSError:
            self.pushed_mtime = None

    # ------------------------------------------------------------------ the player asked
    def ask(self, title, text, on_yes, on_no):
        """Show the player a question (event lechonk.1: Approve / Reject)."""
        if not self.present:
            return False
        self.write({"EV_TITLE": title, "EV_DESC": text})
        self.question = (on_yes, on_no)
        self._effect("lc_ask")
        return True

    def read_input(self):
        """The text in the panel's input box (8CEditBox named lc_input)."""
        try:
            return self.gw.run(_read_editbox)
        except Exception as e:
            self.log("in-game panel: input box: %s" % e)
            return None

    # ------------------------------------------------------------------ text out
    def _valid_marker(self, p, addr, width):
        data = p.try_read(addr, width)
        return (data is not None and len(data) == width and
                hashlib.sha256(data).hexdigest() == self.expected.get(addr))

    def _save_markers(self, g, found):
        cache = os.path.join(roster.ROOT, "logs", ".lc_markers.json")
        rec = {"pid": g.p.pid, "base": g.p.base, "v": 3, "at": found,
               "expected": {str(a): self.expected[a] for ls in found.values()
                            for a, _ in ls if a in self.expected}}
        with open(cache + ".tmp", "w") as f:
            json.dump(rec, f)
        os.replace(cache + ".tmp", cache)

    def _find(self):
        """{marker name: [(address, width)]}, found once a game session (cached by pid)."""
        cache = os.path.join(roster.ROOT, "logs", ".lc_markers.json")

        def scan(g):
            # once the harness wrote a text over a marker, the marker is gone from memory
            # and only this cache knows where it was (26.10: the cache removed mid-game, a
            # rescan found 4 of 42): a new scan only ADDS what the cache lacks
            p = g.p
            found = {}
            try:
                with open(cache) as f:
                    c = json.load(f)
                # PID/base survive a save reload. Version 2 had no content proof and
                # wrote into reused allocations, including a CCountryAI vptr.
                if c.get("pid") == p.pid and c.get("base") == p.base and c.get("v") == 3:
                    self.expected = {int(a): h for a, h in c["expected"].items()}
                    for k, entries in c["at"].items():
                        valid = [(a, w) for a, w in entries
                                 if w in marker_widths(k) and
                                 self._valid_marker(p, a, w)]
                        if valid:
                            found[k] = valid
                    if set(BUILD.MARKERS) <= set(found):
                        return found
            except (OSError, ValueError, KeyError):
                pass
            known = set(found)
            overlap = max(max(BUILD.MARKERS.values()), NATIVE_LOC_LIMIT) - 1
            for m in p.regions(writable=True):
                a = m['start']
                while a < m['end']:
                    n = min(64 << 20, m['end'] - a)
                    buf = p.try_read(a, n)
                    if buf:
                        for key, offset, data in marker_buffers(buf):
                            if key not in known:
                                addr = a + offset
                                entry = (addr, len(data))
                                if entry not in found.setdefault(key, []):
                                    found[key].append(entry)
                                    self.expected[addr] = hashlib.sha256(data).hexdigest()
                    if a + n >= m['end']:
                        break
                    a += max(1, n - overlap)
            self._save_markers(g, found)
            return found
        return self.gw.run(scan)

    def write(self, texts):
        """{marker name: text}: the changed ones are written over their markers."""
        with self.lock:
            self._write(texts)
            if self.online and self.needs_paint_ack and 'HEAD' in texts:
                good = all(self.shown.get(k) == text for k, text in texts.items() if k in BUILD.MARKERS)
                ok, _ = self._con('set_var %s.lc_painted_dirty %s' % (self._tag(), self.dirty or 0))
                if ok:
                    accepted, message = self._effect('lc_ui_paint_ok' if good else 'lc_ui_paint_error')
                    if accepted and 'Invalid' not in message:
                        self.needs_paint_ack = not good

    def _write(self, texts):
        if not self.present:
            return
        if self.at is None:
            self.at = self._find()
            self.log("in-game panel: %d text fields found in memory" % len(self.at))
        todo = {k: t for k, t in texts.items()
                if k in self.at and self.shown.get(k) != t}
        if not todo:
            return

        def go(g):
            written = {}
            own = g._inj is None
            # Prevent the engine freeing a buffer between validation and the write.
            if own:
                g.attach()
            try:
                for k, t in todo.items():
                    valid = []
                    for addr, width in self.at[k]:
                        if not self._valid_marker(g.p, addr, width):
                            self.expected.pop(addr, None)
                            self.log("in-game panel: discarded reused marker %s at %#x" % (k, addr))
                            continue
                        data = _fit_lines(t, width) if k in MULTILINE else _fit(t, width)
                        g.p.write(addr, data)
                        self.expected[addr] = hashlib.sha256(data).hexdigest()
                        valid.append((addr, width))
                    self.at[k] = valid
                    if valid:
                        written[k] = t
            finally:
                if own:
                    g.detach()
            self._save_markers(g, self.at)
            return written
        self.shown.update(self.gw.run(go))


# Found 23.10 with the player typing (probe_input): a CEditBox keeps its text as a
# std::string {ptr, size, capacity|inline buffer} at +0x438; at +0x60 is the pointer to the
# element's definition from the .gui, whose name is a std::string at +0x28.
EB_TEXT = 0x438
EB_DEF = 0x60
DEF_NAME = 0x28
INPUT = b"lc_input"
_box = {"addr": None, "vt": None, "pid": None}


def _std_string(p, a, cap=4096):
    raw = p.try_read(a, 16)
    if not raw or len(raw) < 16:
        return None
    ptr, n = int.from_bytes(raw[:8], "little"), int.from_bytes(raw[8:], "little")
    if not 0 <= n <= cap or ptr < 0x10000:
        return None
    return p.try_read(ptr, n) if n else b""


def _is_input(p, a):
    d = p.try_read(a + EB_DEF, 8)
    if not d:
        return False
    return _std_string(p, int.from_bytes(d, "little") + DEF_NAME, 64) == INPUT


def _read_editbox(g):
    """The text of our input box, or None when the box is not found. The box is looked
    for once a game session (a scan of the heap for CEditBox, a few seconds) and then
    checked by its vtable and name before every read."""
    p = g.p
    a = _box["addr"] if _box["pid"] == p.pid else None
    if a is not None:
        vt = p.try_read(a, 8)
        if not vt or int.from_bytes(vt, "little") != _box["vt"] or not _is_input(p, a):
            a = None
    if a is None:
        found = g.oi.scan(["CEditBox"]).get("CEditBox", [])
        a = next((x for x in found if _is_input(p, x)), None)
        if a is None:
            return None
        _box.update(addr=a, vt=int.from_bytes(p.read(a, 8), "little"), pid=p.pid)
    raw = _std_string(p, a + EB_TEXT)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return raw.decode("latin-1").strip()


def probe_input(g, typed):
    """For finding the input box's text (run with the player having typed `typed`): the
    addresses of `typed` in memory and the CEditBox objects near them."""
    p = g.p
    needle = typed.encode("utf-8")
    hits = []
    for m in p.regions(writable=True):
        a = m["start"]
        while a < m["end"]:
            n = min(64 << 20, m["end"] - a)
            buf = p.try_read(a, n)
            if buf:
                i = buf.find(needle)
                while i >= 0:
                    hits.append(a + i)
                    i = buf.find(needle, i + 1)
            a += n
    g.oi.scan(["CEditBox"])
    boxes = g.oi.instances("CEditBox")
    out = {"hits": [hex(h) for h in hits], "boxes": len(boxes), "near": []}
    for b in boxes:
        blob = p.try_read(b, 0x800) or b""
        for off in range(0, len(blob) - 8, 8):
            v = int.from_bytes(blob[off:off + 8], "little")
            for h in hits:
                if 0 <= h - v < 64:
                    out["near"].append((hex(b), hex(off), hex(v), h - v))
    return out
