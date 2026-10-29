"""Pull the Hearts of Iron IV wiki (hoi4.paradoxwikis.com) into knowledge/wiki/ as plain
text, for the planner's free local lookup before any paid web search (the user, 20.10).

usage: tools/wiki_pull.py            refresh: pages changed since the last pull, or all of
                                     them when the game version moved
       tools/wiki_pull.py --all      pull every page again
       tools/wiki_pull.py --source api|dump|auto   (default auto: the API, else the dump)

Two sources, both polite and neither bypassing anything:
  * the wiki's MediaWiki API, with a descriptive User-Agent, maxlag and robots.txt's
    Crawl-delay (15 s) between requests. On 20.10.2026 every request to the wiki (api.php,
    action=raw, the pages) got a JavaScript "Client Challenge" (bot protection); the script
    then stops asking and does not try to get past it;
  * the newest WikiTeam dump of the wiki on archive.org (wiki-hoi4.paradoxwikis.com-*, made
    with the wiki's own API and published there), one download of ~7 MB. A refresh from the
    dump rewrites only the pages whose revision changed.

Each page goes to one file: the title, its revision and date, the game version the page
says it is verified for ({{Version}}; sections carry their own {{SVersion}}), and the text.
The wiki is community-edited and lags patches: the planner flags what is older than the
running game. Values the wiki reads from the game's defines ({{rd|NProduction|...}}) are
read here from the running game's own 00_defines.lua, so those numbers are current.
Text: CC BY-SA, https://central.paradoxwikis.com/Central:Copyrights
"""
import argparse
import ast
import datetime
import glob
import json
import math
import operator
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "knowledge", "wiki")
STATE = os.path.join(OUT, "_state.json")
ALIASES = os.path.join(OUT, "_aliases.json")
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "le-chonk", "wiki")
WIKI = "https://hoi4.paradoxwikis.com"
API = WIKI + "/api.php"
UA = ("LeChonkWikiCache/1.0 (local text cache for a HoI4 AI-player; one pull per game "
      "patch; strelok4855@gmail.com)")
IA_ID = "wiki-hoi4.paradoxwikis.com-"
DELAY = 15          # robots.txt: Crawl-delay: 15
NS = "{http://www.mediawiki.org/xml/export-0.11/}"

# The mechanics the planner decides on, then the great powers: country, focus tree,
# decisions and events.
PAGES = [
    "Production", "Construction", "Research", "Industry technology", "Engineering technology",
    "Land doctrine", "Air doctrine", "Naval doctrine", "Officer corps",
    "National focus", "Continuous focus", "Division designer", "Tank designer",
    "Ship designer", "Aircraft designer", "Equipment", "Variant",
    "Military industrial organization", "Infantry technology", "Support companies technology",
    "Artillery technology", "Armor technology", "Air technology", "Naval technology",
    "Naval support technology", "Air warfare", "Air combat", "Air missions", "Aircraft",
    "Naval warfare", "Naval missions", "Naval battle", "Navy", "Ship",
    "Land warfare", "Land battle", "Combat tactics", "Battle plan", "Division", "Land units",
    "Logistics", "Fuel", "Terrain", "Weather", "Organization", "Experience", "Commander",
    "Attrition and accidents", "Manpower", "Recruitable population", "Trade",
    "International market", "Government", "Ideas", "National spirit", "Ideology",
    "Diplomacy", "Faction", "World tension", "War goal", "Decisions", "Occupation",
    "Peace conference", "Puppet", "Intelligence agency", "State", "Special project",
    "Mechanics", "Beginner's guide", "Jargon",
    "France", "French national focus tree", "French events", "French events LaR",
    "Free France", "Vichy France",
    "German Reich", "German national focus tree",
    "German national focus tree: industrial/ historical branches",
    "German national focus tree: opposition branches", "German decisions", "German events",
    "United Kingdom", "British national focus tree", "British events",
    "Soviet Union", "Soviet national focus tree", "Soviet national focus tree: industrial branches",
    "Soviet national focus tree: political branches", "Soviet decisions", "Soviet events",
    "Italy", "Italian national focus tree", "Italian decisions", "Italian events",
    "Japanese Empire", "Japanese national focus tree", "Japanese events",
    "United States", "American national focus tree", "American events",
    "Patch 1.16", "Patch 1.17",
]

DLC = {"lar": "La Résistance", "aat": "Arms Against Tyranny", "mtg": "Man the Guns",
       "nsb": "No Step Back", "bba": "By Blood Alone", "wtt": "Waking the Tiger",
       "dod": "Death or Dishonor", "tfv": "Together for Victory", "bftb":
       "Battle for the Bosporus", "gd": "Götterdämmerung", "toa": "Trial of Allegiance"}
SKIP_KEYS = {"image", "file", "link", "width", "size", "style", "class", "id", "ref", "icon",
             "indent", "align", "alignment", "container", "nolink", "float", "px", "color"}


def say(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ the game
def game_version():
    from harness import planner
    return planner.game_version()


_DEFINES = {}


def define(group, key):
    """A value from the running game's 00_defines.lua, or None."""
    if not _DEFINES:
        from harness import bridge
        try:
            txt = open(os.path.join(bridge.game_dir(), "common", "defines", "00_defines.lua"),
                       encoding="utf-8-sig", errors="replace").read()
        except OSError:
            txt = ""
        group_now = None
        for line in txt.splitlines():
            line = line.split("--", 1)[0]
            m = re.match(r"\s*(N\w+)\s*=\s*\{", line)
            if m:
                group_now = m.group(1)
                continue
            m = re.match(r"\s*(\w+)\s*=\s*(-?[\d.]+)", line)
            if m and group_now:
                _DEFINES[(group_now, m.group(1))] = m.group(2)
        _DEFINES.setdefault(("", ""), "")
    v = _DEFINES.get((group, key))
    if v is None:
        return None
    f = float(v)
    return str(int(f)) if f == int(f) else ("%g" % f)


# ------------------------------------------------------------------ wikitext -> text
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.USub: operator.neg, ast.UAdd: operator.pos,
        ast.Pow: operator.pow, ast.Mod: operator.mod}


def _expr(s):
    """MediaWiki's {{#expr:}} on numbers only (+-*/ ^ mod round floor ceil), else None."""
    s = s.replace("^", "**").replace(" mod ", " % ")
    s = re.sub(r"(\S+)\s+round\s+(\d+)", r"round(\1,\2)", s)
    s = re.sub(r"\b(floor|ceil|abs)\b", r"\1", s)

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and \
                n.func.id in ("floor", "ceil", "abs", "round"):
            return {"floor": math.floor, "ceil": math.ceil, "abs": abs,
                    "round": round}[n.func.id](*[ev(a) for a in n.args])
        raise ValueError
    try:
        v = ev(ast.parse(s.strip(), mode="eval"))
    except Exception:
        return None
    return str(int(v)) if float(v) == int(v) else "%g" % v


def _split(body):
    """Template params, split on | outside [[ ]]."""
    out, depth, cur = [], 0, []
    i = 0
    while i < len(body):
        if body.startswith("[[", i):
            depth += 1
            cur.append("[[")
            i += 2
            continue
        if body.startswith("]]", i) and depth:
            depth -= 1
            cur.append("]]")
            i += 2
            continue
        c = body[i]
        if c == "|" and not depth:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def _template(body):
    parts = _split(body)
    name = parts[0].strip()
    low = name.lower()
    pos, named = [], []
    for p in parts[1:]:
        m = re.match(r"\s*([\w \-]+?)\s*=(.*)$", p, re.S)
        if m and not p.strip().startswith(("*", "[")):
            named.append((m.group(1).strip().lower(), m.group(2).strip()))
        else:
            pos.append(p.strip())
    if low.startswith("#expr:"):
        return _expr(name[6:] + "|".join(parts[1:])) or ""
    if low.startswith("#"):                 # #if, #switch...: the plainest branch
        return pos[0] if pos else ""
    if low in ("version",):
        return "\x01V:%s\x01" % (pos[0] if pos and pos[0] else "?")
    if low in ("sversion",):
        return "\x01S:%s\x01" % (pos[0] if pos and pos[0] else "?")
    if low == "rd":
        if len(pos) == 1 and "." in pos[0]:
            pos = pos[0].split(".", 1)
        v = define(pos[0], pos[1]) if len(pos) >= 2 else None
        if v is not None and ("percent", "true") in named:
            v = "%g%%" % (float(v) * 100)
        return v if v is not None else (pos[1] if len(pos) > 1 else "")
    if low in ("cite define", "-", "clear", "anchor", "buffer", "!", "end box wrapper",
               "toc", "tocright"):
        return "|" if low == "!" else ""
    if low in ("icon", "iconify", "iconbox-small", "flag", "state", "country", "trait",
               "expansion", "ug"):
        if not pos:
            return ""
        x = pos[0]
        if low in ("icon", "iconify", "expansion") and x.lower() in DLC:
            return " %s " % DLC[x.lower()]
        return " %s " % x if low in ("icon", "iconify") else x
    if low in ("focus", "event", "decision", "mio", "hover"):
        return pos[-1] if pos else ""
    if low in ("green", "red", "nowrap", "small", "bypass", "color", "plainlist", "big",
               "b", "i", "tooltip", "collapse"):
        return pos[-1] if pos else ""
    if low == "iconbox":            # focus name, then its flavour text
        return "FOCUS %s%s" % (pos[0], ": " + pos[1] if len(pos) > 1 else "") if pos else ""
    if low == "effectbox":          # a national spirit: name, flavour, modifiers
        head = "Spirit %s" % (pos[0] if pos else "?")
        mods = "\n".join(v for k, v in named if k in ("modifiers", "modifier", "effects",
                                                      "research_bonus", "equipment_bonus"))
        return "\n%s: %s\n%s\n" % (head, pos[1] if len(pos) > 1 else "", mods)
    if low == "option":
        return "\n• Option: " + " ".join(pos)
    if low == "main":
        return "(Main article: %s)" % ", ".join(pos)
    vals = [p for p in pos if p and not re.search(r"\.(png|jpg|gif|svg)$", p, re.I)]
    vals += [v for k, v in named if k not in SKIP_KEYS and re.search(r"[A-Za-z]", v)
             and not re.search(r"\.(png|jpg|gif|svg)$", v, re.I)]
    return " ".join(vals)


def _link(m):
    target, _, label = m.group(1).partition("|")
    t = target.strip()
    if re.match(r"(?i)(file|image|category|media)\s*:", t):
        return ""
    if re.match(r"^:?[a-z]{2}(-[a-z]+)?:", t):         # interwiki
        return ""
    if label:
        return label.split("|")[-1]
    return t.split("#")[-1] if t.startswith("#") else t


def _table(lines):
    """A wiki table as text: one block a row, 'Header: value' where headers are known."""
    head, rows, row, cell = [], [], None, None

    def close_cell():
        nonlocal cell
        if cell is not None and row is not None:
            row.append(cell.strip())
        cell = None

    def cells(line, sep):
        out = []
        for c in line[1:].split(sep * 2):
            if c.count("|") == 1 and "=" in c.split("|")[0] and "[" not in c.split("|")[0]:
                c = c.split("|", 1)[1]                # | style="..." | value
            out.append(c)
        return out
    for line in lines[1:]:
        s = line.strip()
        if s.startswith("|}"):
            break
        if s.startswith("|+"):
            continue
        if s.startswith("|-"):
            close_cell()
            if row:
                rows.append(row)
            row = []
            continue
        if s.startswith("!"):
            close_cell()
            for c in cells(s, "!" if "!!" in s else "|"):
                head.append(re.sub(r"<[^>]+>", "", c).strip())
            continue
        if s.startswith("|"):
            close_cell()
            if row is None:
                row = []
            cs = cells(s, "|")
            for c in cs[:-1]:
                row.append(c.strip())
            cell = cs[-1]
            continue
        if cell is not None:
            cell += "\n" + line
    close_cell()
    if row:
        rows.append(row)
    out = []
    for r in rows:
        r = [c for c in r]
        if head and len(head) == len(r):
            parts = []
            for h, c in zip(head, r):
                if not c:
                    continue
                parts.append("%s: %s" % (h, c) if h and "\n" not in c and len(c) < 80
                             else ("%s:\n%s" % (h, c) if h else c))
            out.append("\n".join(parts))
        else:
            out.append("\n".join(c for c in r if c))
    return "\n\n".join(out)


def to_text(wt):
    """Wikitext -> (plain text, page version)."""
    t = re.sub(r"<!--.*?-->", "", wt, flags=re.S)
    t = re.sub(r"<ref[^>/]*/>", "", t)
    t = re.sub(r"<ref[^>]*>.*?</ref>", "", t, flags=re.S)
    t = re.sub(r"<(gallery|imagemap|references|noinclude|math|score|timeline)[^>]*>.*?</\1>",
               "", t, flags=re.S | re.I)
    t = re.sub(r"\{\{#ev:.*?\}\}", "", t, flags=re.S)
    t = re.sub(r"__\w+__", "", t)
    for _ in range(3):
        t = re.sub(r"\[\[([^\[\]]*)\]\]", _link, t)
    for _ in range(30):
        n = re.sub(r"\{\{([^{}]*)\}\}", lambda m: _template(m.group(1)), t)
        if n == t:
            break
        t = n
    t = re.sub(r"\[\[([^\[\]]*)\]\]", _link, t)
    t = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", t)
    t = re.sub(r"\[https?://\S+\]", "", t)
    # tables, innermost first
    for _ in range(5):
        lines = t.split("\n")
        start = None
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith("{|"):
                start = i
            elif s.startswith("|}") and start is not None:
                lines[start:i + 1] = ["", _table(lines[start:i + 1]), ""]
                break
        else:
            break
        t = "\n".join(lines)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</?[a-zA-Z][^>]*>", "", t)
    t = t.replace("'''", "").replace("''", "")
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&minus;", "-")
    page_v = None
    out = []
    for line in t.split("\n"):
        m = re.match(r"^(={2,6})\s*(.*?)\s*\1\s*$", line)
        if m:
            out.append("%s %s" % ("#" * (len(m.group(1)) - 1), m.group(2).strip()))
            continue
        vs = re.findall(r"\x01V:([^\x01]*)\x01", line)
        if vs and page_v is None:
            page_v = vs[0]
        ss = re.findall(r"\x01S:([^\x01]*)\x01", line)
        line = re.sub(r"\x01[VS]:[^\x01]*\x01", "", line)
        if ss:          # a section's own version: on its heading
            for j in range(len(out) - 1, -1, -1):
                if out[j].startswith("#"):
                    if "[verified for" not in out[j]:
                        out[j] += " [verified for %s]" % ss[0]
                    break
                if out[j].strip():
                    break
        line = re.sub(r"^\s*[*#:;]+\s*", lambda m: "  " * (len(m.group(0).strip()) - 1)
                      + "- ", line)
        line = re.sub(r"[ \t]+", " ", line).rstrip()
        out.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return text, page_v or "?"


# ------------------------------------------------------------------ writing
def fname(title):
    return re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_") + ".txt"


def write_page(title, wt, rev, ts, source):
    text, ver = to_text(wt)
    url = WIKI + "/" + title.replace(" ", "_")
    head = ["TITLE: %s" % title, "URL: %s" % url,
            "REVISION: %s (rev %s)" % (ts, rev), "VERIFIED FOR: %s" % ver,
            "SOURCE: %s" % source, "LICENSE: CC BY-SA, "
            "https://central.paradoxwikis.com/Central:Copyrights", ""]
    with open(os.path.join(OUT, fname(title)), "w", encoding="utf-8") as f:
        f.write("\n".join(head) + "\n" + text + "\n")
    return {"rev": rev, "ts": ts, "version": ver, "file": fname(title)}


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_json(path, obj):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(path + ".tmp", path)


# ------------------------------------------------------------------ source: the API
class Challenged(Exception):
    pass


class Api:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.last = 0.0
        self.allowed()

    def allowed(self):
        """robots.txt: refuse paths it disallows; take its Crawl-delay."""
        global DELAY
        r = self.s.get(WIKI + "/robots.txt", timeout=30)
        agent = None
        for line in r.text.splitlines():
            k, _, v = line.partition(":")
            k, v = k.strip().lower(), v.strip()
            if k == "user-agent":
                agent = v
            elif agent == "*" and k == "crawl-delay":
                DELAY = max(DELAY, float(v))
            elif agent == "*" and k == "disallow" and v and "/api.php".startswith(v):
                raise Challenged("robots.txt disallows %s" % v)

    def get(self, **params):
        wait = self.last + DELAY - time.time()
        if wait > 0:
            time.sleep(wait)
        params.update(format="json", formatversion=2, maxlag=5)
        r = self.s.get(API, params=params, timeout=60)
        self.last = time.time()
        if "json" not in r.headers.get("content-type", ""):
            raise Challenged("the wiki answered %s with %s (%s): a bot check; not bypassed"
                             % (r.status_code, r.headers.get("content-type"),
                                (re.search(r"<title>(.*?)</title>", r.text) or
                                 [None, "?"])[1]))
        d = r.json()
        if "error" in d:
            if d["error"].get("code") == "maxlag":
                time.sleep(max(DELAY, 30))
                return self.get(**params)
            raise RuntimeError(d["error"])
        return d

    def pages(self, titles):
        """title -> (wikitext, revid, timestamp), redirects followed (aliases kept)."""
        out, aliases = {}, {}
        for i in range(0, len(titles), 50):
            d = self.get(action="query", prop="revisions", rvprop="content|ids|timestamp",
                         rvslots="main", redirects=1, titles="|".join(titles[i:i + 50]))
            for r in d["query"].get("redirects", []):
                aliases[r["from"]] = r["to"] + ("#" + r["tofragment"]
                                                if r.get("tofragment") else "")
            for p in d["query"]["pages"]:
                if p.get("missing"):
                    continue
                rv = p["revisions"][0]
                out[p["title"]] = (rv["slots"]["main"]["content"], rv["revid"],
                                   rv["timestamp"])
            say("  api: %d/%d" % (min(i + 50, len(titles)), len(titles)))
        return out, aliases

    def changed_since(self, ts, titles):
        want, seen, cont = set(titles), set(), {}
        while True:
            d = self.get(action="query", list="recentchanges", rcend=ts, rcnamespace=0,
                         rcprop="title", rclimit=500, rctype="edit|new", **cont)
            seen |= {c["title"] for c in d["query"]["recentchanges"]} & want
            if "continue" not in d:
                return sorted(seen)
            cont = d["continue"]

    def all_redirects(self):
        out, cont = {}, {}
        while True:
            d = self.get(action="query", generator="allredirects", garnamespace=0,
                         garunique=1, garlimit=500, prop="info", redirects=1, **cont)
            for r in d["query"].get("redirects", []):
                out[r["from"]] = r["to"] + ("#" + r["tofragment"] if r.get("tofragment")
                                            else "")
            if "continue" not in d:
                return out
            cont = d["continue"]


# ------------------------------------------------------------------ source: the dump
def newest_dump():
    r = requests.get("https://archive.org/advancedsearch.php", timeout=60, params={
        "q": "identifier:%s*" % IA_ID, "fl[]": ["identifier", "publicdate"],
        "sort[]": "publicdate desc", "rows": 5, "output": "json"}, headers={"User-Agent": UA})
    docs = r.json()["response"]["docs"]
    if not docs:
        raise RuntimeError("no WikiTeam dump of the wiki on archive.org")
    return docs[0]["identifier"]


def dump_pages(ident):
    """All main-namespace pages of a dump: title -> (wikitext, revid, timestamp), and the
    redirects (title -> target#section). The dump is cached under ~/.cache."""
    meta = requests.get("https://archive.org/metadata/" + ident, timeout=60,
                        headers={"User-Agent": UA}).json()
    names = [f["name"] for f in meta["files"]
             if re.search(r"-(history|current)\.xml\.zst$", f["name"])]
    if not names:
        raise RuntimeError("%s has no xml dump" % ident)
    name = sorted(names, key=lambda n: "current" not in n)[0]
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, os.path.basename(name))
    if not os.path.exists(path):
        say("  downloading %s/%s" % (ident, name))
        with requests.get("https://archive.org/download/%s/%s" % (ident, name), stream=True,
                          timeout=120, headers={"User-Agent": UA}) as r:
            r.raise_for_status()
            with open(path + ".part", "wb") as f:
                for b in r.iter_content(1 << 20):
                    f.write(b)
        os.replace(path + ".part", path)
    pages, aliases = {}, {}
    proc = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
    for _, el in ET.iterparse(proc.stdout):
        if el.tag != NS + "page":
            continue
        if el.findtext(NS + "ns") == "0":
            title = el.findtext(NS + "title")
            best = max(el.findall(NS + "revision"),
                       key=lambda r: r.findtext(NS + "timestamp"))
            txt = best.findtext(NS + "text") or ""
            m = re.match(r"\s*#redirect\s*\[\[([^\]]+)\]\]", txt, re.I)
            if m:
                aliases[title] = m.group(1).replace("_", " ")
            else:
                pages[title] = (txt, best.findtext(NS + "id"), best.findtext(NS + "timestamp"))
        el.clear()
    proc.wait()
    return pages, aliases


# ------------------------------------------------------------------ the pull
def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pull(source, everything):
    os.makedirs(OUT, exist_ok=True)
    state = load_state()
    ver = game_version()
    titles = sorted(set(PAGES) | set(state.get("pages", {})))
    full = everything or not state or state.get("game_version") != ver
    if not full:
        say("refresh (game %s; last pull %s from %s)" % (ver, state.get("pulled"),
                                                       state.get("source")))
    else:
        say("full pull (game %s%s)" % (ver, "" if not state else
                                       "; was %s" % state.get("game_version")))
    got, aliases, src = None, None, None
    if source in ("api", "auto"):
        try:
            api = Api()
            want = titles if full else api.changed_since(state["as_of"], titles)
            got, aliases = api.pages(want) if want else ({}, {})
            if full:
                aliases.update(api.all_redirects())
            src = "MediaWiki API %s" % API
            as_of = now()
        except (Challenged, requests.RequestException) as e:
            say("  the API: %s" % e)
            if source == "api":
                return 1
    if got is None:
        ident = newest_dump()
        if not full and state.get("source") == "WikiTeam dump %s (archive.org)" % ident:
            say("  no newer dump than %s; nothing to do" % ident)
            return 0
        say("  from the WikiTeam dump %s" % ident)
        allp, aliases = dump_pages(ident)
        missing = [t for t in titles if t not in allp]
        if missing:
            say("  not in the dump: %s" % ", ".join(missing))
        old = state.get("pages", {})
        got = {t: allp[t] for t in titles if t in allp
               and (full or str(old.get(t, {}).get("rev")) != str(allp[t][1]))}
        src = "WikiTeam dump %s (archive.org)" % ident
        d = ident[len(IA_ID):]
        as_of = "%s-%s-%sT00:00:00Z" % (d[:4], d[4:6], d[6:8])
    pages = {} if full else dict(state.get("pages", {}))
    for t, (wt, rev, ts) in sorted(got.items()):
        pages[t] = write_page(t, wt, rev, ts, src)
    if full:        # drop files of pages no longer pulled
        keep = {p["file"] for p in pages.values()}
        for f in glob.glob(os.path.join(OUT, "*.txt")):
            if os.path.basename(f) not in keep:
                os.remove(f)
    have = set(pages)
    if aliases:     # only the redirects that land on a page we keep
        save_json(ALIASES, {a: t for a, t in sorted(aliases.items())
                            if t.split("#")[0] in have})
    save_json(STATE, {"game_version": ver, "source": src, "pages": pages, "pulled": now(),
                      "as_of": as_of})
    say("%d pages written, %d in the store" % (len(got), len(pages)))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true", help="pull every page again")
    ap.add_argument("--source", choices=("auto", "api", "dump"), default="auto")
    a = ap.parse_args()
    return pull(a.source, a.all)


if __name__ == "__main__":
    sys.exit(main())
