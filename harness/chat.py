"""The player talks to one agent: a thread of its own, apart from the agent's turns.

The user, 24.10: the player plays the country and talks to an agent directly ("Industry:
build only factories"). The agent sees its role, a fresh picture of its sphere and this
chat, not the other agents' turns. It answers, and keeps the list of the player's standing
directives: a directive is given, changed or cancelled only by writing. Every regular turn
of the agent gets the list, above Le Chonk and the plan.

"Report": the agent writes the player a plain report on its sphere, in the game's language
(the agents talk among themselves in English).

The store is logs/chat.json: {agent: {"log": [[who, text, date]], "directives": [...]}}.
"""
import json
import os
import threading
import time

from .agents import split_content

KEEP = 40                     # chat entries kept per agent
TURNS = 6                     # the agent's own last summaries shown to it

REPLY = {"type": "function", "function": {
    "name": "reply",
    "description": "Answer the player, and give the full list of the player's standing "
                   "directives to you as it stands after this message.",
    "parameters": {"type": "object", "required": ["answer", "directives", "shown"],
                   "properties": {
        "answer": {"type": "string",
                   "description": "Your answer to the player, in the player's language "
                                  "(given in the instructions). Short and concrete. When the "
                                  "player gave, changed or cancelled a directive, say in your "
                                  "own words how you understood it."},
        "directives": {"type": "array", "items": {"type": "string"},
                       "description": "Every standing directive in force after this message, "
                                      "in English, one short imperative each. Unchanged if the "
                                      "player gave none; without the ones the player cancelled."},
        "shown": {"type": "array", "items": {"type": "string"},
                  "description": "The same directives, in the same order, in the player's "
                                 "language, for the player's screen."}}}}}

REPORT = {"type": "function", "function": {
    "name": "report",
    "description": "Your report to the player.",
    "parameters": {"type": "object", "required": ["report"], "properties": {
        "report": {"type": "string"}}}}}

LANGS = {"ru": "Russian", "en": "English", "de": "German", "fr": "French", "es": "Spanish",
         "pl": "Polish", "pt": "Portuguese", "ja": "Japanese", "zh": "Chinese",
         "ko": "Korean"}
GAME_LANGS = {"l_russian": "ru", "l_english": "en", "l_german": "de", "l_french": "fr",
              "l_spanish": "es", "l_polish": "pl", "l_braz_por": "pt", "l_japanese": "ja",
              "l_simp_chinese": "zh", "l_korean": "ko"}


def game_language():
    """The game's language from pdx_settings.txt ("en" when it cannot be read)."""
    path = os.path.expanduser("~/.local/share/Paradox Interactive/Hearts of Iron IV/"
                              "pdx_settings.txt")
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            text = stream.read()
    except OSError:
        return "en"
    i = text.find('"language"')
    if i < 0:
        return "en"
    for key, lang in GAME_LANGS.items():
        if key in text[i:i + 120]:
            return lang
    return "en"


class EmptyReplyError(RuntimeError):
    pass


class Chat:
    def __init__(self, path, fresh=False):
        self.path = path
        self.lock = threading.Lock()
        self.busy = {}                 # agent -> (what it is doing, since when)
        self.data = {}
        if not fresh:
            try:
                self.data = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {}
        self.save()

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    def _of(self, key):
        return self.data.setdefault(key, {"log": [], "directives": []})

    def directives(self, key):
        """In English, for the agents."""
        return list(self.data.get(key, {}).get("directives", []))

    def shown(self, key):
        """The same in the player's language when the agent gave them, else in English."""
        d = self.data.get(key, {})
        s = d.get("shown") or []
        return list(s) if len(s) == len(d.get("directives", [])) else self.directives(key)

    def log(self, key):
        return list(self.data.get(key, {}).get("log", []))

    def _add(self, key, who, text, date):
        d = self._of(key)
        d["log"].append([who, text, date])
        del d["log"][:-KEEP]

    # ------------------------------------------------------------------ the calls
    def _prompt(self, agent, lang, task):
        return ("You are %s in a game of Hearts of Iron IV, talking directly with the player. "
                "The player plays this country: the player stands above Le Chonk (the supreme "
                "commander) and above the plan. You cannot act in this conversation; what you "
                "do happens on your turns. Write to the player in %s, plain text, no markdown.\n"
                "%s\n\nYOUR ROLE (from your standing instructions):\n%s"
                % (agent.name, LANGS.get(lang, "English"), task, agent.system[:3000]))

    def _context(self, key, picture, summaries):
        dirs = self.directives(key)
        talk = "\n".join("%s %s: %s" % (date, "PLAYER" if who == "you" else "YOU", text)
                         for who, text, date in self.log(key)[-12:]) or "(nothing yet)"
        return ("THE PICTURE OF YOUR SPHERE NOW:\n%s\n\nTHE PLAYER'S STANDING DIRECTIVES TO "
                "YOU:\n%s\n\nYOUR LAST TURNS:\n%s\n\nYOUR CONVERSATION WITH THE PLAYER SO FAR:\n%s"
                % ((picture or "(not available)")[:14000],
                   "\n".join("- " + x for x in dirs) or "(none)",
                   "\n".join("- " + x for x in summaries[-TURNS:]) or "(none yet)", talk))

    def _call(self, agent, msgs, tool):
        msg, usage, dt = agent.llm.chat(agent.model, msgs, tools=[tool], tool_choice="any",
                                        max_tokens=3000, role=agent.name + " (chat)",
                                        reasoning="none" if agent.reasoning else None,
                                        budget_s=120)
        for c in msg.get("tool_calls") or []:
            try:
                return json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                pass
        # no tool call: take the text as the answer
        return {"answer": split_content(msg.get("content"))[1] or "",
                "report": split_content(msg.get("content"))[1] or ""}

    def begin(self, key, what, text=None, date=None):
        """At the click: the player's words into the chat at once, and the agent busy (the
        answer takes a model call; the panel shows a spinner meanwhile)."""
        with self.lock:
            if key in self.busy:
                return False
            if text:
                self._add(key, "you", text, date or "?")
            self.busy[key] = (what, time.time())
        try:
            self.save()
        except Exception:
            self.end(key)
            raise
        return True

    def end(self, key):
        with self.lock:
            self.busy.pop(key, None)

    def record_failure(self, key, kind, error, date):
        with self.lock:
            self._add(key, 'report' if kind == 'writing a report' else 'agent',
                      '(the call failed: %s)' % error, date)
        self.save()

    def activity(self):
        with self.lock:
            return dict(self.busy)

    def ask(self, key, agent, text, picture, summaries, date, lang, on_change):
        """The player's message (already in the chat, begin()): the agent's answer into the
        chat; on_change(old, new) when the directives changed."""
        error = None
        try:
            msgs = [{"role": "system", "content": self._prompt(
                        agent, lang, "Answer the player's message, and keep the list of the "
                                     "player's standing directives to you: add a directive the "
                                     "player gives, change or drop one the player changes or "
                                     "cancels, keep the rest as they are.")},
                    {"role": "user", "content": self._context(key, picture, summaries) +
                     "\n\nTHE PLAYER WRITES NOW:\n" + text}]
            out = self._call(agent, msgs, REPLY)
            answer = str(out.get("answer") or "").strip()
            if not answer:
                raise EmptyReplyError()
            old = self.directives(key)
            new = [str(x).strip() for x in out.get("directives") or [] if str(x).strip()]
            if "directives" not in out:
                new = old
            self._of(key)["directives"] = new[:12]
            shown = [str(x).strip() for x in out.get("shown") or [] if str(x).strip()]
            self._of(key)["shown"] = shown[:12] if len(shown) == len(new) else []
            self._add(key, "agent", answer, date)
            if new != old:
                on_change(old, new)
        except Exception as e:
            error = type(e).__name__
            self._add(key, "agent", "(the call failed: %s)" % type(e).__name__, date)
        finally:
            self.save()
        return error

    def report(self, key, agent, picture, summaries, date, lang):
        error = None
        try:
            msgs = [{"role": "system", "content": self._prompt(
                        agent, lang, "Write the player a report on your sphere: where things "
                                     "stand, what you did lately, what you plan next, what worries "
                                     "you, what you need from the player. Up to 120 words, "
                                     "concrete numbers where they matter.")},
                    {"role": "user", "content": self._context(key, picture, summaries)}]
            out = self._call(agent, msgs, REPORT)
            report = str(out.get("report") or "").strip()
            if not report:
                raise EmptyReplyError()
            self._add(key, "report", report, date)
        except Exception as e:
            error = type(e).__name__
            self._add(key, "report", "(the call failed: %s)" % type(e).__name__, date)
        finally:
            self.save()
        return error


def wrap(text, width):
    """Lines of at most width characters, broken at spaces."""
    out = []
    for para in (text or "").split("\n"):
        line = ""
        for word in para.split():
            while len(word) > width:
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:width])
                word = word[width:]
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line += " " + word
            else:
                out.append(line)
                line = word
        if line:
            out.append(line)
    return out


def now():
    return time.strftime("%H:%M")
