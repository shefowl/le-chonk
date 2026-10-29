"""Who plays what: the agents the player has handed a branch to.

The user, 23.10: a player may want to play alone and give only the navy to the admiralty,
or only the dull theatres to the marshals. roster.json at the project root says it; it is
read again whenever it changes, so a branch is taken back or handed over while the game
runs. With no file every agent plays, as before.

    {"planner": "on", "supreme": "on", "events": "on", "politics": "on",
     "industry": "on", "army": "on", "air": "on", "admiralty": "on",
     "theatres": "all", "fleets": "all", "ask_war": "off"}

ask_war "on": Le Chonk's declaration of war waits for the player's approval (in the game,
a question from the panel's mod).

Agents are "on" or "off". Theatres and fleets are "all" (every one), "none", or "tagged":
only those whose name in the game starts with AI: (or [AI]); the player renames a theatre or a
fleet to hand it over, and renames it back to take it.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "roster.json")
TAG = "AI:"                    # what the agents name their own (brackets are markup in the game)
TAGS = ("AI:", "[AI]")         # either marks a theatre or fleet as the agents'
AGENTS = ("planner", "supreme", "events", "politics", "industry", "army", "air",
          "admiralty")
DEFAULT = dict({k: "on" for k in AGENTS}, theatres="all", fleets="all", ask_war="off")

# The pace of the run, set from the panel's SETTINGS tab or here; a key left out keeps the
# command line's value (26.10, the user: speeds in peace and in war, how often the agents
# take turns, and whose turns stop the game). (key, low, high, english, russian)
SETTINGS = [
    ("speed_peace", 1, 5, "Game speed in peace", "Скорость игры в мире"),
    ("speed_war", 1, 5, "Game speed at war", "Скорость игры в войне"),
    ("war_marshal_every", 1, 30, "Field marshals in battle: a turn every N days",
     "Маршалы в бою: ход раз в N дней"),
    ("marshal_every", 1, 60, "Field marshals with no battle: every N days",
     "Маршалы без боёв: раз в N дней"),
    ("supreme_every", 3, 90, "Supreme command at war: a review every N days",
     "Ставка в войне: обзор раз в N дней"),
    ("staff_every", 7, 90, "Staffs and ministers: a review every N days",
     "Штабы и министры: обзор раз в N дней"),
    ("navy_war_every", 1, 30, "Admiralty at war: a review every N days",
     "Адмиралтейство в войне: раз в N дней"),
]
BOUNDS = {k: (lo, hi) for k, lo, hi, _, _ in SETTINGS}
# whose turns stop the game: always; in peace and in a land war (not in a war with no
# battle on land); or only on an alarm (a new war, a lost state, a hole in the front)
PAUSE_AGENTS = ("supreme", "events", "politics", "industry", "army", "air", "admiralty",
                "marshals")
PAUSE_MODES = ("always", "battle", "alarms")

_state = {"mtime": None, "data": dict(DEFAULT), "error": None}


def get():
    """The roster now (read again when the file changed)."""
    try:
        mt = os.path.getmtime(PATH)
    except OSError:
        _state.update(mtime=None, data=dict(DEFAULT), error=None)
        return _state["data"]
    if mt != _state["mtime"]:
        _state["mtime"] = mt
        try:
            with open(PATH, encoding="utf-8") as stream:
                raw = json.load(stream)
            data = dict(DEFAULT)
            for k, v in raw.items():
                v = str(v).strip().lower()
                if k in AGENTS and v in ("on", "off"):
                    data[k] = v
                elif k in ("theatres", "fleets") and v in ("all", "none", "tagged"):
                    data[k] = v
                elif k == "ask_war" and v in ("on", "off"):
                    data[k] = v
                elif k in BOUNDS:
                    try:
                        lo, hi = BOUNDS[k]
                        data[k] = min(hi, max(lo, int(float(v))))
                    except ValueError:
                        pass
                elif k.startswith("pause_") and k[6:] in PAUSE_AGENTS and v in PAUSE_MODES:
                    data[k] = v
            _state.update(data=data, error=None)
        except (OSError, ValueError) as e:
            # a file half written by an editor: keep the last good roster
            _state["error"] = str(e)
    return _state["data"]


def on(agent):
    return get().get(agent, "on") == "on"


def tagged(name):
    return str(name or "").strip().upper().startswith(TAGS)


def _ours(kind, name):
    mode = get()[kind]
    return mode == "all" or (mode == "tagged" and tagged(name))


def theatre_ours(name):
    return _ours("theatres", name)


def fleet_ours(name):
    return _ours("fleets", name)


def name_for_new(kind, name):
    """A theatre or fleet the agents open carries the tag when only tagged ones are
    theirs, or it would be the player's the moment it opens."""
    if get()[kind] == "tagged" and not tagged(name):
        return "%s %s" % (TAG, name)
    return name


def describe():
    d = get()
    off = [k for k in AGENTS if d[k] == "off"]
    sets = ", ".join("%s %s" % (k, d[k]) for k in d
                     if k in BOUNDS or k.startswith("pause_"))
    return "agents off: %s; theatres: %s; fleets: %s%s" % (
        ", ".join(off) or "none", d["theatres"], d["fleets"],
        "; settings: " + sets if sets else "")
