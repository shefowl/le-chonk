"""Run our country (whichever the loaded save plays) under Le Chonk and its field marshals.

  python -m harness.run --days 60 --speed 2 --marshal-every 2 --supreme-every 30

In peace (nobody at war with us) the game runs at --peace-speed, every turn stops it,
field marshals act only on Le Chonk's orders and Le Chonk reviews every
--peace-supreme-every days; a finished focus or an event stops the game at once.

The game must be running with a save loaded. The supreme command plans once with
the game paused (the start of the campaign), then the game runs in real time (it pauses
only for a new fire: a new war, a lost state, the enemy inside our territory, an empty
sector) and:
  * every marshal gets a turn every --marshal-every game days, and at once on an alarm
    (losses, a new directive);
  * Le Chonk gets a turn every --supreme-every days, and at once when a marshal requests
    something.
Model calls run in parallel threads; the game itself is only touched through the
single-threaded gateway. Everything is journaled to logs/run-<time>.jsonl.
"""
import argparse
import collections
import json
import re
import os
import signal
import threading
import time

from . import agents, briefing, chat, modui, ops, roster
from . import politics as politics_mod
from . import front_threats, threats
from .bridge import module as B_module
from .agents import (AirStaff, Army, Chancellery, Industry, Journal, Marshal, Politics,
                     Supreme)
from .bridge import Gateway
from . import planner as planning
from .llm import LLM, ROOT, load_env
from .mapview import MapView
from .admiralty import briefing as admiralty_briefing

def pages_of(rows, width, budget, *, measure=len):
    """Wrap whole reports into visible lines, splitting oversized entries across pages."""
    from .ui_text import wrap_coloured
    if width < 1 or budget < 1:
        raise ValueError("page width and line budget must be positive")
    pages, cur, used = [], [], 0
    for row in rows:
        lines = wrap_coloured(row, width, measure)
        if len(lines) <= budget:
            if cur and used + len(lines) > budget:
                pages.append(cur)
                cur, used = [], 0
            cur.append("\n".join(lines))
            used += len(lines)
        else:
            if cur:
                pages.append(cur)
                cur, used = [], 0
            while len(lines) > budget:
                pages.append(["\n".join(lines[:budget])])
                lines = lines[budget:]
            cur, used = ["\n".join(lines)], len(lines)
    if cur:
        pages.append(cur)
    return pages or [[]]


def strip_date(text):
    """An agent's summary without the date it often starts with (the panel shows it)."""
    return re.sub(r"^\s*\d{4}\.\d{1,2}\.\d{1,2}(\.\d+)?\s*[:\-]?\s*", "", text or "")


def plan_cards(plan, lang, wars=()):
    """Complete sections split into the native plan's three-line, 520-byte cards."""
    from .ui_text import pixel_width, wrap_coloured
    labels = [("economy", "Economy", "Экономика"),
              ("research", "Research", "Исследования"),
              ("production", "Production", "Производство"),
              ("politics", "Politics", "Политика"),
              ("army", "Army", "Армия"), ("air", "Air force", "Авиация"),
              ("navy", "Navy", "Флот"), ("war_plan", "War plan", "План войн")]
    idea = str(plan.get("idea") or "")
    if len(idea.encode("utf-8")) > 800 or len(wrap_coloured(
            idea, 776, lambda t: pixel_width(t, lang))) > 4:
        labels.insert(0, ("idea", "Strategy", "Замысел"))
    sections = [(ru if lang == "ru" else en, str(plan[k]))
                for k, en, ru in labels if plan.get(k)]
    fuel = plan.get("fuel")
    if isinstance(fuel, dict):
        sections.append(("Топливо" if lang == "ru" else "Fuel",
                         "Reserve: %s days of war; estimated use: %s/day; source: %s. %s" %
                         (fuel.get("war_days", "?"), fuel.get("war_burn", "?"),
                          fuel.get("source", "?"), fuel.get("note", ""))))
    sections.extend(("Операция" if lang == "ru" else "Operation", text) for text in wars)
    cards = []
    # The byte bound also leaves room for a heading and non-ASCII line terminators.
    measure = lambda t: max(pixel_width(t, lang), len(t.encode("utf-8")) * 776 / 210)
    for title, text in sections:
        parts = pages_of([text], 776, 2, measure=measure)
        for i, part in enumerate(parts):
            cards.append("%s (%d/%d)\n%s" % (title, i + 1, len(parts), "\n".join(part)))
    return cards or ["Подробного плана пока нет" if lang == "ru" else "No detailed plan yet"]


# alarm targets -> the agent key of the panel's feed (mod/build.py STAFF)
ALARM_KEYS = {"supreme": "supreme", "staff": "army", "industry": "industry",
              "politics": "politics", "air": "air", "navy": "admiralty"}
# the feed's rank badges
BADGES_EN = {"planner": "Planner", "supreme": "Supreme", "events": "Events",
             "politics": "Politics", "industry": "Industry", "army": "Army staff",
             "air": "Air staff", "admiralty": "Admiralty", "marshals": "Marshal"}
BADGES_RU = {"planner": "План", "supreme": "Ставка", "events": "События",
             "politics": "Политика", "industry": "Промышленность", "army": "Армейский штаб",
             "air": "Штаб ВВС", "admiralty": "Адмиралтейство", "marshals": "Маршал"}


LETTERS = ("request from", "message from", "order from")
BACKGROUND_STAFF = {"industry", "politics", "army", "air", "admiralty"}


def coalesce_alarms(pending):
    """One wake per reader for queued letters; every inbox message remains intact.

    Emergencies and player commands keep separate wakes and bypass letter throttling.
    Drain only the current batch: producers can append while it is being processed.
    """
    out, letters = [], {}
    for _ in range(len(pending)):
        target, why = pending.popleft()
        if why.startswith(LETTERS) and "the player" not in why:
            if target in letters:
                i = letters[target]
                reasons = out[i][1].split("; ")
                if why not in reasons:
                    out[i] = (target, out[i][1] + "; " + why)
                continue
            letters[target] = len(out)
        out.append((target, why))
    return out



def day_of(d):
    return d["raw"] // 24



from .credentials import CHEAP, SUBSCRIPTION, LECHONK, resolve_profile, verify_start

class Runner:
    def __init__(self, args):
        from .ownership import acquire
        self._ownership_fd = acquire()
        os.set_inheritable(self._ownership_fd, False)
        load_env()
        if os.environ.get("CLAUSEWITZ_ANY_BUILD"):
            raise ValueError("Harness refuses the research build override; unset CLAUSEWITZ_ANY_BUILD")
        os.environ.setdefault("HOI4_FREEZE_LOG", os.path.join(ROOT, "logs", "freeze.log"))
        resolve_profile(args)
        self.args = args
        self.llm = LLM(args.provider)
        self.gw = Gateway()
        self.mv = MapView()
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        self.journal = Journal(os.path.join(ROOT, "logs", "run-%d.jsonl" % time.time()))
        info = self.gw.run(lambda g: B_module("session").info(g))
        verify_start(args, info)
        agents.COUNTRY.update(name=info.get("my_country") or info["my_tag"], tag=info["my_tag"])
        self.supreme = Supreme(args.supreme_model or os.environ["SUPREME_MODEL"], self.llm,
                               self.journal)
        # the grand strategy, months ahead (the user, 20.10): Le Chonk's own model
        self.planner = planning.Planner(args.supreme_model or os.environ["SUPREME_MODEL"],
                                        self.llm, self.journal)
        self.plan_asked = None         # the supreme's reason to call the planning early
        self.last_plan_day = None
        self.faction_seen = None
        self.chancellery = Chancellery(args.supreme_model or os.environ["SUPREME_MODEL"],
                                       self.llm, self.journal)
        # the economy runs while the generals fight (the user, 18.10: AI off, nobody had
        # researched or built anything since August 1939)
        self.industry = Industry(args.theater_model or os.environ["THEATER_MODEL"],
                                 self.llm, self.journal)
        self.last_industry = None
        self.why_seen = {}             # each staff's alarm reasons at its last wake
        self.politics = Politics(args.theater_model or os.environ["THEATER_MODEL"],
                                 self.llm, self.journal)
        self.last_politics = None
        self.politics_pp = None
        self.focus_checked = (None, False)   # (day, whether a focus could be started)
        self.army = Army(args.theater_model or os.environ["THEATER_MODEL"],
                         self.llm, self.journal)
        self.last_army = None
        self.air_staff = AirStaff(args.theater_model or os.environ["THEATER_MODEL"],
                                  self.llm, self.journal)
        self.last_air = None
        self.attacks_seen = set()      # theatres whose attack the air staff was woken for
        from .admiralty import Admiralty
        from . import navy
        self.admiralty = Admiralty(args.theater_model or os.environ["THEATER_MODEL"],
                                   self.llm, self.journal)
        self.last_navy = None
        self.sea = navy.SeaRegions(self.mv)
        self.commanders = {}           # fleet name -> its FleetCommander (harness/fleets.py)
        self.fleet_memory = {}         # their notes, kept while a fleet has no zone
        self.navy_lock = threading.Lock()   # the Admiralty and its commanders, one round
        self.zone_woken = {}           # fleet -> day its commander was last woken alone
        self.air_owner = {}            # wing id -> the staff that last ordered it
        # what stood when the war began: it does not drill at war (the user, 24.10)
        self.veterans = {"tf": set(), "wings": set(), "taken": False}
        self.drill_day, self.drill_told = -99, {}
        self.last_deploy_day = None
        self.events_seen = set()
        self.marshals = {}
        self.parked = {}               # theatres the player took back (roster.json)
        self.roster_key = None         # (file time, day) the roster was last applied at
        self.roster_seen = None
        self.handed_told = set()       # tagged theatres with no marshal, told of
        self.ui = modui.ModUI(self.gw, lambda m: print(m, flush=True))
        self.ui.on_message = self.player_chat
        self.ui.on_report = self.player_report
        self.ui.settings_fn = self.settings_now
        self.last_said = {}            # role -> its last turn's date and summary (the panel)
        # the panel's feed: (date, agent key of mod/build.py STAFF, text, important)
        self.feed = collections.deque(maxlen=400)
        self.chat = chat.Chat(os.path.join(ROOT, "logs", "chat.json"), fresh=not args.resume)
        self.ui.activity_fn = self.chat.activity
        self.status_at = self.plan_text_at = 0.0
        self.last_date = "?"
        self.war_approved = set()      # wars the player said yes to (ask_war)
        self.alarms = collections.deque()
        self.threads = []
        self.stopping = False
        self._holds, self._hold_lock = 0, threading.Lock()
        self.at_war = set()            # who is at war with us, as of the last loop pass
        self.war_began = {}            # enemy tag -> game day its war with us began
        self.last_request_wake = -99   # day a marshal's request last woke the supreme
        # 27.10, the user: hours of wall time for two months of war. One pause on
        # 1940.10.02 took 267 turns in 32 minutes: Montgomery asked, Le Chonk answered,
        # each answer woke the other. A letter wakes its reader once a game day; the
        # rest wait in the inbox and wake him the next day.
        self.letter_woken = {}         # target -> day a letter last woke it
        self.letter_waiting = {}       # target -> why, for the next day
        self.tension = set()           # (justifier, target) war goals that wake the marshals
        self.tension_told = False      # world tension past 25% was reported
        self.guarantee_wars = set()    # (guaranteed, attacker) wars Le Chonk was told of
        self.last_tension_day = None
        self.last_guarantee_day = None
        self.requests_seen = set()     # incoming request ids Le Chonk was woken for
        self.forts_day = None          # the game day the map's forts were last read
        self.fires_seen = {}           # fire -> the last day it burned
        self.attacking = {}            # group id -> its armies without an attack
        self.digest = {}               # group id -> its alerts, numbers blanked
        self._land_war = None          # (game day, a land front of ours that day)
        self.war_wanted = set()        # wars Le Chonk tried to open before deployment
        self.ready_since = {}          # enemy tag -> first day its army groups were ready
        self.ready_told = set()        # enemies the supreme was woken for as ready
        self.roles = {id(self.supreme): "supreme", id(self.planner): "planner",
                      id(self.chancellery): "events", id(self.industry): "industry",
                      id(self.politics): "politics", id(self.army): "army",
                      id(self.air_staff): "air", id(self.admiralty): "admiralty"}
        self.own = self.gw.run(lambda g: g.player_tag())
        self.state_path = os.path.join(ROOT, "logs", "campaign.json")
        self._save_lock = threading.Lock()
        if args.resume:
            self.load()

    def check_clock(self, clock):
        """The date must move while the game runs. Stuck for 20 s unpaused: find the date
        anew; a jump of more than a day then means the save was loaded under us, and the
        harness stops (its pointers are the old game's)."""
        raw = self.gw.run(lambda g: g.date_raw())
        if raw != clock[0]:
            clock[0], clock[1] = raw, time.time()
            return
        if time.time() - clock[1] < 20:
            return
        if self.gw.run(lambda g: B_module("session").is_paused(g)):
            clock[1] = time.time()
            return
        with self._hold_lock:
            def live(g):
                Gateway._calibrate_clock(g)
                return g.date_raw()
            new = self.gw.run(live)
            clock[0], clock[1] = new, time.time()
            # A reload jumps back, or far ahead. Leave it paused on an invalid jump.
            if new < raw or new - raw > 30 * 24:
                print("THE GAME WAS RELOADED (date %d -> %d): stop and start the harness again"
                      % (raw, new), flush=True)
                raise RuntimeError("game reloaded under the harness")
            if not self._holds and not self.stopping:
                self.run_clock()
        print("date address renewed", flush=True)

    def _sane_clock(self, saved):
        """The date read at a restart must lie near the one saved at the stop; the cached
        address can hold something else by then."""
        if not saved:
            return
        ok = lambda r: saved - 48 <= r <= saved + 24 * 120
        raw = self.gw.run(lambda g: g.date_raw())
        if ok(raw):
            return
        first = raw
        raw = self.gw.run(lambda g: g.p.i32(g.find_date_address(force=True)))
        if not ok(raw):
            # 23.10: after a reload both reads found the old save's frozen clock (the
            # game.log date was the old one too); the clock that moves is the live one
            def live(g):
                Gateway._calibrate_clock(g)
                return g.date_raw()
            try:
                raw = self.gw.run(live)
            except RuntimeError as e:
                print("live date search failed: %s" % e, flush=True)
            print("date reads at start: cached %d, scan %d, live %d; saved %d"
                  % (first, self.gw.run(lambda g: g.p.i32(g._date_addr)), raw, saved),
                  flush=True)
        if not ok(raw) and getattr(self.args, "rewound", False) and 0 < raw < saved:
            # the player loaded an older save of this campaign on purpose (27.10: the
            # game crashed at 1940.10.28 and only the 10.01 autosave was left)
            print("REWOUND: the game is at %d, the campaign was saved at %d; it goes on "
                  "from the game's date" % (raw, saved), flush=True)
            return
        if not ok(raw):
            raise RuntimeError("the date read (%d) is far from the saved one (%d): the "
                               "game is not the saved campaign's, or the date address is "
                               "lost" % (raw, saved))
        print("date address renewed at start", flush=True)

    # ---------------------------------------------------------------- campaign state
    def save(self):
        """Theatres under our field marshals and the agents' notes, so a campaign can run
        in several sittings."""
        try:
            raw = self.gw.run(lambda g: g.date_raw())
        except Exception:
            raw = None
        rec = {"date_raw": raw, "supreme_memory": self.supreme.memory,
               "planner_memory": self.planner.memory, "plan": self.planner.plan,
               "last_plan_day": self.last_plan_day,
               "industry_memory": self.industry.memory,
               "politics_memory": self.politics.memory,
               "army_memory": self.army.memory,
               "air_memory": self.air_staff.memory,
               "navy_memory": self.admiralty.memory,
               "fuel_log": __import__("harness.economy", fromlist=["_FUEL_LOG"])._FUEL_LOG,
               "sea_zones": __import__("harness.fleets", fromlist=["ZONES"]).ZONES,
               "fleet_memory": dict(self.fleet_memory, **{n: c.memory for n, c in
                                                          self.commanders.items()}),
               "air_owner": {str(k): v for k, v in self.air_owner.items()},
               "veterans": {"tf": sorted(self.veterans["tf"]),
                            "wings": sorted(self.veterans["wings"]),
                            "taken": self.veterans["taken"]},
               "feed": [list(e) for e in self.feed],
               "war_wanted": sorted(self.war_wanted), "ready_since": self.ready_since,
               "war_began": self.war_began,
               "guarantee_wars": sorted(map(list, self.guarantee_wars)),
               "operative_orders": __import__("harness.intel", fromlist=["ORDERS"]).ORDERS,
               "operation_orders": __import__("harness.intel", fromlist=["OP_ORDERS"]).OP_ORDERS,
               "staged": {str(k): v for k, v in ops.STAGE.items()},
               "invasions": {str(k): v for k, v in ops.INVASIONS.items()},
               "theatres": [{"theatre": m.theatre, "general_key": m.general_key,
                             "general_name": m.name, "memory": m.memory,
                             "last_divisions": m.last_divisions}
                            for m in list(self.marshals.values())
                            + list(self.parked.values())]}
        with self._save_lock:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)

    STALL_DAYS = 30

    def _stalemate(self, today, at_war, a):
        """A war whose front has not moved for STALL_DAYS: the supreme is woken with the
        plan's answer to it (the user, 23.10: a year before Beiping and nobody turned it)."""
        from . import warplan
        stand = self.__dict__.setdefault("stand", {})
        if stand.get("_day") == today:
            return
        stand["_day"] = today
        wars = [w for w in (self.planner.plan or {}).get("wars") or []
                if warplan.tags(w) & at_war] or [{"enemy": "+".join(sorted(at_war))}]
        holders = self.gw.run(self.holders)
        for w in wars:
            foe = warplan.tags(w) & at_war or set(at_war)
            n = sum(1 for _, (_, ctl) in holders.items() if ctl in foe)
            key = w["enemy"]
            last = stand.get(key)
            if last is None or n < last[1]:
                stand[key] = [today, n, None]
                continue
            stand[key][1] = n
            if today - last[0] >= self.STALL_DAYS and (last[2] is None
                                                       or today - last[2] >= self.STALL_DAYS):
                last[2] = today
                self.supreme_turn(
                    "the front against %s has not moved for %d days (no state taken). THE "
                    "WAR PLAN says, if the front stands: %s" % (
                        key, today - last[0], w.get("stalemate") or "nothing: ask the "
                        "planning session for a way round (ask_replan)"),
                    pause=a.pause != "never")

    def _war_part(self, agent, c):
        """The war plans as this agent reads them (warplan.render)."""
        from . import warplan
        wars = (self.planner.plan or {}).get("wars") or []
        if not wars:
            return ""
        out = []
        for w in wars:
            try:
                if getattr(agent, "theatre", None):
                    t = warplan.render(self.mv, w, "theatre",
                                       (agent.theatre["id"], agent.theatre["name"]), c)
                    shown = set(re.findall(r"PHASE (\d+) ", t))
                    lands = [x for x in warplan.landings(c, w) if x[0] == agent.theatre["id"]
                             and (not shown or any("(phase %s " % k in x[1] for k in shown))]
                    if lands:
                        t += "\n" + "\n".join(x[1] for x in lands) + (
                            "\nTHE LANDINGS ARE YOURS TO PLAN: with a landing army of your "
                            "theatre (off the line), naval_invasion from the port province on "
                            "the coast provinces named; it waits, and launch_operation sets it "
                            "off when the admiralty holds the sea. A landing army not in your "
                            "theatre: ask Le Chonk to send it (report_to_supreme).")
                elif agent.name == "Le Chonk":
                    t = warplan.render(self.mv, w, ctx=c) + "\nREADINESS NOW: " + \
                        warplan.readiness_line(c, w)
                    lands = warplan.landings(c, w)
                    if lands:
                        t += "\n" + "\n".join(
                            x[1] + " -> the main-effort theatre: %s" % (x[0] if x[0] is not None
                                                                     else "none opened")
                            for x in lands) + ("\nA landing army goes to the theatre that "
                                              "lands it (move_to_theatre); its field marshal "
                                              "plans the invasion; the admiralty holds the sea "
                                              "and the convoys.")
                elif agent.name == "Admiralty":
                    t = warplan.render(self.mv, w, "navy")
                elif agent.name == "Air staff":
                    t = warplan.render(self.mv, w, "air")
                elif agent.name in ("Army staff", "Industry"):
                    t = warplan.render(self.mv, w, "army")
                else:
                    t = ""
            except Exception as e:
                t = "WAR PLAN AGAINST %s: could not be read (%s)" % (w.get("enemy"), e)
            if t:
                out.append(t)
        return "\n".join(out)

    def load(self):
        try:
            rec = json.load(open(self.state_path, encoding="utf-8"))
        except FileNotFoundError:
            print("no saved campaign at %s; starting fresh" % self.state_path, flush=True)
            return
        alive = set(self.gw.run(ops.theatres))
        self._sane_clock(rec.get("date_raw"))
        self.supreme.memory = rec.get("supreme_memory", [])
        self.planner.memory = rec.get("planner_memory", [])
        self.planner.plan = rec.get("plan", {})
        self.last_plan_day = rec.get("last_plan_day")
        self.industry.memory = rec.get("industry_memory", [])
        self.politics.memory = rec.get("politics_memory", [])
        from . import intel as _intel
        _intel.ORDERS.update(rec.get("operative_orders") or {})
        _intel.OP_ORDERS.update(rec.get("operation_orders") or {})
        ops.STAGE.update({int(k): v for k, v in (rec.get("staged") or {}).items()})
        ops.INVASIONS.update({int(k): v for k, v in (rec.get("invasions") or {}).items()})
        self.army.memory = rec.get("army_memory", [])
        self.air_staff.memory = rec.get("air_memory", [])
        self.admiralty.memory = rec.get("navy_memory", [])
        from . import economy as _economy        # the fuel's history outlives a restart
        _economy._FUEL_LOG[:] = [tuple(x) for x in rec.get("fuel_log") or []]
        from . import fleets as _fleets
        _fleets.ZONES.update({k: [int(r) for r in v] for k, v in
                              (rec.get("sea_zones") or {}).items()})
        self.fleet_memory = dict(rec.get("fleet_memory") or {})
        self.sync_commanders()
        self.air_owner.update({int(k): v for k, v in rec.get("air_owner", {}).items()})
        vt = rec.get("veterans") or {}
        self.veterans = {"tf": set(vt.get("tf") or ()), "wings": set(vt.get("wings") or ()),
                         "taken": bool(vt.get("taken"))}
        self.feed.extend(tuple(e) for e in rec.get("feed") or [])
        # the planning days survive a restart (17.10: every restart set them back to 0)
        # a plan dated in the future came from a bad date read (23.10: a stale date address
        # read 1960.02.15 at a restart; the planner set its next review for 1960.08 and the
        # supreme could not call it, "set -8000 days ago")
        today = day_of(self.gw.run(ops.date))
        if self.last_plan_day is not None and self.last_plan_day > today:
            self.last_plan_day = None
        nr = planning.day_number(self.planner.plan.get("next_review"))
        if nr is not None and nr > today + 400:
            self.planner.plan["next_review"] = planning.day_text(today)
            self.planner.plan["set"] = "?"
        self.war_wanted = set(rec.get("war_wanted", []))
        self.war_began = dict(rec.get("war_began") or {})
        self.ready_since = dict(rec.get("ready_since", {}))
        self.guarantee_wars = {tuple(x) for x in rec.get("guarantee_wars", [])}
        for t in rec.get("theatres", []):
            th = t["theatre"]
            if th["id"] not in alive:
                self.supreme.inbox.append("The theatre %r (%d) no longer exists in the game; "
                                          "its states are in no theatre." % (th["name"], th["id"]))
                continue
            m = Marshal(th, t["general_key"], t["general_name"],
                        self.args.theater_model or os.environ["THEATER_MODEL"],
                        self.llm, self.journal)
            m.memory, m.last_divisions = t.get("memory", []), t.get("last_divisions")
            m.reasoning = self.args.marshal_reasoning
            self.marshals[th["id"]] = m
        self.journal.write(event="resumed", theatres=sorted(self.marshals))
        print("resumed %d theatres" % len(self.marshals), flush=True)

    # ---------------------------------------------------------------- the roster
    def allowed(self, agent):
        """Whether this agent plays now (roster.json; 23.10)."""
        if isinstance(agent, Marshal):
            return agent.theatre["id"] in self.marshals
        role = self.roles.get(id(agent))
        return role is None or roster.on(role)

    def panel_texts(self, full=False):
        """Every text of the in-game panel (mod/build.py MARKERS). full: the plan's wars too,
        which ask the game for their readiness."""
        d = roster.get()
        from . import ui_text
        lang = chat.game_language()
        ru = lang == "ru"
        def compact(key, text, width):
            return ui_text.ellipsize_coloured(text, width, lang,
                                             max_bytes=modui.BUILD.MARKERS[key])
        out = {}
        out["SENT"] = getattr(getattr(self, "ui", None), "shown", {}).get("SENT", "")
        try:
            date = self.gw.run(ops.date)["text"][:10]
            self.last_date = date
        except Exception:
            date = "?"
        war = sorted(list(self.at_war))
        head = "%s | %s%s" % (date,
            (("война: %s" if ru else "at war with %s") % ", ".join(war)) if war else
            ("мир" if ru else "at peace"),
            ((" | агенты думают" if ru else " | waiting for agents") if self._holds else ""))
        out["HEAD"] = compact("HEAD", head, 520)
        for k in roster.AGENTS:
            key = "ST_" + k.upper()
            status = (("управляешь ты" if ru else "you play it") if d[k] == "off" else
                      self.last_said.get(k, "агент включён, ходов ещё нет" if ru else "on, no turn yet"))
            out[key] = compact(key, status, 398)
        out["TH_MODE"] = ({"all": "Все театры под командованием фельдмаршалов",
                            "tagged": "Агентам переданы только театры с префиксом AI:",
                            "none": "Всеми театрами управляешь ты"} if ru else
                           {"all": "Every theatre is the marshals'",
                            "tagged": "Only theatres named AI: ... are the marshals'",
                            "none": "Every theatre is yours"})[d["theatres"]]
        out["FL_MODE"] = ({"all": "Все флоты под командованием адмиралтейства",
                            "tagged": "Агентам переданы только флоты с префиксом AI:",
                            "none": "Всеми флотами управляешь ты"} if ru else
                           {"all": "Every fleet is the admiralty's",
                            "tagged": "Only fleets named AI: ... are the admiralty's",
                            "none": "Every fleet is yours"})[d["fleets"]]
        for k, _, _, _, _ in roster.SETTINGS:
            out["SV_" + k.upper()] = str(self.setting(k))
        lw = self._land_war[1] if self._land_war else None
        out["SET_NOTE"] = (("Сейчас мир: ходы ставят игру на паузу, кроме режима --pause never." if self.peace()
                           else "Идут сухопутные бои: режим МИР / БОЙ ставит игру на паузу."
                           if lw else "Война без сухопутных боёв: режим МИР / БОЙ позволяет игре идти.") if ru else
                          ("Now: peace; turns pause even in ON ALARM mode (unless --pause never)" if self.peace()
                           else "Now: we fight on land, so PEACE, BATTLE turns stop the game"
                           if lw else "Now: a war with no battle on land, so PEACE, BATTLE "
                           "turns run with the game"))
        ms = list(self.marshals.values())
        for i in range(1, 9):
            if i <= len(ms):
                m = ms[i - 1]
                th = m.theatre
                key = "TH_%d" % i
                in_war = th.get("enemy") in self.at_war
                posture = (("исполнение включено" if ru else "execution on")
                           if th.get("executing") and in_war else
                           ("исполнение выключено" if ru else "execution off"))
                out[key] = compact(key, "%s | %s: %s | %s | %s" % (
                    th["name"], th.get("enemy") or "-",
                    ("война" if ru else "war") if in_war else ("мир" if ru else "peace"),
                    posture, m.name), 776)
            else:
                out["TH_%d" % i] = ""
        out.update(self.feed_texts())
        out.update(self.agent_texts())
        out.update(self.plan_texts(full))
        return out

    def plan_texts(self, full=False):
        from . import warplan
        plan = self.planner.plan
        lang = chat.game_language()
        if (full or getattr(self, "_card_plan", None) is not plan or
                getattr(self, "_plan_language", None) != lang or not hasattr(self, "_plan_reader")):
            wars, ctx = [], None
            for w in (plan or {}).get("wars", []):
                try:
                    ctx = ctx or self.ctx()
                    ready = warplan.readiness_line(ctx, w)
                except Exception as e:
                    ready = "readiness unknown (%s)" % type(e).__name__
                wars.append(warplan.render(self.mv, w) + "\nREADINESS SNAPSHOT %s: %s" % (
                    (ctx or {}).get("date", "unknown"), ready))
            self._plan_cards = plan_cards(plan or {}, lang, wars)
            from .ui_text import reader_pages
            labels = [('idea', 'Strategy', 'Замысел'), ('economy', 'Economy', 'Экономика'),
                      ('research', 'Research', 'Исследования'), ('production', 'Production', 'Производство'),
                      ('politics', 'Politics', 'Политика'), ('army', 'Army', 'Армия'),
                      ('air', 'Air force', 'Авиация'), ('navy', 'Navy', 'Флот'),
                      ('war_plan', 'War plan', 'План войн')]
            document = ['\x11H%s\x11!\n%s' % (ru if lang == 'ru' else en, plan[key])
                        for key, en, ru in labels if (plan or {}).get(key)]
            fuel = (plan or {}).get('fuel')
            if fuel:
                body = (('Запас: %s дней войны; расход: %s/день; источник: %s. %s' if lang == 'ru' else
                         'Reserve: %s days of war; use: %s/day; source: %s. %s') %
                        (fuel.get('war_days', '?'), fuel.get('war_burn', '?'),
                         fuel.get('source', '?'), fuel.get('note', ''))
                        if isinstance(fuel, dict) else str(fuel))
                document.append('\x11H%s\x11!\n%s' % ('Топливо' if lang == 'ru' else 'Fuel', body))
            document.extend('\x11H%s %d\x11!\n%s' % (
                'Операция' if lang == 'ru' else 'Operation', i, text) for i, text in enumerate(wars, 1))
            text = '\n\n'.join(document) or ('Плана пока нет' if lang == 'ru' else 'No plan yet')
            self._plan_reader = reader_pages(text, 756, modui.BUILD.MARKERS['PL_TEXT'], lang, max_lines=18)
            self._card_plan = plan
            self._plan_language = lang
        pages = len(self._plan_reader)
        self.ui.reader_bounds("plan", pages)
        page = min(self.ui.plan_page, pages - 1)
        if page != self.ui.plan_page:
            self.ui.set_page("lc_plan_page", page)
        plan = plan or {}
        review = ("План %s; обзор %s | Часть %d/%d" if lang == "ru" else
                  "Plan %s; review %s | Part %d/%d") % (plan.get("set", "?"),
                    plan.get("next_review", "?"), page + 1, pages)
        out = {"PL_TEXT": self._plan_reader[page], "PL_REVIEW": review, "PL_IDEA": plan.get("idea") or
               ("Плана пока нет" if lang == "ru" else "No plan yet")}
        for i in range(3):
            n = page * 3 + i
            out["PL_WAR_%d" % (i + 1)] = self._plan_cards[n] if n < len(self._plan_cards) else ""
        return out

    # ------------------------------------------------------------------ the feed
    def add_feed(self, key, text, important=False, date=None):
        if date is None:
            try:
                date = self.gw.run(ops.date)["text"]
            except Exception:
                date = "?"
        self.feed.append((str(date)[:10], key, " ".join(str(text).split()), important))
        self.ui.changed = True

    def feed_texts(self):
        """FEED: the reports the filter lets through, newest first, one per line, for the
        scrolled window of the Reports tab."""
        from . import ui_text
        lang = chat.game_language()
        names = BADGES_RU if lang == "ru" else BADGES_EN
        shown = [e for e in reversed(list(self.feed))
                 if e[1] not in self.ui.muted and (e[3] or not self.ui.important)]
        rows = []
        for date, key, text, imp in shown[:200]:
            stamp = date[8:10] + "." + date[5:7] if len(date) >= 10 else date
            rows.append("%s\x11%s%s\x11!  %s\n%s\n" % (
                "\x11R! \x11!" if imp else "", modui.BUILD.BADGE.get(key, "W"),
                names.get(key, key), stamp, text))
        if not rows:
            self.ui.reader_bounds("feed", 1)
            return {"FEED": "По этим фильтрам донесений нет." if lang == "ru" else
                            "No reports match these filters.",
                    "FD_PAGE": "0 донесений" if lang == "ru" else "0 reports"}
        pages = ui_text.reader_pages('\n'.join(rows), modui.BUILD.FEED_WIDTH,
                                     modui.BUILD.MARKERS['FEED'], lang)
        self.ui.reader_bounds("feed", len(pages))
        page = min(self.ui.feed_page, len(pages) - 1)
        if page != self.ui.feed_page:
            # the game's counter went past the last page: put it back, or NEWER would
            # take as many clicks to move (24.10: OLDER clicked to 6 on two pages)
            self.ui.set_page("lc_feed_page", page)
        return {"FEED": pages[page], "FD_PAGE": "" if len(pages) == 1 else (
            ("Часть %d / %d — РАНЬШЕ / ПОЗЖЕ" if lang == "ru" else
             "Part %d / %d — OLDER / NEWER") % (page + 1, len(pages)))}

    def agent_texts(self):
        """The agent window: status, directives, and CHAT: the chat with it (or its turn
        reports), newest first, for the scrolled window."""
        from . import ui_text
        key = self.ui.agent
        out = {"AG_ST": "", "CHAT": "", "SENT": self.ui.feedback.get(key, "")}
        n_dr = modui.BUILD.N_DIRECTIVES
        if key not in modui.BUILD.CHAT:
            out.update({"DR_%d" % i: "" for i in range(1, n_dr + 1)})
            out.update(DR_TEXT="", DR_PAGE="")
            return out
        lang = chat.game_language()
        ru = lang == "ru"
        d = roster.get()
        busy = self.chat.busy.get(key)
        what = secs = None
        if busy:
            what, since = busy
            secs = max(0, int(time.time() - since))
            what = {"answering": "отвечает", "writing a report": "пишет доклад"}.get(
                what, what) if ru else what
        last = self.last_said.get(key, "")
        out["AG_ST"] = "%s%s%s" % (
            ("играете вы" if ru else "you play it") if d.get(key) == "off" else
            ("играет агент" if ru else "the agent plays it"),
            (" | " + ("последний ход " if ru else "last turn ") + last[:10]) if last else "",
            (" | \x11Y%s, %d s\x11!" % (what, secs)) if busy else "")
        dirs = self.chat.shown(key)
        out.update(self.ui.directive_texts(dirs, lang))
        for i in range(1, n_dr + 1):
            if i == 1 and not dirs:
                out["DR_1"] = ("нет. Напишите агенту ниже: «строй только фабрики»" if ru else
                               "none. Write to the agent below: \"build only factories\"")
            elif i == n_dr and len(dirs) > n_dr:
                out["DR_%d" % i] = "%d. %s (+%d)" % (i, dirs[i - 1], len(dirs) - n_dr)
            else:
                out["DR_%d" % i] = "%d. %s" % (i, dirs[i - 1]) if i <= len(dirs) else ""
            marker = "DR_%d" % i
            out[marker] = ui_text.ellipsize_coloured(out.get(marker, ""), 776, lang,
                                                    max_bytes=modui.BUILD.MARKERS[marker])
        names = BADGES_RU if ru else BADGES_EN
        c = modui.BUILD.BADGE.get(key, "W")
        rows = []
        n = 60
        if self.ui.agent_log:
            for date, k, text, imp in [e for e in reversed(list(self.feed)) if e[1] == key][:n]:
                rows.append("\x11%s%s\x11! %s" % (c, date[5:], text))
        else:
            if busy:
                # the spinner: the answer comes whole, so the wait shows (24.10: "at
                # least a spinner, 'the agent is answering'")
                rows.append("\x11%s%s\x11! \x11Y%s%s %d s\x11!" % (
                    c, names.get(key, key), what, "." * (1 + secs % 3), secs))
            for who, text, date in reversed(self.chat.log(key)[-n:]):
                head = (("\x11HВы:\x11! " if ru else "\x11HYou:\x11! ") if who == "you" else
                        "\x11%s%s%s:\x11! " % (c, names.get(key, key),
                                               (" (доклад)" if ru else " (report)")
                                               if who == "report" else ""))
                rows.append(head + " ".join(text.split()))
        pages = ui_text.reader_pages('\n\n'.join(rows) or
                    ('Переписки пока нет.' if ru else 'No messages yet.'),
                    modui.BUILD.CHAT_WIDTH, modui.BUILD.MARKERS['CHAT'], lang)
        self.ui.reader_bounds("chat", len(pages))
        page = min(self.ui.chat_page, len(pages) - 1)
        if page != self.ui.chat_page:
            self.ui.set_page("lc_chat_page", page)
        if len(pages) > 1:
            out["AG_ST"] += (" | часть %d / %d" if ru else " | part %d / %d") % (
                page + 1, len(pages))
        out["CHAT"] = pages[page]
        return out

    def chat_agent(self, key):
        return {"planner": (self.planner, planning.briefing),
                "supreme": (self.supreme, briefing.supreme),
                "politics": (self.politics, briefing.politics),
                "industry": (self.industry, briefing.industry),
                "army": (self.army, briefing.army_staff),
                "air": (self.air_staff, briefing.air_staff),
                "admiralty": (self.admiralty, admiralty_briefing)}[key]

    def _chat_inputs(self, key):
        agent, make = self.chat_agent(key)
        try:
            c = self.ctx()
            date = c["date"][:10]
            picture = make(c)
        except Exception as e:
            date, picture = "?", "(the picture failed: %s)" % type(e).__name__
        summaries = [t for _, k, t, _ in list(self.feed) if k == key]
        return agent, picture, summaries, date

    def _reply_worker(self, key, kind, work):
        error = None
        try:
            error = work()
        except Exception as failure:
            error = type(failure).__name__
            try:
                self.chat.record_failure(key, kind, error, self.last_date)
            except Exception:
                pass  # The visible error still reaches the player if the disk failed.
        finally:
            # Admission via ModUI.requests takes the same lock. An older completion
            # cannot overwrite acceptance/error feedback for the next request.
            with self.ui.lock:
                self.chat.end(key)
                self.ui.finish_request(key, kind, error)

    def player_chat(self, key, text):
        """The player wrote in an agent's window: its answer comes in a thread of its own."""
        with self.ui.lock:
            if not self.chat.begin(key, "answering", text, self.last_date):
                return False

        def go():
            agent, picture, summaries, date = self._chat_inputs(key)

            def changed(old, new):
                gone = [x for x in old if x not in new]
                came = [x for x in new if x not in old]
                self.add_feed(key, "Directives changed: %s%s" % (
                    "new: " + "; ".join(came) if came else "",
                    (" | dropped: " if came else "dropped: ") + "; ".join(gone) if gone else ""),
                    important=True)
                # the agent acts on it now, not at its next scheduled turn
                self.player_message(key, "your standing directives changed. Now: %s" % (
                    "; ".join(new) or "none"))
            return self.chat.ask(key, agent, text, picture, summaries, date, chat.game_language(), changed)
        self.ui.changed = True
        try:
            threading.Thread(target=lambda: self._reply_worker(key, "answering", go), daemon=True).start()
        except Exception:
            self.chat.end(key)
            raise
        return True

    def player_report(self, key):
        with self.ui.lock:
            if not self.chat.begin(key, "writing a report"):
                return False

        def go():
            agent, picture, summaries, date = self._chat_inputs(key)
            return self.chat.report(key, agent, picture, summaries, date, chat.game_language())
        self.ui.changed = True
        try:
            threading.Thread(target=lambda: self._reply_worker(key, "writing a report", go), daemon=True).start()
        except Exception:
            self.chat.end(key)
            raise
        return True

    def player_message(self, to, text):
        """A message typed in the panel: an order from the player, above the plan."""
        msg = "THE PLAYER (above the plan and above Le Chonk): " + text
        if to == "marshals":
            for tid, m in self.marshals.items():
                m.inbox.append(msg)
                self.alarms.append((tid, "a message from the player"))
            return
        if to == "planner":
            self.planner.inbox.append(msg)
            self.plan_asked = "the player writes: " + text
            return
        agent, target = {"supreme": (self.supreme, "supreme"), "army": (self.army, "staff"),
                         "air": (self.air_staff, "air"), "admiralty": (self.admiralty, "navy"),
                         "industry": (self.industry, "industry"),
                         "politics": (self.politics, "politics")}[to]
        agent.inbox.append(msg)
        agent.memory.append("THE PLAYER ORDERED: " + text[:400])
        self.alarms.append((target, "a message from the player"))

    def ask_player_war(self, tag, why):
        """ask_war: Le Chonk's declaration waits for the player's yes (the panel's question)."""
        def yes():
            self.war_approved.add(tag)
            self.supreme.inbox.append("The player APPROVED the war on %s: declare it now." % tag)
            self.alarms.append(("supreme", "the player approved a war"))

        def no():
            self.supreme.inbox.append("The player REJECTED the war on %s for now." % tag)
            self.alarms.append(("supreme", "the player rejected a war"))
        return self.ui.ask("Le Chonk asks: war on %s?" % tag, why, yes, no)

    def sync_roster(self, today):
        """Apply roster.json: park the theatres the player took, bring back those handed
        over, and tell who plays what. Once a game day, and at once when the file changes."""
        from . import navy
        d = roster.get()
        key = (roster._state["mtime"], today)
        if key == self.roster_key:
            return
        self.roster_key = key
        navy.OURS = roster.fleet_ours
        names = self.gw.run(lambda g: {tid: ops.theatre_name(g, tg)
                                       for tid, tg in ops.theatres(g).items()})
        player = {tid for tid, n in names.items() if not roster.theatre_ours(n)}
        ops.PLAYER_THEATRES.clear()
        ops.PLAYER_THEATRES.update(player)
        for tid in [t for t in self.marshals if t in player]:
            m = self.parked[tid] = self.marshals.pop(tid)
            print("roster: theatre %s (%s) is the player's now; %s stands down"
                  % (tid, names.get(tid), m.name), flush=True)
            self.journal.write(event="roster", parked=tid, name=names.get(tid))
        for tid in [t for t in self.parked if t in names and t not in player]:
            m = self.marshals[tid] = self.parked.pop(tid)
            m.inbox.append("The player has handed your theatre back to you: take it up as "
                           "it stands now.")
            self.alarms.append((tid, "theatre handed back"))
            print("roster: theatre %s (%s) is %s's again" % (tid, names.get(tid), m.name),
                  flush=True)
            self.journal.write(event="roster", resumed=tid, name=names.get(tid))
        if d["theatres"] == "tagged":
            new = [t for t in names if t not in player and t not in self.marshals
                   and t not in self.handed_told]
            for tid in new:
                self.handed_told.add(tid)
                msg = ("The player hands you the theatre %r (game theatre %d) with its armies: "
                       "it has no field marshal of ours yet. Open a theatre for those armies "
                       "(define_theatre) with a field marshal." % (names[tid], tid))
                print("roster: " + msg, flush=True)
                if roster.on("supreme"):
                    self.supreme.inbox.append(msg)
                    self.alarms.append(("supreme", "a theatre handed over by the player"))
        if d != self.roster_seen:
            self.roster_seen = dict(d)
            print("roster: " + roster.describe(), flush=True)
            self.journal.write(event="roster", roster=d)

    # ---------------------------------------------------------------- context
    def new_theatre(self, th, key, name):
        """A theatre under a field marshal (a new one, or a new commander for it)."""
        m = Marshal(th, key, name, self.args.theater_model or os.environ["THEATER_MODEL"],
                    self.llm, self.journal)
        m.reasoning = self.args.marshal_reasoning
        self.marshals[th["id"]] = m
        self.alarms.append((th["id"], "theatre opened"))
        self.journal.write(event="theatre", theatre=th, commander=key)

    def ctx(self):
        d = self.gw.run(ops.date)
        ops.DEFENCE.clear()
        ops.DEFENCE.update({tid: {int(k): v["provinces"] for k, v in
                                  (m.theatre.get("defence") or {}).items()
                                  if v.get("kind", "line") == "line"}
                            for tid, m in self.marshals.items()})
        c = {"gw": self.gw, "mv": self.mv, "own": self.own, "date": d["text"],
                "day": day_of(d),
                "holders": self.gw.run(self.holders),
                "enemies": self.gw.run(lambda g: B_enemies(g)),
                "supreme": self.supreme, "marshals": self.marshals, "alarms": self.alarms,
                "staff": self.army, "air_owner": self.air_owner, "veterans": self.veterans,
                "industry": self.industry, "politics": self.politics,
                "guarantees": set(self.gw.run(
                    lambda g: B_module("diplomacy").guarantees(g)["gives"])),
                "air_staff": self.air_staff,
                "admiralty": self.admiralty, "sea": self.sea,
                "new_theatre": self.new_theatre, "war_wanted": self.war_wanted,
                "planning": self.planning,
                "planner": self.planner, "ask_for_plan": self.ask_for_plan,
                "ask_player_war": self.ask_player_war, "war_approved": self.war_approved,
                "neighbours_of_interest": self.args.watch,
                "coming_wars": self.gw.run(self._coming), "plan_days": self.args.plan_days,
                "war_began": dict(self.war_began)}
        # Public faction partners can join the expected war; their border must
        # remain visible before it starts (HUN was dropped from the GER line).
        focus = self.gw.run(front_threats.coalition, front_threats.expected(c))
        allies = set(self.gw.run(lambda g: B_module("combat").subject_tags(g)))
        c["expected_enemies"] = focus - allies - {self.own}
        c["war_threats"] = self.gw.run(threats.cached_snapshot, self.own)
        ops.LINE_KEEP = focus
        ops.LINE_SKIP = ({h[1] for h in c["holders"].values()} - focus - allies - {self.own}
                         if focus else set())
        return c

    @staticmethod
    def _coming(g):
        try:
            return politics_mod.coming_targets(g)
        except Exception:
            return set()

    # ---------------------------------------------------------------- turns
    def freshness_guard(self, agent, ctx, make_briefing, held):
        """Rebrief old background replies under a pause before executing any orders.

        A local hold belongs to _turn and is released in its finally block. Tactical
        turns keep their normal pauses; this catches slow staff or fleet responses.
        """
        def before_orders():
            now = self.gw.run(ops.date)
            enemies = self.gw.run(B_enemies)
            age = day_of(now) - ctx["day"]
            if 0 <= age < 3 and set(enemies) == set(ctx["enemies"]):
                return None
            if not held:
                self.hold()
                held.append(True)
            old_date = ctx["date"]
            fresh = self.ctx()
            text = make_briefing(fresh)
            plan = self.planner.plan_for(agent.name)
            wars = self._war_part(agent, fresh)
            for key in list(ctx):
                if key in ("_fronts", "_regions") or (
                        isinstance(key, tuple) and key and key[0] == "_facing"):
                    ctx.pop(key)
            ctx.update(fresh)
            self.journal.write(event="stale_reply", agent=agent.name,
                               old_date=old_date, date=ctx["date"])
            return "\n\n".join(p for p in (plan, wars, text) if p)
        return before_orders

    def _turn(self, agent, make_briefing, why):
        if not self.allowed(agent):
            return                      # the player plays this branch (roster.json)
        if not agent.busy.acquire(blocking=False):
            return
        started = time.monotonic()
        freshness_held = []
        try:
            c = self.ctx()
            text = make_briefing(c)
            plan = self.planner.plan_for(agent.name)
            wars = self._war_part(agent, c)
            if wars:
                plan = (plan + "\n" if plan else "") + wars
            if plan:
                wrong = politics_mod.wrong_focus_keys(plan, self.own)
                if wrong:
                    # a focus named wrong is waited for forever (23.10)
                    plan += "\nKEYS CORRECTED BY THE GAME'S FILES: " + "; ".join(
                        "%s is no focus; the game's focus is %s" % kv for kv in wrong)
                text = plan + "\n\n" + text
            d = roster.get()
            off = [k for k in roster.AGENTS if d[k] == "off"]
            mine = sorted(set(ops.PLAYER_THEATRES))
            if off or mine or d["fleets"] != "all":
                # what the player plays (roster.json): orders for it go to him
                text = ("THE PLAYER PLAYS: %s%s%s. Nothing of the player's is ours to "
                        "order; what we need from it we ask of the player.\n%s" % (
                            ", ".join({"events": "the events and diplomacy",
                                       "army": "the army staff", "air": "the air staff",
                                       "supreme": "the supreme command (Le Chonk)"}
                                      .get(k, "the " + k) for k in off) or "no branch",
                            "; theatres %s" % mine if mine else "",
                            "; fleets: %s" % ("only those named [AI] are ours"
                                              if d["fleets"] == "tagged" else "none are ours")
                            if d["fleets"] != "all" else "", text))
            key = self.roles.get(id(agent))
            dirs = self.chat.directives("supreme" if key == "events" else key) if key else []
            if dirs:
                # the user, 24.10: the player talks to an agent in its window and gives
                # standing directives; they stand above Le Chonk and the plan
                text = ("THE PLAYER'S STANDING DIRECTIVES TO YOU (the player plays this country "
                        "and stands above Le Chonk and the plan; they hold until the player "
                        "cancels them):\n%s\n\n%s" % ("\n".join("- " + d for d in dirs), text))
            if why not in ("scheduled", "scheduled review"):
                # 17.10: Bock was woken for a Polish break-in and wrote "continues
                # unchanged"; nothing told him why this turn came
                text = "YOU ARE CALLED NOW BECAUSE: %s\n%s" % (why, text)
            self.journal.write(agent=agent.name, date=c["date"], wake=why, briefing=text)
            try:
                maps = self.args.maps
                want = maps == "all" or (maps == "supreme" and agent is self.supreme)
                picture = agent.picture(c) if want else None
            except Exception as e:                  # a turn without a map, not no turn
                picture = None
                self.journal.write(agent=agent.name, map_error="%s: %s" % (type(e).__name__, e))
            t0 = time.time()
            if key in BACKGROUND_STAFF and not self.holds_for(key):
                c["before_orders"] = self.freshness_guard(agent, c, make_briefing,
                                                         freshness_held)
            briefing_s = time.monotonic() - started
            summary = agent.turn(c, text, picture)
            self.keep_plans_running()
            self.journal.write(event="turn_finished", agent=agent.name, date=c["date"],
                               briefing_s=round(briefing_s, 3),
                               total_s=round(time.monotonic() - started, 3))
            print("[%s] %-20s %5.1fs  %s" % (c["date"], agent.name, time.time() - t0,
                                             (summary or "(no summary)")[:150]), flush=True)
            role = self.roles.get(id(agent))
            if isinstance(agent, Marshal):
                role = ("theatre", agent.theatre["id"])
            if summary:
                summary = strip_date(summary)
                if role:
                    self.last_said[role] = "%s: %s" % (c["date"][:10], summary)
                if isinstance(agent, Marshal):
                    self.add_feed("marshals", "%s: %s" % (agent.theatre["name"], summary),
                                  date=c["date"])
                else:
                    self.add_feed(role or "supreme", summary, date=c["date"])
            for r in c.get("player_requests") or []:
                self.add_feed("marshals" if isinstance(agent, Marshal) else role or "supreme",
                              "FOR YOU: %s" % r, important=True, date=c["date"])
        except Exception as e:
            import traceback
            if getattr(self, "_startup_failures", None) is not None:
                self._startup_failures.append(agent.name)
            self.journal.write(agent=agent.name, error="%s: %s" % (type(e).__name__, e),
                               trace=traceback.format_exc()[-3000:])
            print("[%s] turn failed: %s: %s" % (agent.name, type(e).__name__, e), flush=True)
        finally:
            if freshness_held:
                self.release()
            agent.busy.release()
            self.save()

    def sync_commanders(self):
        """A commander for each fleet with a zone (harness/fleets.py); a fleet that lost
        its zone keeps his notes for when it gets one again."""
        from . import fleets
        for n in [n for n in self.commanders if n not in fleets.ZONES]:
            c = self.commanders.pop(n)
            self.fleet_memory[n] = c.memory
            self.roles.pop(id(c), None)
        for n in fleets.ZONES:
            if n not in self.commanders:
                c = fleets.FleetCommander(n, self.args.theater_model or os.environ["THEATER_MODEL"],
                                          self.llm, self.journal)
                c.memory = list(self.fleet_memory.pop(n, []))
                self.commanders[n] = c
                self.roles[id(c)] = "admiralty"

    def _commander_turn(self, name, why):
        from . import fleets
        c = self.commanders.get(name)
        if c is None:
            return
        before = len(c.memory)
        self._turn(c, lambda ctx, n=name: fleets.briefing(ctx, n), why)
        if len(c.memory) > before:      # his report goes to the Admiralty
            self.admiralty.inbox.append("%s: %s" % (name, c.memory[-1]))

    def navy_round(self, why, pause=False, only=None):
        """The Admiralty's turn, then each fleet commander's in his zone (27.10); or one
        commander alone (only)."""
        if not self.navy_lock.acquire(blocking=False):
            return
        held = pause or self.holds_for("admiralty")

        def go():
            if held:
                self.hold()
            try:
                if only is None:
                    self._turn(self.admiralty, admiralty_briefing, why)
                self.sync_commanders()
                # The Admiralty finishes redistribution first. Commanders can then
                # think together; ownership checks and every game call stay in Gateway.
                commanders = [threading.Thread(target=self._commander_turn, args=(n, why),
                                               daemon=True)
                              for n in ([only] if only else list(self.commanders))]
                for commander in commanders:
                    commander.start()
                for commander in commanders:
                    commander.join()
            finally:
                if held:
                    self.release()
                self.navy_lock.release()
        t = threading.Thread(target=go, daemon=True)
        t.start()
        self.threads.append(t)

    def zone_alarm(self, today):
        """Enemy warships at sea in a zone where no patrol or strike force of ours is: that
        zone's commander takes a turn now (27.10: the Italian fleet caught a convoy off Malta
        while the Mediterranean Fleet held the eastern basin only)."""
        from . import admiralty as A, fleets, navy

        def look(g):
            foes = navy.enemy_fleets(g, sorted(B_enemies(g)))
            held, guarded = set(), set()
            for t in navy.task_forces(g):
                if navy.OURS(t["fleet"]) and t["mission"] in fleets.GUARDS:
                    regs = set(navy.mission_regions(g, t["ptr"]))
                    guarded |= regs
                    if t["mission"] != "convoy_escort":
                        held |= regs
            return foes, held, guarded, fleets.troops_at_sea(g, self.sea)
        try:
            foes, held, guarded, troops = self.gw.run(look)
        except Exception:
            return
        zone_of = fleets.region_fleet()
        hit = collections.defaultdict(list)
        for r, n in troops.items():
            if r in zone_of and r not in guarded:
                hit[zone_of[r]].append("our %d divisions at sea in %s with no escort or patrol"
                                       % (n, self.sea.label(r)))
        for tag, tfs in foes.items():
            for x in tfs:
                r = self.sea.region_of.get(x["location"])
                if r in zone_of and r not in held and any(k in A.SURFACE for k in x["ships"]):
                    hit[zone_of[r]].append("%s %s in %s" % (tag, ", ".join(
                        "%d %s" % (n, k) for k, n in x["ships"].most_common()), self.sea.label(r)))
        for n, rows in hit.items():
            if n in self.commanders and today - self.zone_woken.get(n, -99) >= 3:
                self.zone_woken[n] = today
                self.navy_round("in your zone, nobody of ours where: " + "; ".join(rows[:4]),
                                only=n)
                return              # one round at a time

    def spawn(self, agent, make_briefing, why, pause=False):
        # --pause always: every turn stops the game, in war too (21.10: on the
        # subscription a war month passed with two rounds of turns, each queued 75-110 s)
        held = pause or self.holds_for(self.roles.get(id(agent)) or "marshals")

        def go():
            # in peace every turn stops the game: at --peace-speed a month passes in
            # the time of one model call
            if held:
                self.hold()
            try:
                self._turn(agent, make_briefing, why)
            finally:
                if held:
                    self.release()
        t = threading.Thread(target=go, daemon=True)
        t.start()
        self.threads.append(t)

    def supreme_turn(self, why, wait=False, pause=False):
        pause = pause or self.holds_for("supreme")
        if wait:
            self._turn(self.supreme, briefing.supreme, why)
        elif pause:
            t = threading.Thread(target=self._paused_supreme, args=(why,), daemon=True)
            t.start()
            self.threads.append(t)
        else:
            self.spawn(self.supreme, briefing.supreme, why)

    def new_reasons(self, key, why):
        """The alarm's reasons now, and those the staff was not woken for at its last wake
        (numbers aside). One that went away and came back (a slot freed again) is new."""
        blank = lambda r: re.sub(r"\d+(\.\d+)?", "#", r)
        now = [r for r in (why or "").split("; ") if r]
        old = self.why_seen.get(key, [])
        seen = {blank(r) for r in old} & {blank(r) for r in now}
        self.why_seen[key] = [r for r in old if blank(r) in seen]
        return now, [r for r in now if blank(r) not in seen]

    def ask_for_plan(self, reason):
        """The supreme command calls the planning session early (at most once a month)."""
        today = day_of(self.gw.run(ops.date))
        if self.last_plan_day is not None and today - self.last_plan_day < 30:
            raise ops.OpError("the plan was set %d days ago; the planning session meets "
                              "again from %s, or when a war begins or ends"
                              % (today - self.last_plan_day,
                                 planning.day_text(self.last_plan_day + 30)))
        self.plan_asked = reason
        return {"called": True, "note": "the planning session meets now; you will see "
                                        "the new plan in your next briefing"}

    def planner_turn(self, why, held=True, deep=False):
        """Le Chonk's planning session: the game stops for as long as it thinks, then
        every executor reads the new plan in a turn of its own. At the start of a session
        the game is already stopped (held=False) and the executors wait for the clock
        (a turn spawned before it lets the game run when it ends; 20.10)."""
        if held:
            self.hold()
        self.journal.write(event="planning", why=why)
        print("    .. planning session: %s" % why, flush=True)
        before = dict(self.planner.plan)
        self.planner.set_depth(deep)
        try:
            self._turn(self.planner, planning.briefing, why)
        finally:
            if held:
                self.release()
        self.last_plan_day = day_of(self.gw.run(ops.date))
        changed = self.planner.plan != before
        if changed and held:
            self.wake_executors()
        return changed

    def wake_executors(self):
        """Every executor reads the new plan in a turn of its own."""
        for agent, make, last in ((self.industry, briefing.industry, "last_industry"),
                                  (self.politics, briefing.politics, "last_politics"),
                                  (self.army, briefing.army_staff, "last_army"),
                                  (self.air_staff, briefing.air_staff, "last_air"),
                                  (self.admiralty, admiralty_briefing, "last_navy")):
            setattr(self, last, self.last_plan_day)
            self.spawn(agent, make, "a new plan from Le Chonk's planning session")
        self.supreme.inbox.append("A new plan from your planning session (THE PLAN).")

    def start_staff_round(self, new_plan):
        """Keep the initial pause until the opening staff turns have finished."""
        self.hold()
        self._startup_failures = []
        try:
            first = len(self.threads)
            if new_plan:
                self.wake_executors()
            else:
                self.spawn(self.industry, briefing.industry, "session start")
                # A failed opening turn has no saved memory. Resume must retry it
                # before releasing the clock, without rerunning the planner.
                for agent, make in ((self.politics, briefing.politics),
                                    (self.army, briefing.army_staff),
                                    (self.air_staff, briefing.air_staff),
                                    (self.admiralty, admiralty_briefing)):
                    if not agent.memory:
                        self.spawn(agent, make, "retry unfinished campaign opening")
            for thread in self.threads[first:]:
                thread.join()
            if self._startup_failures:
                raise RuntimeError("opening staff turns failed: " +
                                   ", ".join(self._startup_failures))
        except BaseException:
            self.stopping = True
            raise
        finally:
            self._startup_failures = None
            self.release()

    def keep_plans_running(self):
        """After any turn: each theatre at war runs, or stands, as its marshal last
        decided; orders drawn since then are brought in line (see ops.enforce_execution)."""
        ctx = self.ctx()
        for tid, m in list(self.marshals.items()):
            if not agents.at_war_in(ctx, m):
                continue
            try:
                off = self.gw.run(ops.enforce_execution, tid, m.theatre.get("executing", True))
            except ops.OpError:
                continue
            if off:
                self.journal.write(event="plan_brought_in_line", theatre=tid, orders=off,
                                   executing=m.theatre.get("executing", True))

    @staticmethod
    def our_faction(g):
        try:
            return sorted(t for t in g.faction_members(g.player_tag())["members"]
                          if isinstance(t, str))
        except Exception:               # in no faction the pointer is not a faction
            return []

    def keep_lines(self):
        """A theatre's line partly on no army's sector is re-cut by code the same day, and
        the field marshal told (21.10: his sectors stood on the front with Belgium; as
        Germany took Belgian land that front shrank and the armies marched after it)."""
        ctx = self.ctx()
        regs = agents.regions(ctx)
        for tid, m in list(self.marshals.items()):
            if not m.theatre.get("auto_cover", True):
                continue
            try:
                out = self.gw.run(ops.cover_bare, self.mv, ctx["holders"], tid,
                                  regs.get(tid, set(m.theatre["states"])))
            except (ops.OpError, OSError) as e:      # OSError: a front freed under us
                self.journal.write(event="cover_failed", theatre=tid, error=str(e))
                continue
            if not out:
                continue
            self.journal.write(event="cover", theatre=tid, **out)
            if out["given"]:
                # a new sector starts idle: the plan runs, or stands, as the marshal set it
                try:
                    self.gw.run(ops.enforce_execution, tid, m.theatre.get("executing", True))
                except ops.OpError:
                    pass
            print("    .. line covered in %s: %s" % (m.theatre["name"], "; ".join(
                "army %s took %s" % (a, ", ".join(v)) for a, v in out["given"].items())
                or "nobody free"), flush=True)
            seen = sorted(set(out["bare"]))
            if (out["given"] or out["left"]) and (out["given"] or seen != m.theatre.get("cover_seen")):
                m.theatre["cover_seen"] = seen
                m.inbox.append(
                    "AUTO-COVER (your standing order; set_auto_cover off to stop it): the line "
                    "at %s stood on no army's sector. %s%s Your other sectors and attacks were "
                    "not touched." % (
                        ", ".join(sorted(set(out["bare"]))), "".join(
                            "Army %s took %s as an extra sector. " % (a, ", ".join(v))
                            for a, v in out["given"].items()),
                        ("Still bare (no army that is not attacking within %d provinces): %s."
                         % (ops.COVER_REACH, ", ".join(out["left"]))) if out["left"] else ""))

    def tactics(self, today):
        """The tactical staff of every theatre at war, every --tactics-every days (code:
        harness/tactics.py; the user, 25.10)."""
        every = self.args.tactics_every
        if not every:
            return
        from . import tactics
        ctx = None
        for tid, m in list(self.marshals.items()):
            last = m.theatre.get("tactics_day")
            if last is not None and today - last < every:
                continue
            ctx = ctx or self.ctx()
            if not agents.at_war_in(ctx, m):
                continue
            m.theatre["tactics_day"] = today
            enemies = set(ops.tags_of(agents.line_of(ctx, m)))
            try:
                done = self.gw.run(tactics.tick, self.mv, tid, enemies,
                                   m.theatre.get("posture") or {})
            except (ops.OpError, OSError) as e:
                self.journal.write(event="tactics_failed", theatre=tid, error=str(e))
                continue
            if not done:
                continue
            self.journal.write(event="tactics", theatre=tid, orders=done)
            text = "; ".join("%s: %s" % kv for kv in done)
            print("    .. tactics in %s: %s" % (m.theatre["name"], text[:300]), flush=True)
            m.theatre["tactics_last"] = [ctx["date"][:10], text[:900]]

    def stage_reserves(self):
        """Far reserve armies are brought to their theatre's line by code (ops.stage_reserves)."""
        try:
            ctx = self.ctx()
            regs = agents.regions(ctx)
        except Exception:
            return
        for tid, m in list(self.marshals.items()):
            try:
                out = self.gw.run(ops.stage_reserves, self.mv, ctx["holders"], tid,
                                  regs.get(tid, set(m.theatre["states"])))
            except (ops.OpError, OSError) as e:
                self.journal.write(event="stage_failed", theatre=tid, error=str(e))
                continue
            if not out:
                continue
            self.journal.write(event="stage_reserves", theatre=tid, armies=out)
            print("    .. reserves of %s: %s" % (m.theatre["name"], ", ".join(
                "army %s %s" % kv for kv in out.items())), flush=True)
            m.inbox.append("The harness brings your reserve armies that stood far from your "
                           "line to it (a temporary front, the game marches and ships them), "
                           "and takes the front away when they are near: %s. They are your "
                           "reserve behind the line then." % ", ".join(
                               "army %s %s" % kv for kv in out.items()))

    def peace(self):
        return not self.at_war

    def marshals_awake(self):
        """In peace the field marshals take no scheduled turns until a war goal is being
        justified against us or by an enemy they stand against (the user, 19.10: they may
        sleep until 1939); Le Chonk's orders wake them at any time."""
        return not self.peace() or bool(self.tension)

    def drill_at_war(self, today):
        """At war nothing that fights drills (the user, 24.10: Ozawa's carriers trained at
        Regular two months into the China war). The armies of a theatre stop their
        exercises; task forces and wings that stood when the war began stop training (the
        ships hold in port, the wings are the air staff's to send). Only what is new since
        drills, and only while no theatre needs it. Every 3 days."""
        if today - self.drill_day < 3:
            return
        self.drill_day = today
        from . import air as _air, navy as _navy
        v = self.veterans
        if not v["taken"]:
            v["tf"] = {t["name"] for t in self.gw.run(_navy.task_forces)}
            v["wings"] = {w["id"] for w in self.gw.run(_air.wings)}
            v["taken"] = True

        def look(g):
            th = {ops._hid(g, og) for tid in list(self.marshals)
                  for og in ops.theatre_armies(g, tid)}
            return th, {a for a, r in ops.drill_picture(g).items() if r[3]}
        placed, drilling = self.gw.run(look)
        stop = sorted(drilling & placed)
        if stop:
            self.gw.run(ops.exercise, stop, False)
            print("    .. drill: stopped the exercises of armies %s (in a theatre, at war)"
                  % stop, flush=True)
            self.journal.write(event="drill_stopped", armies=stop)
        spare = sorted(drilling - placed)
        if spare:
            ctx = self.ctx()
            sit = self.gw.run(ops.situation, self.mv, ctx["holders"], agents.regions(ctx),
                              set(ctx["enemies"]), agents.foes(ctx))
            holes = ["%s: %s" % (self.marshals[t].theatre["name"], x)
                     for t, al in sit.items() if t in self.marshals
                     for k, _, x in al if k == "HOLE"]
            key = (tuple(spare), bool(holes))
            if holes and self.drill_told.get("army") != key:
                self.drill_told["army"] = key
                self.alarms.append(("supreme", "armies %s drill in reserve while theatres "
                                    "have holes (%s): give them to a theatre now, the drill "
                                    "can wait" % (spare, "; ".join(holes)[:400])))
        tfs = [t for t in self.gw.run(_navy.task_forces)
               if t["mission"] == "training" and t["name"] in v["tf"]]
        for t in tfs:
            self.gw.run(_navy.set_task_force_mission, t["ptr"], "hold")
        if tfs:
            names = [t["name"] for t in tfs]
            print("    .. drill: task forces %s off training, holding (at war)" % names,
                  flush=True)
            self.journal.write(event="drill_stopped", task_forces=names)
            self.alarms.append(("navy", "at war the fleet that fights does not drill: %s "
                                "stopped training and hold in port. Give them a mission the "
                                "war plan needs (a strike force, escort, support of a "
                                "landing)" % ", ".join(names)))
        ws = sorted(w["id"] for w in self.gw.run(_air.wings)
                    if any("training" in m for m in w["missions"]) and w["id"] in v["wings"])
        if ws and self.drill_told.get("air") != ws:
            self.drill_told["air"] = ws
            self.alarms.append(("air", "at war the air force that fights does not drill: "
                                "wings %s are still training. Send them on the missions the "
                                "front needs now (a new mission replaces the training)" % ws))

    def pre_war(self, m):
        """A theatre whose enemy the plan's wars name: its marshal plans that war in peace,
        on the schedule like at war (24.10, the user: the arrows must be whole before the
        war; Kan'in drew two in August 1936 and slept until the war, the plan's monthly
        review never came)."""
        wars = (self.planner.plan or {}).get("wars") or []
        return any(m.theatre.get("enemy") in ops.tags_of(w.get("enemy") or "") for w in wars)

    def watch_world(self, today):
        """World tension crossing the line where guarantees open wakes Le Chonk once
        (19.10: France never guaranteed Poland and sat out 1939; at 23% the game refuses)."""
        wt = self.gw.run(lambda g: B_module("diplomacy").world_tension(g)) or 0
        if wt >= 0.25 and not self.tension_told:
            self.tension_told = True
            self.alarms.append(("supreme", "world tension is %.0f%%: guarantees of independence "
                                           "are open now" % (wt * 100)))

    def watch_war_goals(self, today):
        """A war of the plan whose GO BY is under 90 days off, with no war goal held or being
        justified and no event of the game bringing it, wakes Le Chonk once per war and
        date (24.10: the declaration of 1938.02.08 was refused for want of a war goal)."""
        from . import warplan
        if getattr(self, "_goal_day", None) == today:
            return
        self._goal_day = today
        told = self.__dict__.setdefault("goal_told", set())
        c = None
        for w in (self.planner.plan or {}).get("wars") or []:
            go = planning.day_number(w.get("go_by"))
            if go is None or (w["enemy"], w.get("go_by")) in told:
                continue
            c = c or self.ctx()
            now = planning.day_number(c["date"])
            if now is None or not 0 <= go - now <= 90:
                continue
            g = warplan.war_goal(c, w)
            if not g or g[1]:
                continue
            told.add((w["enemy"], w.get("go_by")))
            text = ("the war on %s is to be declared by GO BY %s, %d days off, and we hold no "
                    "war goal against it, none is being justified and no event of the game "
                    "brings that war: the game refuses a declaration without one. Start "
                    "justify_war_goal now, or move the war" % (w["enemy"], w["go_by"],
                                                             go - now))
            print("    .. war goal: %s" % text, flush=True)
            self.supreme.inbox.append("ALERT: " + text)
            self.alarms.append(("supreme", text))

    def watch_guarantees(self, today):
        """A war against a country we guarantee wakes Le Chonk once per attacker: the
        guarantee does not take us into it (20.10: Germany took Warsaw, France guaranteed
        Poland, and nobody woke the supreme for four weeks)."""
        if self.last_guarantee_day == today:
            return
        self.last_guarantee_day = today
        cb, dip = B_module("combat"), B_module("diplomacy")

        def read(g):
            return {t: cb.enemy_tags(g, t) for t in dip.guarantees(g)["gives"]}
        ours = set(self.gw.run(B_enemies))
        new = {(t, e) for t, es in self.gw.run(read).items() for e in es
               if e not in ours} - self.guarantee_wars
        if not new:
            return
        self.guarantee_wars |= new
        text = "; ".join("%s is at war with %s, whose independence we guarantee"
                         % (e, t) for t, e in sorted(new))
        text += (". We are NOT in that war: a guarantee does not bring us in by itself; "
                 "the attacked country calls us to arms (REQUESTS), and whether we enter is "
                 "yours to decide")
        self.journal.write(event="guarantee_war", new=sorted(new))
        print("    .. guarantee: %s" % text, flush=True)
        self.supreme.inbox.append("ALERT: " + text)
        self.alarms.append(("supreme", text))

    def refresh_forts(self, today):
        """The map's forts as they stand, once a game day (20.10: the 1936 history alone
        hid the Westwall and every raised fort since)."""
        if self.forts_day == today:
            return
        self.forts_day = today
        forts = self.gw.run(lambda g: B_module("mapdata").fort_levels(g))
        if forts:
            self.mv.forts = forts
        # ports too (25.10: the 1936 levels kept Kitakyushu at 6 in every briefing, and
        # the Shanghai landing waited three months on a level already built)
        try:
            ports = self.gw.run(lambda g: B_module("mapdata").port_levels(g))
        except Exception:
            ports = None
        if ports:
            self.mv.ports = ports

    def watch_requests(self):
        """A request from another country wakes Le Chonk: a call to arms or a faction at
        once for a strategic turn, the rest (access, docking, lend-lease) his quick turn on
        events. Unanswered they lapse (31.08.1939: Poland called France to arms against
        Germany and the harness never saw it)."""
        now = {r["request"]: r for r in self.gw.run(ops.requests)}
        new = [r for k, r in now.items() if k not in self.requests_seen]
        self.requests_seen &= set(now)
        if not new:
            return
        major = [r for r in new if r["major"]]
        if major:
            text = "request waiting for your answer: " + "; ".join(r["what"] for r in major)
            print("    .. %s" % text, flush=True)
            self.alarms.append(("supreme", text))
            self.requests_seen |= {r["request"] for r in major}
        minor = [r for r in new if not r["major"]]
        if minor and not self.supreme.busy.locked() and not self.chancellery.busy.locked():
            print("    .. requests: %s" % "; ".join(r["what"] for r in minor), flush=True)
            self.spawn(self.chancellery, briefing.events, "requests waiting")
            self.requests_seen |= {r["request"] for r in minor}
        told = [r["what"] for r in new if r["request"] in self.requests_seen]
        if told:
            self.journal.write(event="request", new=told)

    def watch_tension(self, today):
        """The game's alert 'X is justifying against us', widened to the enemies our
        theatres expect justifying against anyone: new ones wake Le Chonk and the marshals."""
        if self.last_tension_day == today:
            return
        self.last_tension_day = today
        foes = {m.theatre["enemy"] for m in self.marshals.values()}
        report = self.gw.run(threats.cached_snapshot, self.own)
        now = {(j, t) for j, t in report["pairs"]
               if t == self.own or j in foes}
        new = now - self.tension
        self.tension = now
        if not new:
            return
        text = "; ".join("%s is justifying a war goal against %s"
                         % (j, "US" if t == self.own else t) for j, t in sorted(new))
        focus_sources = {t["from"]: t.get("focus_key") for t in report["threats"]
                         if t["source"] == "focus" and (t["from"], t["target"]) in new}
        if focus_sources:
            text += " (war focuses: %s)" % ", ".join("%s %s" % x for x in sorted(focus_sources.items()))
        self.journal.write(event="justification", new=sorted(new))
        print("    .. war goal: %s" % text, flush=True)
        self.supreme.inbox.append("ALERT: " + text)
        self.alarms.append(("supreme", text))
        for tid, m in self.marshals.items():
            if any(j == m.theatre["enemy"] or t == self.own for j, t in new):
                m.inbox.append("ALERT: " + text + ". War may come: take your line now.")
                self.alarms.append((tid, text))

    def sample_clock(self, date):
        """Wall/game progress independent of whether an agent happens to take a turn."""
        now = time.monotonic()
        last = getattr(self, "_last_clock_sample", None)
        if last is None or now - last >= 30:
            self.journal.write(event="clock_sample", date=date, holds=self._holds)
            self._last_clock_sample = now

    def speed(self):
        """Peace runs fast and stops for every decision (the user, 19.10: at speed 2
        France 1936 would die of old age before 1939); war runs at --speed."""
        return self.setting("speed_war" if self.at_war else "speed_peace") - 1

    def setting(self, key):
        """A setting of the run: roster.json (the panel's SETTINGS tab) or the command line.
        Speeds as the game shows them (1-5)."""
        v = roster.get().get(key)
        if v is not None:
            return v
        a = self.args
        if key.startswith("pause_"):
            if a.pause == "never":
                return "alarms"
            if a.pause == "always":
                return "always"
            return "alarms" if key[6:] in BACKGROUND_STAFF else "battle"
        return {"speed_peace": a.peace_speed + 1, "speed_war": a.speed + 1,
                "marshal_every": a.marshal_every, "war_marshal_every": a.war_marshal_every,
                "supreme_every": a.supreme_every, "staff_every": 30,
                "navy_war_every": 7}[key]

    def settings_now(self):
        """Every setting in force (the panel shows and pushes them)."""
        out = {k: self.setting(k) for k, _, _, _, _ in roster.SETTINGS}
        out.update({"pause_" + a: self.setting("pause_" + a) for a in roster.PAUSE_AGENTS})
        return out

    def holds_for(self, key):
        """Whether a turn of this agent (roster.PAUSE_AGENTS) stops the game: always; in
        peace and in a land war; or on alarms at war. Peace always waits for decisions
        unless the run explicitly disables pauses with --pause never."""
        mode = self.setting("pause_" + key)
        if mode == "always":
            return True
        if mode == "alarms":
            # At speed 5 a peaceful month can pass within one model response.
            # Background staff are useful at war; peace preparation waits for them.
            return self.args.pause != "never" and self.peace()
        return self.peace() or self.land_war()

    def run_clock(self):
        s = self.speed()
        self.gw.run(ops.set_running, True, s)
        self._speed_set = s

    def apply_speed(self):
        """A speed changed in the panel, or war came or went: the running game takes it."""
        if self._holds or self.stopping or self.speed() == getattr(self, "_speed_set", None):
            return
        with self._hold_lock:
            if self._holds == 0:
                self.run_clock()

    def hold(self):
        """Pause the game for a turn; several turns may hold it at once (a marshals'
        round during Le Chonk's turn), the last to finish lets it run again."""
        with self._hold_lock:
            if self._holds == 0:
                self.gw.urgent(ops.set_running, False)
                self._pause_started = time.monotonic()
                self.journal.write(event="clock_hold")
            self._holds += 1

    def release(self):
        with self._hold_lock:
            self._holds -= 1
            if self._holds == 0:
                self.journal.write(event="clock_release",
                                   paused_s=round(time.monotonic() - self._pause_started, 3),
                                   stopping=self.stopping)
                if not self.stopping:
                    self.run_clock()

    def _marshal_round(self, ms, why):
        """The marshals' turns with the game paused, all at once. Unpaused, Large 4
        marshals (10-80 s a turn) acted once every ~6 game days at speed 2 (17.10)."""
        self.hold()
        try:
            ts = [threading.Thread(target=self._turn,
                                   args=(m, lambda c, m=m: briefing.marshal(c, m), why),
                                   daemon=True) for m in ms]
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=900)   # a planning session thinks up to 10 minutes
        finally:
            self.release()

    def _paused_supreme(self, why):
        """Strategic pause: the game stops for every Le Chonk turn. At speed 2 a game day
        passes in 3-4 s and a supreme turn takes minutes; unpaused, East Prussia fell and
        two weeks went by before the turn about it was over, and a scheduled review begun
        on 25.08 was still thinking on 14.09 with the war not declared (17.10). The turn's
        own budget bounds the pause."""
        if self.supreme.busy.locked():
            return
        self.hold()
        self.journal.write(event="strategic_pause", why=why)
        print("    .. strategic pause: %s" % why, flush=True)
        try:
            self._turn(self.supreme, briefing.supreme, why)
        finally:
            self.release()

    def marshal_turn(self, m, why):
        if m.theatre["id"] not in self.gw.run(ops.theatres):
            # closed by the game: its last forces left
            self.marshals.pop(m.theatre["id"], None)
            self.supreme.inbox.append("The theatre %r (%d) no longer exists; %s has no command."
                                      % (m.theatre["name"], m.theatre["id"], m.name))
            self.journal.write(event="theatre_gone", theatre=m.theatre)
            return
        self.marshal_round([m], why)

    def planning(self, tag, today=None):
        """'ready', or what still keeps the war against `tag` from opening: deployment
        (ops.readiness), then --plan-days of planning with the attacks drawn, the time the
        divisions need to build the planning bonus (the user, 17.10: draw the arrows
        first, then wait for the planning modifier)."""
        if today is None:
            today = day_of(self.gw.run(ops.date))
        ctx = self.ctx()
        reg = agents.regions(ctx)
        todo = self.gw.run(ops.readiness, self.mv, ctx["holders"],
                           {tid: reg[tid] for tid, m in self.marshals.items()
                            if m.theatre["enemy"] == tag or tag in agents.facing(ctx, m)}, tag)
        if todo:
            self.ready_since.pop(tag, None)
            return todo
        since = self.ready_since.setdefault(tag, today)
        if today - since < self.args.plan_days:
            return ["deployed; the attacks have been planned for %d of %d days (the "
                    "planning bonus is still building)" % (today - since, self.args.plan_days)]
        return "ready"

    def fires(self):
        """What is burning now (ops.situation at war): holes, pockets, and armies whose
        attack has just run out. Keys, so that only a NEW fire pauses the game (17.10:
        pausing for every turn made the war crawl; the Danzig pocket had been the reason)."""
        ctx = self.ctx()
        cmds = [tid for tid, m in list(self.marshals.items()) if agents.at_war_in(ctx, m)]
        if not cmds:
            return set()
        try:
            sit = self.gw.run(ops.situation, self.mv, ctx["holders"], agents.regions(ctx),
                              set(ctx["enemies"]), agents.foes(ctx))
        except ops.OpError:
            return set()
        out = set()
        for gid in cmds:
            rows = sit.get(gid, [])
            self.digest[gid] = frozenset(re.sub(r"\d+", "#", t) for _, _, t in rows)
            out |= {key for k, key, _ in rows if k in ("HOLE", "POCKET")}
            # arrows done: the game deletes a reached arrow and the army stands until
            # someone draws the next (17.10: Warsaw fell and the front stood two weeks)
            idle = {key.split()[-1] for k, key, _ in rows if k == "IDLE"}
            still = {key.split()[-1] for k, key, _ in rows if k in ("IDLE", "NO SECTOR")}
            was = self.attacking.get(gid)
            if was is not None:
                out |= {"arrows done: army %s" % a for a in idle - was}
            self.attacking[gid] = still
        return out

    YOUNG_WAR = 14              # days: the opening of a war, when the enemy line is thinnest

    def war_young(self, today=None):
        """A war of ours began within YOUNG_WAR days."""
        if today is None:
            today = day_of(self.gw.run(ops.date))
        return any(today - d <= self.YOUNG_WAR for t, d in self.war_begun().items())

    def war_begun(self):
        return {t: d for t, d in self.war_began.items() if t in self.at_war}

    def marshal_every(self, today):
        """Days between a marshal's scheduled turns: --marshal-every in peace, at most
        --war-marshal-every at war, every other day in a war's first two weeks (25.10, n3:
        Beiping went from 5 Chinese divisions to 50 in the seven days between Kan'in's
        first two turns of the war)."""
        every, war_every = self.setting("marshal_every"), self.setting("war_marshal_every")
        if self.peace():
            return every
        if self.war_young(today):
            return min(every, 2)
        if not self.land_war():
            return every
        return min(every, war_every)

    def land_war(self):
        """A land war of ours: a state we or our subjects control touches one the enemy
        controls, the enemy holds one of ours, or our divisions fight somewhere (an
        ally's front too). Without one (26.10, the user: the UK in the Phony War, the
        Channel between it and Germany, and the game stood paused for turn after turn)
        --pause always pauses only as fires does, and the marshals keep the peace pace."""
        if self.peace():
            return False
        try:
            today = day_of(self.gw.run(ops.date))
            if self._land_war and self._land_war[0] == today:
                return self._land_war[1]
            h = self.gw.run(self.holders)
            en = set(self.gw.run(lambda g: B_enemies(g)))
            try:
                ours = {self.own} | set(self.gw.run(
                    lambda g: B_module("combat").subject_tags(g)))
            except Exception:
                ours = {self.own}
            front = any(self.mv.front_states(h, t, en)[0] for t in ours)
            held = any(o in ours and c in en for o, c in h.values())
            fight = bool(front or held) or bool(self.gw.run(
                lambda g: B_module("combat").combats(g, only_mine=True)))
        except Exception:
            return True
        if not self._land_war or self._land_war[1] != fight:
            print("    .. land war: %s" % ("yes, turns pause the game" if fight else
                  "none (no front, no fighting): turns run with the game"), flush=True)
        self._land_war = (today, fight)
        return fight

    def marshal_round(self, ms, why, pause=None):
        ms = [m for m in ms if not m.busy.locked()]
        if not ms:
            return
        if pause is None:
            pause = self.holds_for("marshals")
        if not pause and self.setting("pause_marshals") != "alarms":
            # a planning session thinks for minutes; unpaused at war, Kan'in's of 07.03.1938
            # took 178 s and ended in April, the front standing all the while (25.10, n3).
            # The war's first days are paused too: the opening blow is decided by the day
            try:
                ctx = self.ctx()
                pause = self.war_young(ctx["day"]) or any(m.plan_due(ctx) for m in ms)
            except Exception:
                pass
        if pause:
            t = threading.Thread(target=self._marshal_round, args=(ms, why), daemon=True)
            t.start()
            self.threads.append(t)
        else:
            for m in ms:
                self.spawn(m, lambda c, m=m: briefing.marshal(c, m), why)

    def events_turn(self):
        """The chancellery answers new events. A new event seen while the supreme was busy
        stays new for the next pass (it was marked seen and never answered)."""
        ev = {e["window"] for e in self.gw.run(ops.pending_events)}
        if ev - self.events_seen and not self.supreme.busy.locked() \
                and not self.chancellery.busy.locked():
            self.spawn(self.chancellery, briefing.events, "events waiting")
            self.events_seen = ev
        else:
            self.events_seen &= ev

    def focus_startable(self, today):
        """Whether the game would let a focus start now, asked once a game day (each focus
        is a CanExecute inside the game). 20.10: with nothing startable in the tree the
        minister was woken every day for seven weeks to say so."""
        if self.focus_checked[0] != today:
            self.focus_checked = (today, bool(self.gw.run(
                lambda g: B_module("cabinet").startable_focuses(g))))
        return self.focus_checked[1]

    def peace_stops(self, today):
        """At --peace-speed a day passes in a fraction of the loop: a finished focus and a
        new event stop the game at once, before the day's other checks (the user, 19.10:
        pause on a finished focus and on events such as the Rhineland)."""
        if not self.politics.busy.locked() \
                and (self.last_politics is None or today - self.last_politics >= 1) \
                and self.gw.run(lambda g: g.current_focus()) is None \
                and self.focus_startable(today):
            self.last_politics = today
            self.politics_pp = self.gw.run(lambda g: g.player().political_power)
            self.spawn(self.politics, briefing.politics, "the national focus is done; "
                       "no focus is running")
        self.events_turn()

    # ---------------------------------------------------------------- main loop
    def holders(self, g):
        """state -> (owner, controller), with our subjects' states controlled by us: their
        borders are ours to hold and fight from (23.10: Japan's border with the USSR and
        North China is Manchukuo's). The owner stays theirs: we cannot build there."""
        h = self.mv.holders(g)
        subj = set(B_module("combat").subject_tags(g))
        if subj:
            h = {sid: (o, self.own if c in subj else c) for sid, (o, c) in h.items()}
        return h

    def panel_loop(self):
        """The in-game panel in a thread of its own: clicks read 4 times a second and
        answered at once, not when the campaign loop comes round (24.10: a page turn took
        ~2 s); every 10 s the texts anew, every second while an agent answers the player."""
        while not self.stopping:
            try:
                self.ui.poll()
                if self.ui.present and (time.time() - self.status_at >
                                        (1 if self.chat.busy else 10) or self.ui.changed):
                    self.ui.changed = False
                    full = time.time() - self.plan_text_at > 120
                    self.status_at = time.time()
                    if full:
                        self.plan_text_at = self.status_at
                    self.ui.write(self.panel_texts(full))
            except Exception as e:
                print("in-game panel: %s: %s" % (type(e).__name__, e), flush=True)
                time.sleep(5)
            time.sleep(modui.POLL)

    def run(self):
        threading.Thread(target=self.panel_loop, daemon=True).start()
        a = self.args
        self.gw.run(ops.set_running, False)
        self.refresh_forts(day_of(self.gw.run(ops.date)))
        today0 = day_of(self.gw.run(ops.date))
        new_plan = False
        note = os.path.join(ROOT, "logs", ".note_planner")
        if os.path.exists(note):
            # a one-time note to the planning session convenes it now
            text = open(note, encoding="utf-8").read().strip()
            # "DEEP" on its first line: a session for the course of years (21.10: the war
            # session of 1939.09.01 was cut by a restart; it is called again by note)
            deep = text.startswith("DEEP")
            self.planner.inbox.append("HARNESS NOTE: " + text[4:].strip() if deep else
                                      "HARNESS NOTE: " + text)
            os.remove(note)
            new_plan = self.planner_turn("a note from the harness", held=False, deep=deep)
        elif not self.planner.plan:
            new_plan = self.planner_turn("campaign start: the first plan", held=False,
                                         deep=True)
        elif today0 >= self.planner.due_day(today0):
            new_plan = self.planner_turn("the plan's review is due", held=False)
        self.faction_seen = self.gw.run(self.our_faction)
        print("planning with the game paused: Le Chonk's opening turn", flush=True)
        self.supreme_turn("campaign start", wait=True)
        self.at_war = set(self.gw.run(B_enemies))
        if self.peace():
            self.watch_tension(day_of(self.gw.run(ops.date)))
        for m in list(self.marshals.values()) if self.marshals_awake() else []:
            # first orders before the clock runs
            self._turn(m, lambda c, m=m: briefing.marshal(c, m), "campaign start")
        # a sleeping marshal had no opening turn: Le Chonk's message from the opening turn
        # still wakes him (20.10: "close Haguenau" was cleared here with the rest)
        keep = [x for x in self.alarms if x[0] in self.marshals and not self.marshals_awake()]
        self.alarms.clear()
        self.alarms.extend(keep)
        self.fires_seen = dict.fromkeys(self.fires(), day_of(self.gw.run(ops.date)))
        # (what already burns was planned for, paused)
        start = day_of(self.gw.run(ops.date))
        self.sync_roster(start)
        for m in list(self.marshals.values()) + list(self.parked.values()):
            m.last_turn_day = start
        last_supreme = start
        self.at_war = set(self.gw.run(B_enemies))
        # the minister of industry looks at the lines at every start, not only at the
        # month's review (20.10: a fix to the factory counts waited four weeks for him).
        # Wars and opening marshal turns are already read; keep a shared pause until
        # the initial staff round finishes so fast peace speed cannot outrun it.
        self.industry.inbox.append(
            "HARNESS NOTE (20.10): three of your tools were broken until now. "
            "set_line_factories counted from the factories working on a line, not those it "
            "asked for, and piled the requests up (the requests are set back to what works). "
            "cancel_construction removed nothing (every coastal fort you cancelled stayed in "
            "the queue). And you did not see what the army needs: THE ARMY'S EQUIPMENT now "
            "shows what our divisions' templates use, against stock and daily output. Look "
            "at the lines and the queue again. "
            if not os.path.exists(os.path.join(ROOT, "logs", ".industry_told"))
            else "Start of a session: check the production lines.")
        # a one-time note from us to an agent (logs/.note_<supreme|industry|...>), read
        # and removed at the start of a session; the supreme is woken for his
        for who, agent in (("supreme", self.supreme), ("industry", self.industry),
                           ("politics", self.politics), ("army", self.army)):
            path = os.path.join(ROOT, "logs", ".note_" + who)
            if os.path.exists(path):
                agent.inbox.append("HARNESS NOTE: " + open(path, encoding="utf-8").read().strip())
                os.remove(path)
                if who == "supreme":
                    self.alarms.append(("supreme", "a note from the harness"))
        self.start_staff_round(new_plan)
        open(os.path.join(ROOT, "logs", ".industry_told"), "a").close()
        prev_day = start
        lost_seen = len(briefing.lost_ground(self.ctx()))
        enemies_seen = set(self.gw.run(B_enemies))
        clock = [self.gw.run(lambda g: g.date_raw()), time.time()]
        try:
            while True:
                time.sleep(0.5)
                self.check_clock(clock)
                date = self.gw.run(ops.date)
                today = day_of(date)
                self.sample_clock(date["text"])
                self.sync_roster(today)
                self.apply_speed()
                self.refresh_forts(today)
                if self.peace():
                    self.peace_stops(today)
                    self.watch_tension(today)
                    self.watch_world(today)
                    self.watch_guarantees(today)
                self.watch_war_goals(today)
                if today != prev_day:
                    for target, why in self.letter_waiting.items():
                        self.alarms.append((target, why + " (waited a day)"))
                    self.letter_waiting.clear()
                for target, why in coalesce_alarms(self.alarms):
                    if (why.startswith(LETTERS) and "the player" not in why
                            and not self.peace()):
                        if self.letter_woken.get(target) == today:
                            self.letter_waiting.setdefault(target, why)
                            continue
                        self.letter_woken[target] = today
                    if "from the player" not in why:
                        self.add_feed(ALARM_KEYS.get(target, "marshals"), "ALARM: " + why,
                                      important=True)
                    if target == "supreme":
                        if self.peace() and why.startswith("request from"):
                            # in peace a request waits in the inbox: a week between such
                            # wakes (19.10: the colonial groups asked every turn for
                            # divisions France has not got, and every answer woke them)
                            if today - self.last_request_wake < 7:
                                continue
                            self.last_request_wake = today
                        self.supreme_turn(why)
                        last_supreme = today
                    elif target == "staff":
                        # an order from the supreme: the staff acts on it now
                        self.last_army = today
                        self.spawn(self.army, briefing.army_staff, why)
                    elif target == "industry":
                        self.last_industry = today
                        self.spawn(self.industry, briefing.industry, why)
                    elif target == "politics":
                        self.last_politics = today
                        self.spawn(self.politics, briefing.politics, why)
                    elif target == "air":
                        self.last_air = today
                        self.spawn(self.air_staff, briefing.air_staff, why)
                    elif target == "navy":
                        self.last_navy = today
                        self.navy_round(why)
                    elif target in self.marshals:
                        # an order wakes a field marshal even in peace (the user, 19.10:
                        # Le Chonk orders them to dig in along a border); only the
                        # scheduled turns wait for a war goal
                        self.marshal_turn(self.marshals[target], why)
                if today == prev_day:
                    continue
                prev_day = today
                if today - start >= a.days:
                    break
                if not self.peace():
                    self.keep_lines()
                    try:
                        self.tactics(today)
                    except Exception as e:
                        print("    .. tactics failed: %r" % e, flush=True)
                    try:
                        self.drill_at_war(today)
                    except Exception as e:
                        print("    .. drill check failed: %r" % e, flush=True)
                elif self.veterans["taken"]:
                    self.veterans = {"tf": set(), "wings": set(), "taken": False}
                self.stage_reserves()
                due = []
                # in peace a field marshal acts only on Le Chonk's orders (the alarms
                # above: a directive, a front, a transfer), never on a schedule (19.10)
                awake = self.marshals_awake()
                for m in [m for m in list(self.marshals.values())
                          if awake or self.pre_war(m)]:
                    if m.last_turn_day is None or today - m.last_turn_day >= self.marshal_every(today):
                        if m.theatre["id"] not in self.gw.run(ops.theatres):
                            self.marshal_turn(m, "scheduled")      # reports it gone
                            continue
                        m.last_turn_day = today
                        due.append(m)
                burning = (self.fires() if self.setting("pause_marshals") != "always" and due
                           else set())
                # a fire that flickers (the enemy steps out of a state and back) is not
                # new: it pauses again only after 3 days out
                new = {f for f in burning if today - self.fires_seen.get(f, -99) > 3}
                for f in burning:
                    self.fires_seen[f] = today
                if new:
                    self.journal.write(event="fire", new=sorted(new))
                    print("    .. fire: %s" % "; ".join(sorted(new)), flush=True)
                if not new:
                    # a theatre with no enemy at war on its border takes no scheduled turn
                    # while its inbox is empty (27.10: Slim wrote "peace in India" 360 times)
                    ctx_q = self.ctx()
                    due = [m for m in due if m.inbox or agents.at_war_in(ctx_q, m)]
                    # a quiet group whose picture has not changed skips the day (17.10:
                    # the Westwall's marshal wrote "holding" 40 times, 7000 tokens each)
                    keep = []
                    for m in due:
                        d = self.digest.get(m.theatre["id"])
                        if (d is not None and d == m.last_digest and not m.inbox
                                and today - (m.last_real_turn or -99) < 5):
                            continue
                        m.last_digest, m.last_real_turn = d, today
                        keep.append(m)
                    due = keep
                self.marshal_round(due, "scheduled" if not new else
                                   "NEW FIRE: " + "; ".join(sorted(new)), pause=bool(new) or None)
                from . import economy
                economy.FUEL_POLICY.clear()
                economy.FUEL_POLICY.update(self.planner.plan.get("fuel") or {},
                                           at_war=not self.peace())
                if not self.industry.busy.locked():
                    why = self.gw.run(briefing.industry_alarm)
                    last = self.last_industry
                    # a reason he was already woken for does not wake him again before the
                    # month's review (20.10, 1936: the same unfixable alarm every 3 days, and
                    # each turn he re-planned the research)
                    now, fresh = self.new_reasons("industry", why)
                    if (fresh and (last is None or today - last >= 3)) or \
                            (last is None or today - last >= self.setting("staff_every")):
                        self.last_industry = today
                        self.why_seen["industry"] = now
                        self.spawn(self.industry, briefing.industry, why or "monthly review")
                from . import economy
                self.gw.run(economy.track_fuel, today)
                if self.last_deploy_day != today:
                    # trained divisions go out by code: nothing to decide (18.10)
                    from . import army
                    self.last_deploy_day = today
                    out = self.gw.run(army.deploy_ready, self.mv)
                    if out:
                        self.journal.write(event="deployed", templates=out)
                        self.supreme.inbox.append("New divisions deployed and waiting for "
                                                  "an army: %s" % ", ".join(out))
                if not self.army.busy.locked():
                    from . import army
                    try:
                        why = self.gw.run(army.alarm)
                    except Exception as e:      # one check must not end the campaign
                        print("army alarm failed: %s" % e, flush=True)
                        why = None
                    if not self.peace():
                        # once a game day: it builds each theatre's front (26.10: called every
                        # pass of the loop it held it 25+ s, and the game, running, went weeks
                        # between the scheduler's looks)
                        if getattr(self, "_supply_day", None) != today:
                            try:
                                self._supply_why = briefing.supply_alarm(self.ctx())
                            except Exception:
                                self._supply_why = []
                            self._supply_day = today
                        why = "; ".join(([why] if why else []) + self._supply_why) or None
                    last = self.last_army
                    # 20.10: "106 XP pays for a subdoctrine" woke the staff every 8 days all
                    # winter while the plan said hold the XP for Army Reform
                    now, fresh = self.new_reasons("army", why)
                    if (fresh and (last is None or today - last >= 7)) or \
                            (last is None or today - last >= self.setting("staff_every")):
                        self.last_army = today
                        self.why_seen["army"] = now
                        self.spawn(self.army, briefing.army_staff, why or "monthly review")
                # a theatre's attack begins: the air staff puts fighters and close air
                # support over it at once (24.10: the whole air war is the air staff's).
                # "executing" alone is no attack: a holding theatre executes its plan too
                # (24.10: "attack under way in North Theatre" at peace with the Soviets)
                attacking = {tid for tid, m in self.marshals.items()
                             if m.theatre.get("executing")
                             and m.theatre.get("enemy") in self.at_war}
                if attacking - self.attacks_seen:
                    self.alarms.append(("air", "attack under way in %s: air superiority and "
                                        "close air support over it" % ", ".join(
                                            repr(self.marshals[t].theatre["name"])
                                            for t in sorted(attacking - self.attacks_seen))))
                self.attacks_seen = attacking
                if not self.air_staff.busy.locked():
                    from . import air
                    foes = set(self.gw.run(B_enemies))
                    hold = self.gw.run(self.holders)
                    war_states = {s for s, (_, c) in hold.items() if c in foes}
                    # and ours on the front: the theatres at war
                    for m in self.marshals.values():
                        if foes & set(ops.tags_of(m.theatre.get("enemy") or "")) or \
                                foes & set(agents.theatre_enemies({"planner": self.planner,
                                                                   "enemies": foes},
                                                                  m.theatre)):
                            war_states |= set(m.theatre.get("states") or ())
                    ours = {s for s, (_, c) in hold.items() if c == self.own}
                    why = self.gw.run(air.strategic_alarm, briefing.air_regions(self.mv),
                                      bool(foes), war_states, ours, sorted(foes))
                    last = self.last_air
                    now, fresh = self.new_reasons("air", why)
                    if (fresh and (last is None or today - last >= 7)) or \
                            (last is None or today - last >= self.setting("staff_every")):
                        self.last_air = today
                        self.why_seen["air"] = now
                        self.spawn(self.air_staff, briefing.air_staff, why or "monthly review")
                if not self.navy_lock.locked():
                    # a week at war, a month at peace; a commander alone when the enemy's
                    # warships sail his zone unwatched
                    at_war = bool(self.gw.run(B_enemies))
                    last = self.last_navy
                    if last is None or today - last >= (self.setting("navy_war_every") if at_war
                                                        else self.setting("staff_every")):
                        self.last_navy = today
                        self.navy_round("weekly review at war" if at_war else "monthly review")
                    elif at_war and self.commanders:
                        self.zone_alarm(today)
                if not self.politics.busy.locked():
                    from . import politics
                    why = self.gw.run(politics.alarm, self.politics_pp)
                    if why and "focus" in why and not self.focus_startable(today):
                        why = "; ".join(w for w in why.split("; ") if "focus" not in w) or None
                    last = self.last_politics
                    wait = 1 if why and "focus" in why else 5
                    if (why and (last is None or today - last >= wait)) or \
                            (last is None or today - last >= self.setting("staff_every")):
                        self.last_politics = today
                        self.politics_pp = self.gw.run(lambda g: g.player().political_power)
                        # an empty focus slot loses days: in war too the game waits for
                        # this short turn (21.10: Utilize the Leagues ended mid-September,
                        # the minister's turn came on the 21st behind the marshals' calls)
                        self.spawn(self.politics, briefing.politics, why or "monthly review",
                                   pause=bool(why and "focus" in why))
                self.events_turn()
                self.watch_requests()
                at_war = set(self.gw.run(B_enemies))
                # every enemy an army group stands against, asked for or not (18.10: the
                # supreme never tried declare_war, woke every 30 days, and Poland waited
                # three weeks after the plans had ripened)
                # In peace only a war Le Chonk asked for: an army group standing against a
                # neighbour may be there to defend (19.10: the call made the supreme of a
                # France of 1936 try to declare war on Germany)
                planned = set() if self.peace() else \
                    {m.theatre["enemy"] for m in self.marshals.values()}
                for tag in sorted((self.war_wanted | planned) - at_war - self.ready_told):
                    # a busy supreme would drop the call, and the war would wait forever
                    if not self.supreme.busy.locked() and self.planning(tag, today) == "ready":
                        self.war_wanted.discard(tag)
                        self.ready_told.add(tag)
                        # (19.10: France stands against Germany to defend, not to open a war)
                        self.supreme_turn("the theatres against %s are deployed with their "
                                          "attacks drawn: if opening this war is your plan, "
                                          "it can be opened now (declare_war); if they stand "
                                          "to defend, nothing to do" % tag,
                                          pause=a.pause != "never")
                        last_supreme = today
                fac = self.gw.run(self.our_faction)
                turning = []
                if at_war - enemies_seen:
                    turning.append("war began with %s" % ", ".join(sorted(at_war - enemies_seen)))
                    for t in at_war - enemies_seen:
                        self.war_began.setdefault(t, today)
                for t in set(self.war_began) - at_war:
                    self.war_began.pop(t, None)
                if enemies_seen - at_war:
                    turning.append("war ended with %s" % ", ".join(sorted(enemies_seen - at_war)))
                if fac != self.faction_seen:
                    turning.append("our faction is now %s" % (", ".join(fac) or "none"))
                    self.faction_seen = fac
                for t in turning:
                    self.add_feed("supreme", t.upper()[:1] + t[1:], important=True)
                if turning:
                    # a war with a great power sets the course for years: a deep session
                    # (the user, 20.10: unlimited at the start and when a big war begins)
                    big = sorted((at_war - enemies_seen) & set(planning.MAJORS))
                    self.planner_turn("; ".join(turning), deep=bool(big))
                elif self.plan_asked:
                    why, self.plan_asked = self.plan_asked, None
                    self.planner_turn("the supreme command asks for a new plan: " + why)
                elif today >= self.planner.due_day(today):
                    self.planner_turn("the plan's review is due")
                if at_war - enemies_seen:
                    self.supreme_turn("now at war with %s" % ", ".join(sorted(at_war - enemies_seen)),
                                      pause=a.pause != "never")
                    last_supreme = today
                enemies_seen = self.at_war = at_war
                lost = len(briefing.lost_ground(self.ctx()))
                if lost > lost_seen and today - last_supreme >= 3:
                    last_supreme = today
                    self.supreme_turn("we lost %d state(s) to the enemy" % (lost - lost_seen),
                                      pause=a.pause != "never")
                    lost_seen = lost
                elif lost < lost_seen:
                    lost_seen = lost
                if at_war:
                    self._stalemate(today, at_war, a)
                # in peace the supreme command answers alarms only: the planning session
                # holds the long view (the user, 20.10)
                every = self.setting("supreme_every") if at_war else a.peace_supreme_every
                if every and today - last_supreme >= every:
                    last_supreme = today
                    self.supreme_turn("scheduled review")
        finally:
            self.stopping = True
            self.gw.urgent(ops.set_running, False)
            self.ui.disconnect()
            for t in self.threads:
                t.join(timeout=120)
            print("stopped at %s; journal %s" % (self.gw.run(ops.date)["text"],
                                                 self.journal.path), flush=True)


def B_enemies(g):
    from .bridge import module
    return module("combat").enemy_tags(g)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", choices=sorted(CHEAP) + ["lechonk", "configured"], default="haiku",
                    help="a cheap stand-in for every role (default), or lechonk: Mistral "
                         "Large 4 via Mistral Studio; configured uses BYOK setup")
    ap.add_argument("--marshal-reasoning", default="none",
                    help="the marshals' reasoning on the first step of a turn: none, low, "
                         "medium, high, or a token cap such as 2500 (none suits Large 4, "
                         "which only knows none and high)")
    ap.add_argument("--plan-days", type=int, default=10,
                    help="days of planning with the attacks drawn before a war may open")
    ap.add_argument("--expect-country", default=None, help="Refuse a different loaded country before model calls")
    ap.add_argument("--expect-start-day", default=None, help="Expected calibrated YYYY.MM.DD, for fresh recordings")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--supreme-model", default=None)
    ap.add_argument("--theater-model", default=None, help="field marshals' model")
    ap.add_argument("--days", type=int, default=30, help="stop after this many game days")
    ap.add_argument("--speed", type=int, default=3, help="game speed index 0..4")
    ap.add_argument("--peace-speed", type=int, default=4,
                    help="game speed index while nobody is at war with us")
    ap.add_argument("--peace-supreme-every", type=int, default=0,
                    help="days between the supreme command's scheduled reviews in peace "
                         "(0: none; the planning session holds the long view)")
    ap.add_argument("--marshal-every", type=int, default=7)
    ap.add_argument("--personas", choices=("on", "off"), default="off",
                    help="the field marshals' historical profiles (off: one plain profile)")
    ap.add_argument("--tactics-every", type=int, default=3,
                    help="days between the tactical staff's rounds at war (0: off)")
    ap.add_argument("--war-marshal-every", type=int, default=3,
                    help="at war a marshal's scheduled turn comes at least this often")
    ap.add_argument("--supreme-every", type=int, default=30)
    ap.add_argument("--watch", nargs="*", default=[], help="neighbour tags to describe")
    ap.add_argument("--pause", choices=("fires", "always", "never"), default="fires",
                    help="when turns stop the game: fires (a new war, a lost state, the "
                         "enemy inside our territory, an empty sector; otherwise command "
                         "runs in real time), always, or never")
    ap.add_argument("--maps", choices=("supreme", "all", "none"), default="all",
                    help="who gets the map picture each turn (Small 4 misread it on 17.10)")
    ap.add_argument("--history", choices=("on", "off"), default="on",
                    help="on: the country keeps its historical course (the historical focuses "
                         "and event options; one that leaves it is refused); off: any focus "
                         "and option, by taste")
    ap.add_argument("--rewound", action="store_true",
                    help="with --resume: the game was loaded from an OLDER save of this "
                         "campaign on purpose; accept its earlier date")
    ap.add_argument("--resume", action="store_true",
                    help="restore theatres and notes from logs/campaign.json")
    def on_term(*_):
        # without this a TERM killed the harness mid-pause and the game ran on unwatched
        # (21-25.09 on 17.10); as KeyboardInterrupt it goes through run()'s finally, which
        # pauses the game; a second TERM must not break the wait for the turns in flight
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    args = ap.parse_args()
    from . import personas
    personas.ENABLED = args.personas == "on"
    from . import planner
    planner.HISTORICAL = args.history == "on"
    runner = Runner(args)
    try:
        runner.run()
    finally:
        runner.stopping = True
        runner.ui.disconnect()


if __name__ == "__main__":
    main()
