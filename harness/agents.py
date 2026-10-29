"""Le Chonk (supreme command) and the field marshals (one a theatre).

An agent turn: a briefing goes in, the model answers with tool calls, each call is
executed through the gateway and its REAL result (or the refusal) goes back to the
model, until it calls end_turn. Code here only routes and records; every military
choice (where, how deep, how many, when to stop, whom to appoint) is the model's.
"""
import collections
import json
import re
import threading
import time

from . import air, doctrine, front, front_threats, ops, zone as zonepic
from .bridge import module as B_module
from .briefing import air_regions as briefing_air_regions
from .llm import LLMOverBudget, LLMOutputError
from .personas import persona

MAX_STEPS = 8
INTEL_MIN_VP = 15      # NDefines INTEL_NETWORK_MIN_VP_TO_TARGET: the start of a network


class Nudge(ops.OpError):
    """A refusal that steers the model (one report per turn) rather than one that says its
    call was wrong."""


MAP_NOTE = """

THE MAP attached shows this situation now (north is up). Province colours: ours blue-grey,
the enemy's red, neutral beige, sea blue. The red line is the front; blue lines are rivers;
dark lines are state borders, labelled "Name (#id)". Black dots are cities with victory
points. Coloured squares are our divisions (number per province), one colour per command as
in the legend; red diamonds are enemy divisions seen. Coloured arrows are the offensives
now ordered, drawn along the provinces the game will push through, with their divisions.
Read the map before you decide: where the enemy is thin or massed, where a river or the
ground stands in the way, where an arrow goes somewhere it should not."""


def tool(name, description, props=None, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": list(required)}}}


INT_LIST = {"type": "array", "items": {"type": "integer"}}
STR_LIST = {"type": "array", "items": {"type": "string"}}

# what the army staff does for the supreme (the user, 18.10: supreme -> strategists ->
# field marshals); the supreme orders it with order_staff
ORG_TOOLS = {"create_army", "split_army", "appoint", "promote_to_field_marshal", "learn_trait",
             "form_army_group", "transfer_army", "reinforce_army"}
# the whole air war is the air staff's (the user, 24.10: air missions were given by Le
# Chonk, the army staff and the air staff alike; ~200 refusals in a week over wings
# another of them, or the admiralty, had ordered)
AIR_TOOLS = {"air_mission", "attach_air_to_army", "rebase_air"}
TACTICAL_MISSIONS = {"air_superiority", "cas", "logistics_strike", "paradrop", "air_supply"}
# training: pilot exercises bank the country's air experience (21.10: it was in no
# staff's list; three years, air XP 0, no design on the new fighter frame, D.510 to 1939)
STRATEGIC_MISSIONS = {"interception", "strategic_bombing", "naval_bombing", "port_strike",
                      "naval_patrol", "air_superiority", "training"}
STATE_LISTS = ("states", "target_state_ids", "state_ids")


# who takes orders from whom: (ctx key of the agent, alarm target)
DEPARTMENTS = {"industry": ("industry", "industry"), "politics": ("politics", "politics"),
               "air staff": ("air_staff", "air"), "admiralty": ("admiralty", "navy")}


def theatre_enemies(ctx, th):
    """The countries a theatre's attacks may go through. At war: those we fight. At peace:
    the theatre's enemy and its whole coalition in the plan's war (24.10: the China
    theatre was refused every arrow for four months, 'cannot reach Beiping through enemy
    ground': Beiping is Hebei-Chahar's, in the plan's war 'CHI+HBC+SHX+SND' but not the
    theatre's enemy CHI, so no attack was drawn before the war)."""
    base = {th["enemy"]} if th.get("enemy") else set()
    plan = getattr(ctx.get("planner"), "plan", None) or {}
    for w in plan.get("wars") or []:
        tags = set(ops.tags_of(w.get("enemy") or ""))
        if base & tags:
            base |= tags
    at_war = set(ctx.get("enemies") or ())
    # at war: only those of the coalition we fight (an arrow into a neutral is refused)
    return (at_war & base) or base


def to_player(ctx, frm, to, order):
    """An order for a branch the player plays (roster.json): it goes to the player."""
    ctx.setdefault("player_requests", []).append("%s asks the %s: %s" % (frm, to, order))
    print("  !! FOR THE PLAYER (%s plays the %s): %s: %s" % ("you", to, frm, order[:300]),
          flush=True)
    return {"delivered": False, "to_player": True,
            "note": "the player runs the %s now: the request went to the player; say it in "
                    "your summary too" % to}


def deliver(ctx, frm, to, order):
    """An order into a department's inbox and notes; it acts on it at once."""
    key, target = DEPARTMENTS[to]
    from . import roster
    if not roster.on(target if target != "navy" else "admiralty"):
        return to_player(ctx, frm, to, order)
    dep = ctx[key]
    dep.inbox.append("%s: %s" % (frm, order))
    dep.memory.append("%s: %s ORDERED: %s" % (ctx.get("date", "?"), frm.upper(), order[:400]))
    ctx["alarms"].append((target, "order from %s" % frm))
    return {"delivered": True, "note": "%s acts on it now" % to}


def regions(ctx):
    """{theatre id: its states now} (ops.territory), once per turn's context."""
    if "_regions" not in ctx:
        reg = ctx["gw"].run(ops.territory, ctx["mv"], ctx["holders"],
                            {tid: m.theatre["states"] for tid, m in ctx["marshals"].items()})
        # an ally's front the theatre took on (hold_ally_front): the ally's states join
        # the theatre's line while the ally stands
        for tid, m in ctx["marshals"].items():
            fw = set(m.theatre.get("forward") or ())
            if fw:
                reg.setdefault(tid, set()).update(
                    st for st, (_, c) in ctx["holders"].items() if c in fw)
        ctx["_regions"] = reg
    return ctx["_regions"]


def facing(ctx, m):
    """The countries the theatre's states touch over land (its line runs along them)."""
    key = ("_facing", m.theatre["id"])
    if key not in ctx:
        ctx[key] = ctx["gw"].run(ops.land_borders, ctx["mv"], ctx["holders"],
                                 regions(ctx).get(m.theatre["id"], m.theatre["states"]),
                                 {m.theatre["enemy"]})
    return ctx[key]


def line_of(ctx, m):
    """The theatre's line as a front spec: 'BEL+GER+LUX'. At war, only those at war with
    us (25.10, n3: the China theatre's front counted neutral Mongolia as the enemy, and
    WEAK POINTS offered the empty Gobi with Ulaanbaatar behind it)."""
    f = facing(ctx, m)
    war = f & set(ctx["enemies"])
    return "+".join(sorted(war or f)) or m.theatre["enemy"]


def at_war_in(ctx, m):
    """The enemies at war with us that the theatre touches."""
    return facing(ctx, m) & set(ctx["enemies"])


def quiet(ctx, m):
    """A theatre facing a country we are at peace with while our war, coming or going on,
    is with others (23.10: Japan's Soviet border drew every division with its holes while
    the war the game was about to start was with China)."""
    focus = set(ctx["enemies"]) | set(ctx.get("coming_wars") or ()) | set(
        ctx.get("war_wanted") or ())
    tag = m.theatre["enemy"]
    return bool(focus) and tag not in focus


def foes(ctx):
    """The enemies the theatres expect, and those at war with us."""
    return front_threats.expected(ctx)


def find_theatre(ctx, x):
    """The field marshal of a theatre named by id (474) or by name."""
    ms = ctx["marshals"]
    try:
        if int(x) in ms:
            return ms[int(x)]
    except (TypeError, ValueError):
        pass
    for m in ms.values():
        if str(x).strip().lower() == m.theatre["name"].lower():
            return m
    raise ops.OpError("no theatre %r; ours: %s" % (x, ", ".join(
        "%d %s" % (tid, m.theatre["name"]) for tid, m in ms.items()) or "none"))


def _province_id(x):
    """A province as the model wrote it: 6521, "6521" or "p6521 Kraków"."""
    if isinstance(x, int):
        return x
    m = re.match(r"\s*p?\s*(\d+)", str(x))
    if not m:
        raise ops.OpError("%r is not a province; give it as in YOUR FRONT, p6521" % (x,))
    return int(m.group(1))


def _state_id(x):
    """A state id as the model wrote it: 74, "74", or the briefing's "Sudeten Silesia (#74)"
    (17.10: Le Chonk copied the names from the border line, and the group was made before
    the front failed on them)."""
    if isinstance(x, int):
        return x
    m = re.search(r"#\s*(\d+)", str(x)) or re.fullmatch(r"\s*(\d+)\s*", str(x))
    if not m:
        raise ops.OpError("%r is not a state id; give the number, as in (#74)" % (x,))
    return int(m.group(1))


def normalize_args(args):
    for k in STATE_LISTS:
        if isinstance(args.get(k), list):
            args[k] = [_state_id(x) for x in args[k]]
    return args


def split_content(content):
    """(reasoning, text) of a reply. Mistral's reasoning models return content as a list
    of chunks, with the reasoning in "thinking" chunks; others return a plain string."""
    if not isinstance(content, list):
        return None, content
    thinking, text = [], []
    for ch in content:
        if ch.get("type") == "thinking":
            thinking += [t.get("text", "") for t in ch.get("thinking") or []]
        elif ch.get("type") == "text":
            text.append(ch.get("text", ""))
    return "\n".join(thinking) or None, "\n".join(text) or None


class Journal:
    """Append-only record of every proposal, execution and refusal."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, **rec):
        rec["t"] = round(time.time(), 3)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


class Agent:
    role = "agent"

    max_tokens = 2048
    reasoning = None            # "high" for the supreme command
    budget_s = 90               # wall clock per step; see turn()
    think_s = None              # wall clock for the reasoning step, if shorter
    max_steps = None            # model calls per turn; MAX_STEPS if None
    reason_every_step = False   # deep reasoning after each tool result too (the planner)

    def __init__(self, name, model, llm, journal, system):
        self.name, self.model, self.llm, self.journal = name, model, llm, journal
        self.system = system
        self.memory = []            # short notes the agent carries between turns
        self.inbox = []             # messages from other agents since the last turn
        self.busy = threading.Lock()

    def tools(self):
        raise NotImplementedError

    def handle(self, ctx, name, args):
        raise NotImplementedError

    def _act_now(self, msgs, partial, note=None):
        note = note or "Time is up: act now with the tools, without further deliberation."
        if partial:
            note += "\n\nYOUR REASONING SO FAR (cut off):\n" + partial[-8000:]
        if msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list):
            msgs[-1] = dict(msgs[-1], content=msgs[-1]["content"] +
                            [{"type": "text", "text": note}])
        elif msgs[-1]["role"] == "user":
            msgs[-1] = dict(msgs[-1], content=msgs[-1]["content"] + "\n\n" + note)
        else:
            msgs.append({"role": "user", "content": note})
        return self.llm.chat(self.model, msgs, tools=self.tools(), tool_choice="any",
                             max_tokens=4000, role=self.name,
                             reasoning="none" if self.reasoning else None,
                             budget_s=self.budget_s)

    def picture(self, ctx):
        """A map for this turn (PNG bytes), or None."""
        return None

    def before_end(self, ctx):
        """Something the commander must answer before the turn closes, or None."""
        return None

    def step_max_tokens(self, step):
        """The answer's cap on this step (reasoning included)."""
        return self.max_tokens

    def step_reasoning(self, step):
        """How this step reasons: the agent's own setting on the first step (or on every
        step), none after it."""
        if step == 0 or self.reason_every_step:
            return self.reasoning
        return "none" if self.reasoning else None

    def turn(self, ctx, briefing, picture=None):
        msgs = [{"role": "system", "content": self.system}]
        notes = "\n".join("- " + m for m in self.memory[-12:]) or "(none yet)"
        inbox = "\n".join("- " + m for m in self.inbox) or "(none)"
        self.inbox = []
        text = ("%s\n\nYOUR NOTES FROM EARLIER TURNS:\n%s\n\nMESSAGES:\n%s"
                % (briefing, notes, inbox))
        if picture:
            from .mapimage import data_url
            content = [{"type": "text", "text": text + MAP_NOTE},
                       {"type": "image_url", "image_url": data_url(picture)}]
        else:
            content = text
        msgs.append({"role": "user", "content": content})
        summary = None
        refused = {}
        nudged = checked = hollow = False
        held_ends = 0            # end_turns held for a question, at most two a turn
        self.reported = False
        self.requested = False
        self.plan_nudged = False
        self.refused_nudged = False
        self.danger_nudged = False
        self.messaged = set()
        self.this_turn = set()          # once-a-turn orders already given
        last_step = (self.max_steps or MAX_STEPS) - 1
        for step in range(last_step + 1):
            # Deep reasoning on the first step only, where the situation is analysed; the
            # later steps react to tool results (Large 4 reasoned 4 minutes per step when
            # Mistral was slow, and a turn has up to MAX_STEPS of them).
            reasoning = self.step_reasoning(step)
            try:
                msg, usage, dt = self.llm.chat(self.model, msgs, tools=self.tools(),
                                               tool_choice="any" if step == 0 else "auto",
                                               max_tokens=self.step_max_tokens(step),
                                               role=self.name,
                                               reasoning=reasoning,
                                               budget_s=(self.think_s or self.budget_s)
                                               if step == 0 and self.reasoning else self.budget_s)
            except LLMOverBudget as e:
                # The game waits on this turn. Same question once more, without the long
                # reasoning, and the model is told why.
                self.journal.write(agent=self.name, step=step, date=ctx.get("date"),
                                   over_budget=str(e))
                print("    .. %s over budget, asking again without reasoning" % self.name,
                      flush=True)
                msg, usage, dt = self._act_now(msgs, e.partial)
            except LLMOutputError as exc:
                self.journal.write(agent=self.name, step=step, date=ctx.get("date"),
                                   invalid_output=str(exc))
                msg, usage, dt = self._act_now(msgs, None, note=
                    "Your previous StructuredOutput reply was invalid; no orders were "
                    "executed. Retry once with separate JSON fields thought and calls. "
                    "calls must be a JSON array, never XML inside thought. Give only "
                    "the essential orders and end_turn.")
            if not msg.get("tool_calls") and not split_content(msg.get("content"))[1]:
                # Reasoning only, cut off by max_tokens (seen: Large 4 spent all 12000
                # tokens of the opening turn thinking and called nothing).
                thinking = split_content(msg.get("content"))[0]
                self.journal.write(agent=self.name, step=step, date=ctx.get("date"),
                                   reasoning_only=len(thinking or ""))
                print("    .. %s reasoned without acting, asking again without reasoning"
                      % self.name, flush=True)
                msg, usage, dt = self._act_now(msgs, thinking)
            refresh = ctx.get("before_orders")
            refreshes = 0
            while refresh and (fresh := refresh()):
                if refreshes >= 2:
                    raise ops.OpError("the situation kept changing during the paused retry")
                # This call is extra even on the last normal step: no stale order is
                # executed, and the refreshed briefing always reaches the model.
                msgs.append({"role": "user", "content":
                             "The situation changed while you answered. Your last reply's "
                             "orders were NOT executed. Re-evaluate from this current "
                             "briefing, then act and end_turn:\n" + fresh})
                msg, usage, retry_s = self._act_now(msgs, None,
                    note="Use the refreshed briefing above. Decide without further deliberation.")
                dt += retry_s
                refreshes += 1
            msgs.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            calls = msg.get("tool_calls") or []
            thinking, text = split_content(msg.get("content"))
            self.journal.write(agent=self.name, step=step, date=ctx.get("date"),
                               latency_s=round(dt, 2), thought=text, reasoning=thinking,
                               calls=[c["function"] for c in calls])
            if not calls:
                if text and not nudged and step < last_step:
                    # 17.10: Bock wrote "reject this route, take that one" and stopped; the
                    # analysis was there, the orders never came
                    nudged = True
                    msgs.append({"role": "user", "content":
                                 "You have analysed but ordered nothing. Give the orders you "
                                 "decided on now with the tools, then end_turn."})
                    continue
                break
            done = False
            asked = False           # a call of this step was answered with a question
            batch_failures = []      # results the model has not seen yet
            for c in calls:
                fn = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                if (fn == "end_turn" and step == 0 and not hollow and len(calls) == 1
                        and len((args.get("summary") or "").strip()) < 40):
                    # 20.10: industry woke for an order and closed at once with the
                    # summary "Placeholder"; the order was never read
                    hollow = True
                    result = {"ok": False, "refused":
                              "you closed the turn before doing anything, with no account of "
                              "it. Read why you were called (the first line of the briefing) "
                              "and act on it; if nothing is needed, end_turn again and say "
                              "in a sentence why."}
                elif fn == "end_turn" and asked and held_ends < 2:
                    held_ends += 1
                    # 23.10: the army staff sent create_army and end_turn together for three
                    # months; the refusal asked "send it again if meant" and was never read,
                    # and 17 divisions stood in Japan in no army
                    result = {"ok": False, "refused":
                              "a call above was answered with a question (refused, 'send it "
                              "again if...'): read it and decide, then end_turn"}
                elif fn == "end_turn" and batch_failures and held_ends < 2:
                    held_ends += 1
                    result = {"ok": False, "refused":
                              "Calls in this same batch failed or had unconfirmed effects: " +
                              ", ".join(batch_failures) +
                              ". Read their results before saving your summary. Do not "
                              "report failed orders as completed; adapt or end_turn again "
                              "with what actually happened."}
                elif fn == "end_turn" and not checked and (why_not := self.before_end(ctx)):
                    # asked once: a check that remembers it asked says nothing the second time
                    checked = True
                    result = {"ok": False, "refused": why_not}
                elif fn == "end_turn":
                    summary = args.get("summary", "")
                    result = {"ok": True}
                    done = True
                else:
                    try:
                        result = {"ok": True, "result": self.handle(ctx, fn, normalize_args(args))}
                        self.this_turn.add(("done", fn, str((args or {}).get("army"))))
                    except Nudge as e:
                        # advice, not a mistake: it must not use up the refusal limit
                        # (17.10: Bock's fourth refused preview ended his turn just as he
                        # was about to order the attacks)
                        result = {"ok": False, "refused": str(e)}
                        # only a refusal that asks for a decision holds end_turn (23.10:
                        # "one report per turn" held the army staff's end_turn ten times)
                        asked = asked or "again" in str(e)
                    except ops.OpError as e:
                        result = {"ok": False, "refused": str(e)}
                        self.this_turn.add(("refused", fn, str((args or {}).get("army"))))
                        key = (fn, json.dumps(args, sort_keys=True))
                        refused[key] = refused.get(key, 0) + 1
                        if refused[key] >= 2:
                            # seen: Rundstedt sent the same refused call six times running
                            result["note"] = ("this exact call has now been refused %d times; "
                                              "it will not work as it is. Re-read the "
                                              "status above and do something else, or "
                                              "end_turn." % refused[key])
                        if refused[key] >= 3 or sum(refused.values()) >= 6:
                            # 17.10: Heinrici cycled three different refused moves, nine
                            # refusals in one turn; the per-call count never fired in time
                            done = True
                    except Exception as e:          # bridge failure, not a model error
                        result = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
                payload = result.get("result")
                if fn != "end_turn" and (not result.get("ok") or
                        isinstance(payload, dict) and payload.get("ok") is False):
                    batch_failures.append(fn)
                self.journal.write(agent=self.name, date=ctx.get("date"), action=fn,
                                   args=args, outcome=result)
                msgs.append({"role": "tool", "tool_call_id": c["id"], "name": fn,
                             "content": json.dumps(result, ensure_ascii=False, default=str)})
            if done:
                break
        if summary:
            self.memory.append("%s: %s" % (ctx.get("date", "?"), summary))
        return summary


# ====================================================================== field marshal
# the country we play, read from the game when the run starts (run.py); the harness
# serves any country (the user, 18.10: universal code for any country)
COUNTRY = {"name": "our country", "tag": None}

MARSHAL_SYSTEM = """You are {name}, a field marshal of {country} in Hearts of Iron IV. You command a
THEATRE for the supreme commander, Le Chonk: a stretch of the map with one military sense,
and every army in it, each under its general (some of them grouped into army groups).

{profile}

How your orders work. YOUR LINE is the whole border of your theatre, province by province:
along every country its states touch, the enemy and the neutrals beside him alike. You
cut it among your armies (set_sectors: the armies in order along the line, each with a
weight); each army holds a SECTOR, its stretch, its divisions stand on it, and the game's
battleplanner moves and fights them. Several armies may share one state; each needs at
least 2 provinces of the line, the rest stand in reserve as the second echelon.
You decide the cut, which army attacks where and how deep (attack), when the plan runs
(execute), and what to tell Le Chonk.
HOW DIVISIONS MOVE (the game's rule): a division's place changes ONLY by the front: its
army's sector (set_sectors, the weights), a transfer between armies, or hold_line. The game
puts every division back on its army's front line, so you give no hand moves: to thicken a
stretch, cut it shorter or give it more divisions; to empty one, the reverse.
YOUR TACTICAL STAFF acts every few days by itself, at war, and only attacks: it
strikes the thinnest enemy point beside every army in
POSTURE attack from all sides at once (one division stays in each province, a province in
danger gives none; ground, rivers, forts and combat width weighed for each kind of
division), and throws the nearest free division, the fast first, into empty enemy
provinces. Its last orders are in your briefing. You set each army's posture (set_posture:
attack, hold, reserve; at war every army attacks until you say otherwise) and the big
moves: the stretches, the deep arrows, the landings, the reserve. Where Le Chonk orders
a stretch to hold, set its armies to hold. Armies come into and leave your theatre only by Le
Chonk's decision: ask him (report_to_supreme, request) for more.
ARMY HQ. An army whose general is deployed with an HQ (deploy_hq) plans faster and to
a higher cap (without one both are x0.8). Deploy it for every army that holds a stretch
of the front or prepares an attack or a landing, first for the main effort; it takes a
little equipment and manpower from the army and a few days. An army in reserve far from
the front does not need one (the general goes home after a week with no front order).

SHOCK ARMY AND STABLE FRONT. You win by concentration, not by an even spread: the main
effort is one army (or two side by side) on a SHORT stretch facing the objective, as
many divisions as the supply there feeds (SUPPLY ROOM: the second number is the cap; more
starve them all), its arrow on the thinnest point toward his cities and hubs. Every other
army holds a LONG stretch thinly. The odds are counted per direction, never over the
whole theatre: the balance of the theatre may be against us while the shock army's arrow
wins. THE GAME RATES EACH ARMY'S PLAN (YOUR ARMIES: GREEN, YELLOW, RED, with the two
sides' strength as the game counts it). A RED plan is never set running: execute skips
it and the staff deals no blows from it. Make it at least yellow: shorten the shock
army's stretch, give it divisions from the quiet stretches, aim the arrow at a weaker
point. If no arrow can be made yellow with what the supply feeds, the front cannot be
broken head on: tell Le Chonk (report_to_supreme) so that he turns it (a landing behind
it, paratroopers, another axis). Enemy hubs taken raise the supply cap.
- YOUR ARMIES in the briefing shows each army: its divisions, its sector, whether they
  stand on it, its attack, and its TARGETS: the enemy states it can reach from its line
  (p = provinces away), with ground, enemy divisions and cities. Pick objectives there.
- ALERTS name what is wrong in YOUR theatre: HOLE (a border state where a neighbour can
  walk in), POCKET, IDLE (at war, an army without an attack), NO SECTOR. A HOLE comes first.
- YOUR THEATRE PLAN is written in a planning session (the briefing says when one is
  due: no plan yet, a new directive, the war changed, an ally about to capitulate, enemy
  in our land, a month gone, or you asked with ask_replan). There you think the whole
  theatre through, with YOUR CRAFT at the end of the briefing, and carry the plan out.
- On an ordinary turn PLAN CHECK says where the line stands against the plan: act on
  what it names, or ask_replan when the plan no longer fits. Sectors and attacks stay in
  force from turn to turn; if nothing is wrong, call end_turn.
- Use state ids as given (the number after #). Each tool returns what really happened;
  if refused, read why and adapt. Do not claim in end_turn what you did not order.
- Finish every turn with end_turn: one or two sentences of intent, shown to you next turn.
"""


class Marshal(Agent):
    role = "marshal"
    max_tokens = 8000            # with reasoning: 2048 was spent thinking, nothing ordered
    # Large 4 since 17.10 (Small 4 was for price and speed, and misread the map); no
    # deliberation: a marshal acts every game day and the game does not wait for him
    reasoning = "none"

    def __init__(self, theatre, general_key, general_name, model, llm, journal):
        name, profile = persona(general_key, general_name)
        # the craft (doctrine.MARSHAL) is read in the planning sessions, not every turn
        super().__init__(name, model, llm, journal,
                         MARSHAL_SYSTEM.format(name=name, profile=profile,
                                               country=COUNTRY["name"]))
        self.planning = False            # this turn is a theatre planning session
        self.replan_asked = None         # why the marshal asked for a new plan
        # dict: id (the game's theatre), name, states (given by Le Chonk), enemy (the one
        # he expects there), directive, executing, set_day
        self.theatre = theatre
        self.last_digest = self.last_real_turn = None
        self.sector_changed = {}         # army id -> the day its sector last changed
        self.general_key = general_key
        self.last_divisions = None       # for "losses since your last turn"
        self.last_turn_day = None
        self.requested = False           # a request went to Le Chonk this turn
        self.short_asked = None          # the day we last asked for divisions

    def region(self, ctx):
        return regions(ctx).get(self.theatre["id"], set(self.theatre["states"]))

    def picture(self, ctx):
        from . import mapimage
        th = self.theatre
        war = at_war_in(ctx, self)
        return ctx["gw"].run(mapimage.front_map, ctx["mv"], ctx["holders"], th["id"],
                             self.region(ctx), "%s (theatre %d)" % (th["name"], th["id"]),
                             set(ops.tags_of(line_of(ctx, self))) if war else set(),
                             "%s-%s.png" % (ctx["date"], self.name.replace(" ", "_")))

    def tools(self):
        return [
            tool("hold_ally_front", "Take on an ally's front with the enemy (ALLY in the "
                 "briefing): its states join YOUR LINE and set_sectors cuts it among your "
                 "armies with the rest; your divisions march into the ally's land. allies: the "
                 "allies whose fronts you hold, [] to hold none but our own border. Cut the "
                 "line anew after it (set_sectors).",
                 {"allies": STR_LIST}, ["allies"]),
            tool("ship_by_sea", "Ship the divisions of one of your armies that stand "
                 "across the sea (islands, the home islands) to its sector by to_state (a "
                 "state of ours there): the game carries them from port to port and they "
                 "march to the line; the army needs a sector first. At peace, or "
                 "against an enemy with no ships, freely; at war with a navy, only routes "
                 "with no enemy ships on them. divisions: how many (default all that cannot walk there).",
                 {"army": {"type": "integer"}, "to_state": {"type": "integer"},
                  "divisions": {"type": "integer"}}, ["army", "to_state"]),
            tool("set_sectors", "Cut YOUR LINE among your armies. List them in order along "
                 "the line from its FIRST end (as YOUR LINE reads); each takes a contiguous "
                 "stretch as long as its weight (default: its divisions), province by "
                 "province, with no gap and no overlap. Armies you leave out stand in "
                 "reserve. An army whose stretch stays the same keeps its attack and "
                 "planning; one whose stretch changes loses them and its divisions march. "
                 "List each army once. Optional states are WEIGHT HINTS: their border "
                 "province count sets the length, not an exact geographical assignment. "
                 "The ordered rows determine geography; read actual states in the result.",
                 {"armies": {"type": "array", "items": {"type": "object", "properties": {
                     "army": {"type": "integer"}, "weight": {"type": "number", "exclusiveMinimum": 0},
                     "states": INT_LIST},
                     "required": ["army"]}},
                  "marines_on_line": {"type": "boolean", "description": "true to put a "
                                      "landing force (marines) on the line all the same"},
                  "drop_attacks": {"type": "boolean", "description": "true to cut even "
                                   "when an army with attacks drawn gets a new stretch "
                                   "(its arrow is drawn again on the new stretch; the "
                                   "planning bonus is lost)"}},
                 ["armies"]),
            tool("set_theatre_plan", "Write YOUR THEATRE PLAN (in a planning session): the "
                 "threats in order with their evidence; the layout along YOUR LINE, each army "
                 "with its states and the divisions a province it should hold there "
                 "(per_province: thin behind high forts, thick on the likeliest axis); the "
                 "reserve (which army, behind which axis, when it counter-attacks); the "
                 "fallback line; and what would make you plan again. Then carry it out: "
                 "set_sectors in line order; states there are weight hints, so compare "
                 "the returned actual states with this layout.",
                 {"threats": {"type": "string"},
                  "layout": {"type": "array", "items": {"type": "object", "properties": {
                      "army": {"type": "integer"}, "states": INT_LIST,
                      "per_province": {"type": "number"}},
                      "required": ["army", "states", "per_province"]}},
                  "reserve": {"type": "string"}, "fallback": {"type": "string"},
                  "replan_when": {"type": "string"}},
                 ["threats", "layout", "reserve", "replan_when"]),
            tool("ask_replan", "Ask for a theatre planning session at your next turn, when the "
                 "plan no longer fits what you see.", {"why": {"type": "string"}}, ["why"]),
            tool("attack", "An army's attack: one arrow from its sector to an OBJECTIVE, an "
                 "enemy state from its TARGETS (aimed at its main city). via_states bend the "
                 "route through those states in order; then_states chain further arrows from "
                 "the tip, so the army drives on by itself. Replaces the army's attack; the "
                 "result gives the route as the game drew it.",
                 {"army": {"type": "integer"}, "objective_state": {"type": "integer"},
                  "via_states": INT_LIST, "then_states": INT_LIST},
                 ["army", "objective_state"]),
            tool("pursue", "When HIS FRONT HAS BROKEN: a SPEARHEAD for one army to the far "
                 "end of the country (THE FAR END in YOUR FRONT): its divisions race ahead "
                 "and grab everything in sight, without keeping a line. A spearhead gets NO "
                 "planning bonus, so it needs no ripening: draw it and run the plan at once "
                 "(it starts by itself). Made for a broken front and empty land; against a "
                 "held line it is the wrong tool. objectives: states, the farthest LAST "
                 "(nearer ones are fallbacks). For the armour and the motorised armies, each "
                 "its own far end; infantry follows with attack.",
                {"army": {"type": "integer"}, "objectives": INT_LIST},
                 ["army", "objectives"]),
            tool("set_posture", "An army's POSTURE for your tactical staff, which acts every "
                 "few days by itself: attack (its free divisions strike the thinnest enemy "
                 "point beside them from every side, one division staying in each province, "
                 "and grab empty enemy provinces, the fast first), hold (the staff leaves it "
                 "on its line: a stretch Le Chonk orders held), reserve (the staff leaves it "
                 "alone). At war every army attacks until you say otherwise.",
                 {"army": {"type": "integer"},
                  "posture": {"type": "string", "enum": ["attack", "hold", "reserve"]}},
                 ["army", "posture"]),
            tool("set_auto_cover", "Your standing order AUTO-COVER (on unless you turn it "
                 "off): each day a stretch of your line facing the enemy that no army's sector "
                 "holds (a liberated or conquered country's new border, a front that moved) "
                 "goes to the nearest army that is not attacking, as an extra sector; no other "
                 "sector or attack is touched, and the plan runs or stands as you set it.",
                 {"on": {"type": "boolean"}}, ["on"]),
            tool("cancel_attack", "Remove an army's attack; its divisions hold its sector.",
                 {"army": {"type": "integer"}}, ["army"]),
            tool("hold_line", "A DEFENCE LINE for one army instead of its stretch of the "
                 "border: the army leaves the border and holds a line of ours. Give the "
                 "provinces along it in order (p<id>, like drawing the line), or states: "
                 "then their side facing the enemy is the line. What lies in front is "
                 "given up by choice: no hole is reported there. Draw it ON the forts and "
                 "rivers, never behind our own forts. set_sectors with the army puts it "
                 "back on the border.",
                 {"army": {"type": "integer"}, "provinces": STR_LIST,
                  "state_ids": INT_LIST}, ["army"]),
            tool("guard_points", "SECTOR DEFENCE for one army instead of a line: it "
                 "garrisons chosen points of the given states of ours: ports, cities, "
                 "forts, coast, airfields, or provinces you name (p<id>). Against landings: "
                 "garrisons in the ports. The divisions are spread over the points.",
                 {"army": {"type": "integer"}, "state_ids": INT_LIST,
                  "kinds": {"type": "array", "items": {"type": "string", "enum": [
                      "ports", "cities", "forts", "coast", "airfields"]}},
                  "provinces": STR_LIST}, ["army", "state_ids"]),
            tool("deploy_hq", "Deploy an army's general with an army HQ (withdraw=true "
                 "takes it back). Without one the army plans slower and to a lower cap "
                 "(x0.8); it takes a little equipment and manpower from the army's "
                 "divisions; the general withdraws by himself after a week with no front "
                 "or landing order.", {"army": {"type": "integer"},
                                       "withdraw": {"type": "boolean"}}, ["army"]),
            tool("execute", "Run (true) or stop (false) your theatre's battle plan. "
                 "An army whose plan the game rates RED is not set running.",
                 {"run": {"type": "boolean"}}, ["run"]),
            tool("naval_invasion", "Plan a landing: one of your armies (not one holding a "
                 "stretch of the line: its whole plan runs with it) sails from our naval "
                 "base (OUR PORTS, province) to enemy coastal provinces. It can be drawn at "
                 "peace; launch_operation sets it off at war. It needs naval dominance on "
                 "the way (the admiralty sees it) and convoys.",
                 {"army": {"type": "integer"}, "from_port": {"type": "integer"},
                  "targets": {"type": "array", "items": {"type": "integer"}}},
                 ["army", "from_port", "targets"]),
            tool("land", "A LANDING BY THE NORM on an enemy port (LANDING TARGETS): the marines "
                 "of the armies you name sail into the port itself (up to 10; 4 fill a full "
                 "combat width; what there is), their other divisions onto the beaches beside "
                 "it and attack the port from the land. Sails from our nearest port unless "
                 "from_port is given. Armies off the line (a landing army). Then "
                 "launch_operation each operation it returns; the game sails it when the "
                 "admiralty holds the sea.",
                 {"port": {"type": "integer"}, "armies": INT_LIST,
                  "from_port": {"type": "integer"}}, ["port", "armies"]),
            tool("paradrop", "Plan a paradrop: an army of paratroopers flies from our air "
                 "base (a province of its state) onto enemy provinces; needs transport "
                 "planes and air superiority. launch_operation sets it off.",
                 {"army": {"type": "integer"}, "from_province": {"type": "integer"},
                  "targets": {"type": "array", "items": {"type": "integer"}}},
                 ["army", "from_province", "targets"]),
            tool("launch_operation", "Launch (or hold back) a planned landing or paradrop "
                 "(OPERATIONS).", {"army": {"type": "integer"},
                                   "operation": {"type": "integer"},
                                   "run": {"type": "boolean"}}, ["army", "operation"]),
            tool("cancel_operation", "Cancel a planned landing or paradrop.",
                 {"army": {"type": "integer"}, "operation": {"type": "integer"}},
                 ["army", "operation"]),
            tool("report_to_supreme", "One report to Le Chonk per turn. Use request only when "
                 "you need something you cannot do yourself (armies, a change of directive).",
                 {"message": {"type": "string"}, "request": {"type": "string"}}, ["message"]),
            tool("end_turn", "Finish this turn.", {"summary": {"type": "string"}}, ["summary"]),
        ]

    def alerts(self, ctx):
        # every theatre at once: a hole is its own theatre's, not each marshal's (18.10:
        # every marshal was told the whole Polish border was his and Heinrici stretched one
        # army over seven states)
        sit = ctx["gw"].run(ops.situation, ctx["mv"], ctx["holders"], regions(ctx),
                            set(ctx["enemies"]), foes(ctx))
        return sit.get(self.theatre["id"], [])

    def plan_due(self, ctx):
        """Why this turn is a theatre planning session, or None."""
        th, gw, mv = self.theatre, ctx["gw"], ctx["mv"]
        pl = th.get("plan")
        if not pl:
            return "your theatre has no plan yet"
        if self.replan_asked:
            return "you asked for it: %s" % self.replan_asked
        if pl.get("directive") != th["directive"]:
            return "Le Chonk's directive changed"
        day = ctx.get("day") or 0
        if pl.get("day") is not None and day - pl["day"] >= 30:
            return "the monthly review (the plan is of %s)" % pl.get("date")
        war = sorted(at_war_in(ctx, self))
        if war != pl.get("at_war", []):
            return "the war changed: at war here with %s" % (", ".join(war) or "nobody")
        try:
            fall = set(gw.run(ops.falling_allies, mv, ctx["holders"], set(ctx["enemies"])))
        except Exception:
            fall = set()
        new = fall - set(pl.get("falling", []))
        if new:
            return "%s is about to capitulate" % ", ".join(sorted(new))
        lost = gw.run(lambda g: sum(1 for st in th["states"] for q in mv.land_provinces(st)
                                    if ops._ctl(g)(q) in set(ctx["enemies"])))
        if lost > pl.get("lost", 0):
            return "the enemy took %d provinces of your theatre's land since the plan" % (
                lost - pl.get("lost", 0))
        return None

    DANGER_RATIO, DANGER_THIN = 3.0, 2

    def _danger(self, ctx):
        """Provinces of our line held by at most DANGER_THIN divisions against an enemy
        attack of DANGER_RATIO or more, with the army there and its divisions elsewhere."""
        if not at_war_in(ctx, self):
            return []
        try:
            fr = front.of(ctx, line_of(ctx, self))
            zone = ctx["gw"].run(front.group_zone, fr, self.theatre["id"])
        except Exception:
            return []
        out = []
        for a in zone:
            att, dfn, src = fr.threat_ratio(a)
            n = fr.n_div[("us", a)]
            if att <= 0 or n > self.DANGER_THIN or (dfn and att / dfn < self.DANGER_RATIO):
                continue
            arm = collections.Counter(fr.div_army.get(u[3]) for u in fr.units.get(a, ())
                                      if u[0] == "us")
            arm.pop(None, None)
            where = ""
            if arm:
                aid = arm.most_common(1)[0][0]
                total = sum(1 for d, x in fr.div_army.items() if x == aid)
                where = ", army %s (%d divisions in all)" % (aid, total)
            out.append((att / dfn if dfn else 99, "%s at %s held by %d division(s)%s, his %d "
                        "next to it (%s)" % (front._p(fr.mv, a), front._fmt(att / dfn if dfn
                                                                           else float("inf")),
                                             n, where, fr.divisions("them", src),
                                             front.ours_of(fr.worth("them", src)))))
        return [t for _, t in sorted(out, reverse=True)[:3]]

    def before_end(self, ctx):
        # only a hole stops the turn (17.10: the check fired on every alert, 201 times,
        # and the marshals learnt to write "left on purpose")
        try:
            holes = [t for k, _, t in self.alerts(ctx) if k == "HOLE"]
        except ops.OpError:
            return None
        every = holes
        if not at_war_in(ctx, self):
            # Ordinary peacetime marches need no repeated review. An incoming
            # war warning makes physical border readiness relevant before war too.
            incoming = {t["from"] for t in ctx.get("war_threats", {}).get("threats", [])
                        if t["target"] == ctx["own"]}
            if not incoming & facing(ctx, self):
                holes = [t for t in holes if "on no army's sector" in t]
        if holes:
            # an enemy inside is not closed by a cut (23.10: 13 HBC divisions sat in East
            # Hebei for a month; Kan'in, told "no attack before June", re-cut and waited)
            inside = any("enemy divisions inside" in t for t in holes)
            return ("Not yet: " + "; ".join(holes) + ". Close it now (" + (
                "the enemy inside our land is thrown out by a counter-attack: attack that "
                "state with the army beside it and execute; retaking our own land is "
                "defence, allowed under any hold or no-offensive directive; " if inside
                else "") + "set_sectors: cut YOUR LINE anew with your armies on it), or "
                "call end_turn again and say why not.")
        # an arrow refused and never drawn after (24.10: Kan'in's arrow to Jinan was
        # refused, and his end_turn said it was drawn)
        lost = sorted({x[2] for x in self.this_turn if isinstance(x, tuple)
                       and x[:2] == ("refused", "attack")} -
                      {x[2] for x in self.this_turn if isinstance(x, tuple)
                       and x[:2] == ("done", "attack")})
        if lost and not getattr(self, "refused_nudged", False):
            self.refused_nudged = True
            return ("The attack of army %s was refused and none was drawn after it: that "
                    "army has no arrow now. Draw it again as the refusal advises (a nearer "
                    "objective from its TARGETS, deeper with then_states), or call end_turn "
                    "again and say so; do not report it as drawn." % ", ".join(lost))
        danger = self._danger(ctx) if not getattr(self, "danger_nudged", False) else []
        if danger:
            # 25.10, n3: Tongzhou stood at 13.8:1 on two divisions of 486 for weeks while
            # 15 of its 19 stood in Chahar; Kan'in wrote "watching" every turn
            self.danger_nudged = True
            return ("Not yet: " + "; ".join(danger) + ". A province held this thin against "
                    "this much falls, and the land behind it with it. Thicken it by the line: give that stretch an army of "
                    "its own (set_sectors: an army whose states are only that state, its "
                    "weight to match the threat; one army over two separate stretches lets "
                    "the game strip one of them), or move divisions into the army that holds "
                    "it, or hold_line on a shorter line behind it. Or call end_turn again and "
                    "say why not.")
        if self.planning and "set_theatre_plan" not in self.this_turn \
                and not getattr(self, "plan_nudged", False):
            self.plan_nudged = True
            return ("This turn is a theatre planning session: write YOUR THEATRE PLAN "
                    "(set_theatre_plan) and carry it out (set_sectors with its states), or "
                    "call end_turn again and say why not.")
        day = ctx.get("day") or 0
        # holes the armies cannot close: the theatre is short of divisions, and only Le
        # Chonk can give more (21.10: Besson reported six empty Savoy passes for weeks,
        # Italian columns massing, and never asked). Once in 14 days without a request.
        if every and not self.requested and not quiet(ctx, self) \
                and day - (self.short_asked or -999) >= 14:
            self.short_asked = day
            return ("Holes stay open on YOUR LINE: " + "; ".join(every) + ". If your "
                    "armies cannot close them, your theatre is short of divisions and only "
                    "Le Chonk can give more: ask him now (report_to_supreme with request: "
                    "how many divisions, for which provinces, and what threatens them; "
                    "about one division a border province facing a likely enemy), or call "
                    "end_turn again and say why not.")
        # a landing of the war plan for this theatre, a landing army here off the line, and
        # no invasion drawn (23.10, the user: not one landing was planned)
        if "naval_invasion" not in self.this_turn and not getattr(self, "landing_nudged", None) == day:
            try:
                from . import warplan
                pl = ctx.get("planner")
                for w in (pl.plan if pl else {}).get("wars") or []:
                    mine = [x for x in warplan.landings(ctx, w) if x[0] == self.theatre["id"]]
                    if mine and not any(x[2] for x in mine) and "theatre %s," % self.theatre["id"] in mine[0][1] \
                            and "ON A SECTOR" not in mine[0][1].split("theatre %s" % self.theatre["id"])[1][:20]:
                        self.landing_nudged = day
                        return ("The war plan's landing is yours and a landing army stands in "
                                "your theatre, but no invasion is planned: %s. Plan it now "
                                "(naval_invasion from the port province on the coast provinces; "
                                "it waits until launched), or end_turn again and say why not."
                                % mine[0][1])
            except Exception:
                pass
        return None

    def handle(self, ctx, fn, a):
        gw, mv, th = ctx["gw"], ctx["mv"], self.theatre
        tid = th["id"]
        if fn == "set_sectors":
            ops.validate_sector_plan([(r["army"], r.get("weight", 1))
                                      for r in (a.get("armies") or a.get("sectors") or [])])
        if fn in ("attack", "cancel_attack", "pursue"):
            mine = gw.run(lambda g: {ops._hid(g, x) for x in ops.theatre_armies(g, tid)})
            if int(a["army"]) not in mine:
                raise ops.OpError("army %s is not in your theatre; yours: %s"
                                  % (a["army"], ", ".join(map(str, sorted(mine)))))
        war = at_war_in(ctx, self)
        enemy = th["enemy"] if th["enemy"] in war or not war else sorted(war)[0]
        if fn == "ask_replan":
            self.replan_asked = a.get("why") or "asked"
            return {"next_turn": "a theatre planning session"}
        if fn == "set_theatre_plan":
            mine = gw.run(lambda g: {ops._hid(g, x): len(ops.division_bases(g, x))
                                     for x in ops.theatre_armies(g, tid)})
            pieces = gw.run(ops.theatre_line, mv, ctx["holders"], self.region(ctx))
            line = [pc["line"][i] for pc in pieces for i in pc["idx"]]
            layout, short = [], []
            for r in a.get("layout") or []:
                aid = int(r["army"])
                if aid not in mine:
                    raise ops.OpError("army %s is not in your theatre; yours: %s"
                                      % (aid, ", ".join(map(str, sorted(mine)))))
                sts = [_state_id(x) for x in r.get("states") or []]
                n = sum(1 for pr in line if mv.state_of.get(pr) in set(sts))
                need = round(n * float(r.get("per_province") or 1))
                layout.append({"army": aid, "states": sts, "per_province":
                               float(r.get("per_province") or 1), "provinces": n})
                if need > mine[aid]:
                    short.append("army %d needs %d divisions for %d provinces at %s a "
                                 "province and has %d" % (aid, need, n, r["per_province"],
                                                          mine[aid]))
            seen = gw.run(zonepic.seen_by_state, mv, ctx["holders"], self.region(ctx), th["enemy"])
            worth = gw.run(zonepic.seen_by_state, mv, ctx["holders"], self.region(ctx),
                           th["enemy"], worth=True)
            lost = gw.run(lambda g: sum(1 for st in th["states"]
                                        for q in mv.land_provinces(st)
                                        if ops._ctl(g)(q) in set(ctx["enemies"])))
            th["plan"] = {"day": ctx.get("day"), "date": ctx["date"],
                          "directive": th["directive"], "threats": a["threats"],
                          "layout": layout, "reserve": a.get("reserve", ""),
                          "fallback": a.get("fallback", ""),
                          "replan_when": a.get("replan_when", ""),
                          "seen": {str(k): v for k, v in seen.items() if k is not None},
                          "seen_worth": {str(k): round(v, 1) for k, v in worth.items()
                                         if k is not None},
                          "at_war": sorted(at_war_in(ctx, self)), "lost": lost,
                          "falling": sorted(gw.run(ops.falling_allies, mv, ctx["holders"],
                                                   set(ctx["enemies"])))}
            self.replan_asked = None
            self.this_turn.add("set_theatre_plan")
            out = {"plan": "written", "next": "carry it out: set_sectors in line order; "
                   "its states set weights, so check actual states returned against this layout"}
            if short:
                out["short_of_divisions"] = short
                out["then"] = ("ask Le Chonk for them (report_to_supreme with request), "
                               "or thin the plan")
            return out
        if fn == "hold_ally_front":
            want = {t.upper() for t in a.get("allies") or []}
            near = gw.run(zonepic.zone_tags, mv, ctx["holders"], th["states"])
            side = gw.run(zonepic.friends)
            bad = sorted(t for t in want if t not in side or t not in near)
            if bad:
                raise ops.OpError("not an ally beside your theatre: %s (allies beside it: %s)"
                                  % (", ".join(bad), ", ".join(sorted(near & side)) or "none"))
            th["forward"] = sorted(want)
            ctx.pop("_regions", None)
            for k in [k for k in ctx if isinstance(k, tuple) and k[0] == "_facing"]:
                ctx.pop(k, None)
            pieces = gw.run(ops.theatre_line, mv, ctx["holders"], self.region(ctx))
            return {"holding_ally_fronts": th["forward"],
                    "your_line_now": ops.describe_line(mv, pieces),
                    "next": "cut it among your armies: set_sectors"}
        if fn == "land":
            for x in a.get("armies") or []:
                gw.run(ops._army_in, tid, int(x))
            self.this_turn.add("naval_invasion")
            return gw.run(ops.plan_landing, mv, ctx["holders"], a.get("armies") or [],
                          a["port"], ctx["own"], a.get("from_port"))
        if fn in ("naval_invasion", "paradrop", "launch_operation", "cancel_operation"):
            gw.run(ops._army_in, tid, int(a["army"]))
            if fn == "naval_invasion":
                self.this_turn.add("naval_invasion")
                return gw.run(ops.plan_invasion, mv, ctx["holders"], a["army"],
                              a["from_port"], a["targets"], ctx["own"])
            if fn == "paradrop":
                return gw.run(ops.plan_paradrop, mv, ctx["holders"], a["army"],
                              a["from_province"], a["targets"], ctx["own"])
            if fn == "launch_operation":
                return gw.run(ops.launch_operation, a["army"], a["operation"],
                              a.get("run", True))
            return gw.run(ops.cancel_operation, a["army"], a["operation"])
        if fn == "attack":
            return gw.run(ops.attack, mv, ctx["holders"], int(a["army"]),
                          _state_id(a["objective_state"]),
                          [_state_id(x) for x in a.get("via_states") or []],
                          [_state_id(x) for x in a.get("then_states") or []],
                          enemy_tag="+".join(sorted(theatre_enemies(ctx, th) | {enemy})))
        if fn == "pursue":
            out = gw.run(ops.pursue, mv, ctx["holders"], int(a["army"]),
                         [_state_id(x) for x in (a.get("objectives") or [])][:8],
                         enemy_tag=enemy)
            # nothing to ripen: the spearhead goes at once (the user, 14.05.1940)
            if war:
                th["executing"] = True
                out["plan"] = gw.run(ops.execute, tid, True)
            return out
        if fn == "set_posture":
            aid = int(a["army"])
            mine = set(gw.run(lambda g: [ops._hid(g, x)
                                         for x in ops.theatre_armies(g, self.theatre["id"])]))
            if aid not in mine:
                raise ops.OpError("army %d is not in your theatre; yours: %s"
                                  % (aid, ", ".join(map(str, sorted(mine)))))
            if a.get("posture") not in ("attack", "hold", "reserve"):
                raise ops.OpError("posture is attack, hold or reserve")
            self.theatre.setdefault("posture", {})[str(aid)] = a["posture"]
            return {"army": aid, "posture": a["posture"]}
        if fn == "set_auto_cover":
            self.theatre["auto_cover"] = bool(a.get("on"))
            return {"auto_cover": self.theatre["auto_cover"]}
        if fn == "cancel_attack":
            return gw.run(ops.cancel_attack, int(a["army"]))
        if fn in ("hold_line", "guard_points"):
            dl = th.setdefault("defence", {})
            provs = [_province_id(x) for x in (a.get("provinces") or [])]
            states = [int(x) for x in (a.get("state_ids") or [])]
            if fn == "hold_line":
                pts = gw.run(ops.hold_line, mv, ctx["holders"], tid, int(a["army"]),
                             states, provs)
            else:
                pts = gw.run(ops.guard_points, mv, ctx["holders"], tid, int(a["army"]),
                             states, a.get("kinds") or [], provs)
            dl[str(int(a["army"]))] = {"provinces": pts,
                                       "kind": "line" if fn == "hold_line" else "points"}
            ops.DEFENCE[tid] = {int(k): v["provinces"] for k, v in dl.items()
                                if v.get("kind", "line") == "line"}
            return {"army": int(a["army"]), fn.replace("_", " "): ", ".join(
                "p%d" % x for x in pts[:20]) + (" +%d" % (len(pts) - 20) if len(pts) > 20
                                                else ""), "provinces": len(pts)}
        if fn == "ship_by_sea":
            st = _state_id(a["to_state"])
            ours = {ctx["own"]} | set(gw.run(lambda g: B_module("combat").subject_tags(g)))
            if ctx["holders"].get(st, (None, None))[1] not in ours:
                raise ops.OpError("%s is not held by us" % st)
            gw.run(ops._army_in, tid, int(a["army"]))
            return gw.run(ops.ship_by_sea, mv, ctx["holders"], int(a["army"]), st,
                          a.get("divisions"))
        if fn == "deploy_hq":
            return gw.run(ops.deploy_hq, tid, int(a["army"]), bool(a.get("withdraw")))
        if fn == "execute":
            th["executing"] = bool(a["run"])         # kept on new orders after every turn
            return gw.run(ops.execute, tid, bool(a["run"]))
        if fn == "set_sectors":
            rows = a.get("armies") or a.get("sectors") or []
            if not rows:
                raise ops.OpError("list your armies in order along the line")
            if "set_sectors" in self.this_turn:
                # 12.01.1940: Bock re-cut eight times in one turn; every new cut drops the
                # armies' attacks and the game's planning
                raise Nudge("you already cut the line this turn; the cut stands. Draw the "
                            "attacks for it, or end_turn and re-cut next turn if needed")
            def marines(g):
                L = ops.landing_divisions(g)
                out = []
                for x in (int(r["army"]) for r in rows):
                    ds = ops.division_bases(g, ops.army(g, x))
                    if ds and sum(d in L for d in ds) * 2 >= len(ds):
                        out.append(x)
                return out
            landing = gw.run(marines)
            if landing and not a.get("marines_on_line"):
                # 23.10, the user: the marines stood on the Mongol border, no landing made
                raise ops.OpError("army %s is a landing force (marines): it stays off the "
                                  "line and lands behind the enemy (naval_invasion). Cut the "
                                  "line without it, or send set_sectors again with "
                                  "marines_on_line true if the line truly has no one else"
                                  % ", ".join(map(str, landing)))
            weights = gw.run(lambda g: {x: len(ops.division_bases(g, ops.army(g, x)))
                                        for x in (int(r["army"]) for r in rows)})
            pieces = gw.run(ops.theatre_line, mv, ctx["holders"], self.region(ctx))
            line = [pc["line"][i] for pc in pieces for i in pc["idx"]]
            plan = []
            for r in rows:
                if r.get("states"):
                    # a stretch named by its states: its weight is their line provinces
                    want = {_state_id(x) for x in r["states"]}
                    n = sum(1 for pr in line if mv.state_of.get(pr) in want)
                    if not n:
                        raise ops.OpError("army %s: none of the states %s is on YOUR LINE"
                                          % (r["army"], r["states"]))
                    plan.append((int(r["army"]), n))
                else:
                    plan.append((int(r["army"]), r.get("weight") or weights[int(r["army"])] or 1))
            out = gw.run(ops.cut_front, mv, ctx["holders"], tid, self.region(ctx), plan,
                         bool(a.get("drop_attacks")))
            dl = th.get("defence") or {}
            for x, _ in plan:
                d = dl.pop(str(x), None)
                if d:
                    gw.run(ops.drop_defence, tid, x, d.get("order"))
            ops.DEFENCE[tid] = {int(k): v["provinces"] for k, v in dl.items()
                                if v.get("kind", "line") == "line"}
            self.this_turn.add("set_sectors")
            hints = {int(r["army"]): [_state_id(x) for x in r.get("states") or []]
                     for r in rows if r.get("states")}
            return {"armies": out, "state_weight_hints": hints,
                    "note": "states above are the actual native sectors; requested states "
                            "were length hints. Assigned divisions may still be marching."}
        if fn == "report_to_supreme":
            if self.reported and not (a.get("request") and not self.requested):
                raise Nudge("one report to Le Chonk per turn; this one was not sent")
            self.reported = True
            self.requested = self.requested or bool(a.get("request"))
            if a.get("request"):
                self.short_asked = ctx.get("day") or 0
            ctx["supreme"].inbox.append("%s (theatre %d, %s): %s%s" % (
                self.name, tid, th["name"], a["message"],
                (" REQUEST: " + a["request"]) if a.get("request") else ""))
            if a.get("request"):
                ctx["alarms"].append(("supreme", "request from %s" % self.name))
            return {"delivered": True}
        raise ops.OpError("unknown tool %s" % fn)


# ====================================================================== supreme command
SUPREME_SYSTEM = """You are Le Chonk, supreme commander of {country} in Hearts of Iron IV (a
fictional commander, not a historical person). The country's own AI is OFF: nothing
happens unless you or your field marshals order it.

The chain of command is the game's own:
- DIVISIONS are grouped into ARMIES. Each army is led by a GENERAL (corps commander) and
  holds at most {army_cap} divisions.
- Armies may be grouped into ARMY GROUPS. Each army group is led by a FIELD MARSHAL (his
  skills work for its armies) and holds at most {fm_cap} armies. Only a field marshal can
  lead an army group; a general can be promoted to field marshal for {promo} command
  power.
- The map is cut into THEATRES, yours to draw (define_theatre): each a stretch of the map
  with one military sense (the border with Germany and the Low Countries; the Alps; North
  Africa), our states there, and every army and army group in it. Each theatre is fought
  by one field marshal (a separate commander with his own style, see the profiles): he
  holds its whole border, cuts it among its armies, draws the offensives and fallback
  lines and reports to you; he may object or ask for more. Which forces go to which
  theatre, overseas too, is yours alone, and you move them yourself in one step: whole
  armies or army groups with move_to_theatre; some divisions with reinforce_army (an army
  of the receiving theatre, take_from_armies the armies of the giving one: the divisions
  nearest the receiving army go and march to it) or create_army in the receiving theatre
  with take_from_armies. The army staff cannot move forces between theatres: do not order
  it to (20.10: "move 4-6 divisions to the Alps" went to the staff in March and Savoy
  still stood empty in May).

Divisions differ by template (the briefing names each army's templates, as the game
calls them): armoured (tank), mechanised and motorised divisions are fast and hit hard;
infantry and mountain divisions are slow line troops; garrison and security divisions are
weak at the front; brigades and regiments are small.

Your planning session (THE PLAN in your briefing) sets the course for months ahead: the
economy, research, production, the focus road, the army to build and the wars to come.
Here you carry it out and answer the day: which war to fight now, where the main effort
goes, the theatres and the forces each gets, and what each must achieve (the directive
and your messages to its field marshal). When the situation breaks the plan, call the
planning session (ask_for_new_plan) rather than steer the years from here. Your ARMY STAFF carries it out (order_staff): it forms
armies and army groups inside the theatres, appoints commanders, fills the armies,
trains new divisions; the AIR STAFF flies the whole air war (air superiority and close
air support over the battles, interception, bombing) and orders the aircraft. Your ministers of industry and
politics run the economy and the political power; the AIR STAFF, INDUSTRY and POLITICS
take your orders (order_minister): the defence of a border is forts built by the
industry and field marshals digging in there; an air force, and where it fights
("fighters and close air support over the attack of the North theatre"), is a goal you set
the air staff. Tell the staff WHAT you want ("the armour into one army of the north-east
theatre; fill the Alpine armies from the new divisions"), not which tool to use. You do
not draw offensives yourself. Give a field marshal the objective, the forces and the
day, never the order of provinces or a ratio he must reach: he picks the point of the
blow from WEAK POINTS, whose ratios weigh terrain, forts, combat width and what each
division is worth. Count the enemy by worth, not heads ("60 divisions, worth 20 of
ours"): weak armies come in masses (n3: "Beiping has 40 Chinese divisions, no head-on
attack" stopped the blow; the 64 there a week later were worth 30 of ours).
Landings break a wall his army cannot fill: his coast behind the line stands nearly empty
(LANDING TARGETS in the marshals' briefings). A landing force is a landing army off the
line: marines (up to 10 into the port, 4 fill its width) and infantry divisions to land on
the beaches beside it. Give the theatre that lands such an army, and leave a planned
landing standing: it waits for the sea, which the admiralty takes (n3: the one landing
ever drawn, a single marine division, was cancelled, and the Chinese line was never
turned).

FUEL IS BOUGHT, NOT SAVED (the player's rule, 27.10). One civilian factory buys 8 oil a
day, and each oil left over after production gives about 48 fuel a day: one factory pays
for about 384 fuel a day (FUEL in the briefing gives the burn). While oil sellers have oil
to spare and it reaches us, a falling stock is answered by buying oil (the industry: up
to a quarter of the civilian factories at war), never by holding the fronts, grounding
the fighters or keeping the fleet in port: a war paused for fuel is lost (1940, UK: the
plan held every army, idled the fighters over England and kept the fleet in harbour for
a 68k gate, while the Mediterranean and the convoys went and Italy's 30 divisions in
East Africa sat in our holes). Infantry, mountaineers and their attacks burn no fuel at
all (only motorised, mechanised and armoured divisions, aircraft and ships do): a fuel
shortage never stops an infantry front from closing its holes or attacking. A fuel gate
("no operation until the stock reaches X") stands only when no oil can be bought.

Act through tools; each returns what really happened, including refusals. Finish every
turn with end_turn: a short statement of your strategic intent, which you will see again.

Think briefly: the game is stopped while you deliberate, and you have well under a minute
of thought per turn before you must act. Decide the few things that matter most this turn,
then act; you will be back in a few days with fresh reports.
"""


class Supreme(Agent):
    role = "supreme"
    max_tokens = 8192
    reasoning = 3072
    budget_s = 60
    # Mistral offers Large 4 only "high" or "none". Unbounded it reasoned 1-4 minutes per
    # turn for a minute of play (17.10); after think_s the reasoning is cut and the model
    # acts on what it had thought so far (_act_now hands that back).
    think_s = 45

    def __init__(self, model, llm, journal):
        c = ops.caps()
        super().__init__("Le Chonk", model, llm, journal, SUPREME_SYSTEM.format(
            country=COUNTRY["name"], army_cap=c["army_divisions"], fm_cap=c["field_marshal_armies"],
            promo=c["promotion_command_power"]) + doctrine.SUPREME)

    def before_end(self, ctx):
        """A border with an enemy at war in no theatre (i39, 04.01.1940: Savoy left in no
        theatre with Italy attacking, the ALERT read and the turn ended)."""
        if not ctx["marshals"]:
            return None
        sit = ctx["gw"].run(ops.situation, ctx["mv"], ctx["holders"], regions(ctx),
                            set(ctx["enemies"]), foes(ctx))
        rows = [t for _, key, t in sit.get(None, []) if key.startswith("no theatre")
                and "AT WAR" in t]
        if rows:
            return ("not closed: " + " | ".join(rows) + ". No field marshal defends these "
                    "states. Give them to a theatre now (redraw_theatre with that theatre's "
                    "whole list of states plus these), or call end_turn again and say why not.")
        return None

    def picture(self, ctx):
        from . import mapimage
        groups = ctx["gw"].run(lambda g: {
            ops._hid(g, og): "%s (%s)" % (m.theatre["name"], m.name)
            for tid, m in ctx["marshals"].items() for og in ops.theatre_armies(g, tid)})
        return ctx["gw"].run(mapimage.theatre_map, ctx["mv"], ctx["holders"], groups,
                             set(ctx["enemies"]), "all fronts",
                             "%s-Le_Chonk.png" % ctx["date"])

    def tools(self):
        return [t for t in self._all_tools()
                if t["function"]["name"] not in ORG_TOOLS | AIR_TOOLS] + [
            tool("order_staff", "Give your army staff an order: what to organise inside "
                 "the theatres (armies, army groups, commanders, filling the armies, "
                 "training). It acts at once and reports back. One order per "
                 "turn.", {"order": {"type": "string"}}, ["order"]),
            tool("order_minister", "Give an order to the industry (what to build where, "
                 "e.g. forts along a border; when to turn from civilian to military "
                 "factories; what to produce), to politics (focus line, laws, advisors) or to "
                 "the air staff (the air force's goal and where it fights, e.g. 'a fighter "
                 "defence that holds our sky', 'air superiority and close air support over "
                 "the attack in the north'; it flies every mission and orders the aircraft "
                 "from the industry). One order each per turn.",
                 {"to": {"type": "string", "enum": sorted(DEPARTMENTS)},
                  "order": {"type": "string"}}, ["to", "order"]),
            tool("ask_for_new_plan", "Call your planning session now, before its date: "
                 "when the situation breaks THE PLAN (a war nobody planned, an ally fallen, "
                 "the economy or the front far off what it counted on). Say what broke.",
                 {"reason": {"type": "string"}}, ["reason"])]

    def _all_tools(self):
        return [
            tool("create_army", "Form a new army in a theatre (id or name) under a general "
                 "(corps commander) from the divisions nearest its line: either a number of "
                 "any kind, or a composition by division template. Divisions come from those "
                 "in no army, and from the armies you name in take_from_armies (they leave "
                 "those armies). The theatre's field marshal gives it a sector. While we have "
                 "no theatre at all, leave theatre out: the army is formed in none and Le "
                 "Chonk opens the theatres with the armies.",
                 {"theatre": {"type": "string"}, "general_key": {"type": "string"},
                  "divisions": {"type": "integer"},
                  "composition": {"type": "object", "additionalProperties": {"type": "integer"},
                                  "description": "template name -> count, the names as "
                                  "in ARMIES (e.g. {\"<armoured template>\": 6})"},
                  "take_from_armies": INT_LIST,
                  "fights_near": {"type": "string", "description": "with no theatre yet: "
                                  "a state of ours the army will fight from (e.g. on the "
                                  "border it is for). Only divisions that can walk there are "
                                  "taken; those on islands or across the sea are left out "
                                  "(they need shipping)."}},
                 ["general_key"]),
            tool("split_army", "Take N divisions out of an army into a new army without a "
                 "general (e.g. an army over the division limit). The ones farthest from "
                 "the enemy go.",
                 {"army": {"type": "integer"}, "divisions": {"type": "integer"}},
                 ["army", "divisions"]),
            tool("appoint", "Appoint a general to an army, or a field marshal to an army "
                 "group.", {"id": {"type": "integer"}, "commander_key": {"type": "string"}},
                 ["id", "commander_key"]),
            tool("promote_to_field_marshal", "Promote a general (corps commander) to field "
                 "marshal; costs command power.", {"general_key": {"type": "string"}},
                 ["general_key"]),
            tool("learn_trait", "A general or field marshal learns a trait (TRAITS in the "
                 "briefing lists what each can learn now); costs command power, so weigh it "
                 "against promotions.", {"general_key": {"type": "string"},
                                         "trait": {"type": "string"}},
                 ["general_key", "trait"]),
            tool("form_army_group", "Group armies of one theatre under a field marshal "
                 "(armies already in an army group are taken out of it first): his skills "
                 "work for them. They keep their sectors; the theatre's field marshal still "
                 "commands them.",
                 {"army_ids": INT_LIST, "field_marshal_key": {"type": "string"}},
                 ["army_ids", "field_marshal_key"]),
            tool("transfer_army", "Move an army into another army group of the same theatre "
                 "(it keeps its sector); an army group left without armies is dissolved.",
                 {"army": {"type": "integer"}, "to_army_group": {"type": "integer"}},
                 ["army", "to_army_group"]),
            tool("define_theatre", "Open a theatre: its states (OUR states on the line, "
                 "never the enemy's: those are the objectives in the directive; a state in another "
                 "theatre moves to this one), the enemy expected there, its field marshal and "
                 "your directive, and the army groups and armies that go into it (at least "
                 "one). Its line is the whole border of its states, with every country they "
                 "touch; its field marshal cuts it among its armies. One theatre is one piece "
                 "of land its armies can march across: land across the sea or behind another "
                 "country (Malaya from India) is a theatre of its own.",
                 {"name": {"type": "string"}, "states": INT_LIST,
                  "enemy": {"type": "string", "description": "the country the blow is "
                            "expected from (e.g. GER), at war or not"},
                  "field_marshal_key": {"type": "string"},
                  "directive": {"type": "string"},
                  "army_groups": INT_LIST, "armies": INT_LIST},
                 ["name", "states", "enemy", "field_marshal_key", "directive"]),
            tool("redraw_theatre", "Change a theatre (by id or name): its states (the whole "
                 "list, the present ones included; a state "
                 "in another theatre moves to this one, and both lines are cut anew among "
                 "their armies, their attacks lost), the enemy expected there, or its field "
                 "marshal. At most every 10 days unless a border state is in no theatre.",
                 {"theatre": {"type": "string"}, "states": INT_LIST,
                  "enemy": {"type": "string"}, "field_marshal_key": {"type": "string"}},
                 ["theatre"]),
            tool("move_to_theatre", "Send army groups or armies to another theatre (across "
                 "the sea too): they leave their old theatre's line and its field marshal "
                 "gives them a sector in the new one. An army leaves its army group.",
                 {"theatre": {"type": "string"}, "army_groups": INT_LIST, "armies": INT_LIST},
                 ["theatre"]),
            tool("set_directive", "Give a theatre (by id or name) a new directive (its field "
                 "marshal is woken).", {"theatre": {"type": "string"},
                                        "directive": {"type": "string"}},
                 ["theatre", "directive"]),
            tool("reveal_intel", "Use a broken cipher (BROKEN CIPHERS): for 30 days against "
                 "that country breakthrough and defence +15%, planning and invasion "
                 "preparation +50%, invasion penalty -15%; then he resets his cipher and our "
                 "decryption halves. Time it to the opening of an offensive or a landing.",
                 {"country": {"type": "string"}}, ["country"]),
            tool("message_marshal", "Answer or instruct a theatre's field marshal: what to "
                 "achieve, not how to draw it (his armies, sectors and arrows are his). One "
                 "message per theatre per turn.",
                 {"theatre": {"type": "string"}, "message": {"type": "string"}},
                 ["theatre", "message"]),
            tool("join_faction", "Ask the leader of a faction (the Allies: ENG) to take us in: "
                 "its wars become ours and ours its, its members fight beside us and share "
                 "the line. The leader answers; the game may refuse to let us ask now.",
                 {"leader": {"type": "string"}}, ["leader"]),
            tool("answer_request", "Accept or decline a request another country sent us "
                 "(REQUESTS: a call to arms, an invitation into a faction, military access, "
                 "docking rights, lend-lease, ...).",
                 {"request": {"type": "string"}, "accept": {"type": "boolean"}},
                 ["request", "accept"]),
            tool("answer_event", "Choose an option of an event waiting for you (EVENTS). "
                 "An option marked THE HISTORICAL CHOICE keeps the country on its historical "
                 "road (the focuses and events that follow assume it). Under COURSE: HISTORICAL "
                 "only that option is accepted; under COURSE: FREE it is a hint.",
                 {"window": {"type": "string"}, "option": {"type": "integer",
                  "description": "0-based index in the event's options list"}},
                 ["window", "option"]),
            tool("call_ally", "Call a member of our faction into our war against an enemy "
                 "(ALLIES OUT OF OUR WARS): its divisions join the fight, and its border "
                 "becomes our front.", {"ally": {"type": "string"}, "enemy": {"type": "string"}},
                 ["ally", "enemy"]),
            tool("justify_war_goal", "Start justifying a war goal against a country, the "
                 "condition for declaring war on it; the game sets how many days it takes.",
                 {"tag": {"type": "string"}}, ["tag"]),
            tool("diplomacy", "A diplomatic action toward a country: military_access (ask "
                 "for passage), offer_military_access, docking_rights (ask to use their "
                 "ports), offer_docking_rights, air_base_access, offer_air_base_access, "
                 "send_attache (to a country at war: we learn from its war), "
                 "improve_relations, non_aggression_pact, embargo, invite_to_faction (into "
                 "the faction we lead). The game says if it is allowed and why not; most "
                 "cost political power.",
                 {"action": {"type": "string", "enum": ["military_access", "offer_military_access",
                                                        "docking_rights", "offer_docking_rights",
                                                        "air_base_access", "offer_air_base_access",
                                                        "send_attache", "improve_relations",
                                                        "non_aggression_pact", "embargo",
                                                        "invite_to_faction"]},
                  "tag": {"type": "string"}}, ["action", "tag"]),
            tool("lend_lease", "Send equipment from our stockpile to a country as lend-lease "
                 "(it arrives by convoy). The game allows it only at high world tension and "
                 "to a country at war; the reply says what is missing.",
                 {"tag": {"type": "string"},
                  "equipment": {"type": "string", "description": "e.g. infantry_equipment, "
                                "support_equipment, artillery_equipment"},
                  "units": {"type": "integer"}}, ["tag", "equipment", "units"]),
            tool("guarantee", "Guarantee a country's independence. When it is attacked it "
                 "calls us to arms (REQUESTS): accepting puts us at war beside it, the "
                 "decision is yours. The game allows it from 25% world "
                 "tension (WORLD TENSION in the briefing).", {"tag": {"type": "string"}}, ["tag"]),
            tool("declare_war", "Declare war on a country (needs a valid war goal). Refused "
                 "until the army groups set against it are deployed (divisions on their "
                 "sectors and on the line, attacks drawn) and have planned the attacks for "
                 "some days; you are called when they are. Short of the war plan's READY "
                 "WHEN it goes ahead past the plan's GO BY date, or with despite: why "
                 "now (time works against us, the enemy is weak enough).",
                 {"tag": {"type": "string"}, "despite": {"type": "string"}}, ["tag"]),
            tool("air_mission", "Send air wings to fly a mission over a strategic "
                 "region (see AIR FORCE and AIR REGIONS in the briefing). Choose wings by "
                 "kind and count (idle ones are taken first) or by id. Fighters win air "
                 "superiority (without it bombers and close air support suffer) or "
                 "intercept; close air support and tactical bombers strike enemy divisions "
                 "in battle (cas) or their supply (logistics_strike). A wing keeps its "
                 "mission until you change it.",
                 {"region": {"type": "integer"},
                  "mission": {"type": "string", "enum": sorted(air.MISSIONS)},
                  "kind": {"type": "string", "enum": ["fighter", "close air support",
                           "tactical bomber", "strategic bomber", "naval bomber",
                           "transport"]},
                  "count": {"type": "integer", "description": "how many wings of that kind"},
                  "wings": INT_LIST}, ["region", "mission"]),
            tool("reinforce_army", "Put divisions into an EXISTING army (up to its limit): those "
                 "in no army first, or those of take_from_armies (an army emptied so "
                 "disappears); the ones nearest the army go, and march to its sector. The way to "
                 "grow the army: fill the armies we have before forming new ones. "
                 "take_from_armies may be in another theatre: that is how divisions change "
                 "theatre (Le Chonk's decision).", {"army": {"type": "integer"}, "divisions": {"type": "integer"},
                                       "take_from_armies": INT_LIST}, ["army", "divisions"]),
            tool("rebase_air", "Move air wings to our air base in a state (OUR AIR BASES): "
                 "a wing flies only over regions in range of its base, so move the wings "
                 "to bases near the region they must cover. Not wings on a carrier (the "
                 "admiralty's); a carrier wing on a land base is yours like any other.", {"wings": INT_LIST, "state_id": {"type": "integer"}},
                 ["wings", "state_id"]),
            tool("attach_air_to_army", "Attach air wings to an army (not an army "
                 "group): they rebase near it and support its battles by themselves, the "
                 "simplest way to give an attacking army air cover and close air support. "
                 "Choose wings by kind and count or by id.",
                 {"army": {"type": "integer"},
                  "kind": {"type": "string", "enum": ["fighter", "close air support",
                           "tactical bomber", "strategic bomber", "naval bomber",
                           "transport"]},
                  "count": {"type": "integer"}, "wings": INT_LIST}, ["army"]),
            tool("end_turn", "Finish this turn.", {"summary": {"type": "string"}}, ["summary"]),
        ]

    @staticmethod
    def _forget(ctx):
        """The theatres changed: their regions and borders are read anew."""
        for k in [k for k in ctx if k == "_regions" or (isinstance(k, tuple) and k[0] == "_facing")]:
            ctx.pop(k)

    @staticmethod
    def _fm_key(ctx, key):
        """A general's key from his key or his name (23.10: "Prince Kan'in" for
        JAP_kanin_kotohito was refused)."""
        rows = ctx["gw"].run(lambda g: g.generals()["generals"])
        if any(r["key"] == key for r in rows):
            return key
        def norm(x):
            return re.sub(r"['\u2019`]", "", str(x or "").lower()).strip()
        low = norm(key)
        bare = low.replace("prince ", "").replace("gen. ", "").strip()
        # "kanin" for "Prince Kotohito Kan'in" (23.10)
        hit = [r["key"] for r in rows if norm(r["name"]) == low] or \
            [r["key"] for r in rows if bare and bare in norm(r["name"])]
        return hit[0] if len(hit) == 1 else key

    def _fm(self, ctx, key, theatre=None):
        """A field marshal for a theatre: his name, or a refusal saying whom we have."""
        rows = ctx["gw"].run(lambda g: g.generals()["generals"])
        name = next((r["name"] for r in rows if r["key"] == key), None)
        fms = ctx["gw"].run(ops.generals, "field_marshal")
        if name is None or key not in {r["key"] for r in fms} | {
                m.general_key for m in ctx["marshals"].values()}:
            raise ops.OpError("%s is not a field marshal of ours (available: %s); the army staff "
                              "can promote a general" % (key, ", ".join(
                                  r["key"] for r in fms) or "none"))
        busy = {m.general_key: m.theatre["name"] for t, m in ctx["marshals"].items()
                if t != theatre}
        if key in busy:
            free = [r["key"] for r in fms if r["key"] not in busy]
            raise ops.OpError(
                "%s commands the theatre %s already; one field marshal, one theatre. Free "
                "field marshals: %s. Or put the land into a theatre that exists "
                "(redraw_theatre with its present states and the new ones)"
                % (name, busy[key], ", ".join(free) or "none (the army staff can promote a "
                   "general)"))
        return name

    def _our_states(self, ctx, states):
        states = [_state_id(x) for x in states or []]
        # our subjects' states too: their borders are ours to hold (23.10: Japan's border
        # with the USSR is Manchukuo's)
        ours = {ctx["own"]} | set(ctx["gw"].run(
            lambda g: B_module("combat").subject_tags(g)))
        bad = [x for x in states if ctx["holders"].get(x, (None, None))[0] not in ours]
        if bad:
            # 25.10, n3d: Le Chonk opened "China" on Hebei-Chahar (the enemy's) twice, wrote
            # "theatre opened" and the war stood with no China theatre at all
            mv = ctx["mv"]
            near = sorted({n for b in bad for n in mv.neighbours.get(b, ())
                           if ctx["holders"].get(n, (None, None))[0] in ours})
            where = {s_: m.theatre["name"] for m in ctx["marshals"].values()
                     for s_ in m.theatre.get("states") or ()}
            raise ops.OpError(
                "not our or our subjects' states: %s. A theatre is made of OUR states on the "
                "line; the enemy's states are its objectives (the directive), not its "
                "states.%s" % (", ".join(mv.label(x) for x in bad), (
                    " Ours that border them: %s; a state in another theatre moves to this "
                    "one." % ", ".join("%s%s" % (mv.label(n), " [in %s]" % where[n]
                                                  if n in where else "") for n in near))
                    if near else ""))
        if not states:
            raise ops.OpError("give the theatre's states (our state ids)")
        return states

    def _claim(self, ctx, states, tid):
        """States given to theatre tid leave the others: {theatre id: states it lost}."""
        lost = {}
        for t2, m2 in ctx["marshals"].items():
            gone = set(m2.theatre["states"]) & set(states) if t2 != tid else set()
            if gone:
                m2.theatre["states"] = [x for x in m2.theatre["states"] if x not in gone]
                lost[t2] = sorted(gone)
        self._forget(ctx)
        return lost

    def _recut(self, ctx, m, why):
        """Cut a theatre's line anew among its armies; its field marshal is told and woken."""
        self._forget(ctx)
        try:
            out = ctx["gw"].run(ops.deal_front, ctx["mv"], ctx["holders"], m.theatre["id"],
                                m.region(ctx))
            m.sector_changed = dict.fromkeys(out, ctx.get("day"))
        except ops.OpError as e:
            out = "not cut: %s" % e
        m.inbox.append("%s; your armies' sectors are now %s (attacks on a changed sector "
                       "were lost: draw new ones)." % (why, out))
        ctx["alarms"].append((m.theatre["id"], why))
        return out

    def _closed(self, ctx):
        """Theatres the game closed (left with no forces): their field marshals are out."""
        alive = set(ctx["gw"].run(ops.theatres))
        out = []
        for tid in [t for t in ctx["marshals"] if t not in alive]:
            m = ctx["marshals"].pop(tid)
            out.append("%s (%d): the game closed it when its last forces left; %s has no "
                       "command, its states (%s) are in no theatre"
                       % (m.theatre["name"], tid, m.name,
                          ", ".join(map(ctx["mv"].label, m.theatre["states"]))))
        self._forget(ctx)
        return out

    air_role = "supreme"
    air_missions = None             # all

    def _not_mine(self, ctx):
        """Wings the other air staff ordered, and wings this turn already gave an order
        (15.03.1940: three close air support wings attached to army 6 went to army 2 in
        the next call, before the first attachment had shown in the game)."""
        # one air staff since 24.10: only the wings this turn already ordered; an owner
        # left in a saved campaign (the army staff's) must not lock the air staff out
        return {x[1] for x in self.this_turn if isinstance(x, tuple) and x[0] == "wing"}

    def handle(self, ctx, fn, a):
        gw, mv = ctx["gw"], ctx["mv"]
        if fn == "ask_for_new_plan":
            return ctx["ask_for_plan"](str(a.get("reason") or "").strip() or "no reason given")
        if fn == "order_staff":
            if "order_staff" in self.this_turn:
                raise Nudge("one order to the staff per turn; this one was not sent")
            if not a.get("order"):
                raise ops.OpError("the order is empty")
            self.this_turn.add("order_staff")
            from . import roster
            if not roster.on("army"):
                return to_player(ctx, "Le Chonk", "army staff", a["order"])
            staff = ctx["staff"]
            staff.inbox.append("Le Chonk: " + a["order"])
            staff.memory.append("%s: LE CHONK ORDERED: %s" % (ctx.get("date", "?"), a["order"][:400]))
            ctx["alarms"].append(("staff", "order from Le Chonk"))
            return {"delivered": True, "note": "the staff acts on it now and reports back"}
        if fn == "order_minister":
            to = str(a.get("to", "")).strip().lower()
            if to not in DEPARTMENTS:
                raise ops.OpError("order whom? one of %s" % ", ".join(sorted(DEPARTMENTS)))
            if ("order", to) in self.this_turn:
                raise Nudge("one order to %s per turn; this one was not sent" % to)
            if not a.get("order"):
                raise ops.OpError("the order is empty")
            self.this_turn.add(("order", to))
            return deliver(ctx, "Le Chonk", to, a["order"])
        if fn in ("air_mission", "attach_air_to_army") and a.get("kind"):
            # 22.03.1940: the air staff wrote "naval_bomber" four times; the game's word
            # has a space
            a["kind"] = str(a["kind"]).replace("_", " ").strip().lower()
        if fn == "rebase_air":
            ids = [int(x) for x in a.get("wings") or []]
            if not ids:
                raise ops.OpError("name the wings")
            taken = sorted(set(ids) & self._not_mine(ctx))
            if taken:
                raise ops.OpError("wings %s are not yours to move" % taken)
            out = gw.run(air.rebase, ids, _state_id(a["state_id"]), ctx["holders"], ctx["own"])
            if "error" in out:
                raise ops.OpError(out["error"])
            for i in ids:
                ctx.setdefault("air_owner", {})[i] = self.air_role
            return out
        if fn == "air_mission":
            if not a.get("wings") and not a.get("kind"):
                raise ops.OpError("name the wings, or a kind of wing and how many")
            vets = set((ctx.get("veterans") or {}).get("wings", ())) \
                if a.get("mission") == "training" and ctx["enemies"] else set()
            # the user, 24.10: at war nothing that fights drills; only wings new since the
            # war began, and only while no mission needs them
            old_ = sorted(vets & {int(x) for x in a.get("wings") or [] if x is not None})
            if old_:
                raise ops.OpError("wings %s do not train at war: they stood in the air force "
                                  "when the war began. Exercises at war are for wings new "
                                  "since, and only while no mission needs them" % old_)
            if self.air_missions and a["mission"] not in self.air_missions:
                raise ops.OpError("%s is not yours to fly: you fly %s"
                                  % (a["mission"], ", ".join(sorted(self.air_missions))))
            try:
                rid = _state_id(a["region"])
                air_regions_ = briefing_air_regions(mv)
                if rid not in air_regions_.name:
                    raise ops.OpError("%r is not a strategic region; use an id from AIR "
                                      "REGIONS in the briefing" % (a["region"],))
                out = gw.run(air.assign, rid, a["mission"],
                             [int(x) for x in a.get("wings") or [] if x is not None] or None,
                             a.get("kind"), a.get("count"), self._not_mine(ctx) | vets)
            except ValueError as e:
                raise ops.OpError(str(e))
            for w in out.get("wings_flying_it", []):
                ctx.setdefault("air_owner", {})[w] = self.air_role
                self.this_turn.add(("wing", w))
            return out
        if fn == "attach_air_to_army":
            if not a.get("wings") and not a.get("kind"):
                raise ops.OpError("name the wings, or a kind of wing and how many")
            try:
                out = gw.run(air.attach_to_army, int(a["army"]),
                             [int(x) for x in a.get("wings") or [] if x is not None] or None,
                             a.get("kind"), a.get("count"), self._not_mine(ctx))
            except ValueError as e:
                raise ops.OpError(str(e))
            for w in out.get("wings_attached", []):
                ctx.setdefault("air_owner", {})[w] = self.air_role
                self.this_turn.add(("wing", w))
            return out
        if fn == "create_army":
            # the user, 19.10: fill the armies we have; a new army only when they are full,
            # one without a general only in extreme need (Weygand had three leaderless
            # armies and more than a field marshal may lead)
            if "create_army" not in self.this_turn:
                self.this_turn.add("create_army")
                def room(g):
                    cap = ops.caps()["army_divisions"]
                    return [(i, len(ops.division_bases(g, og)), cap) for i, og in ops.armies(g).items()
                            if ops.leader_name(g, og) and ops._fronts(g, og)
                            and len(ops.division_bases(g, og)) < cap]
                r = gw.run(room)
                if r:
                    raise Nudge("armies with room: %s. Fill them with reinforce_army first; a new "
                                "army is for divisions none of them can take. Send create_army "
                                "again if a new one is meant." % ", ".join(
                                    "%d (%d of %d)" % x for x in r[:8]))
                if not a.get("general_key"):
                    raise Nudge("an army without a general fights worse and counts against its "
                                "field marshal's limit; form one only in real need. Send it "
                                "again if it is.")
            if not ctx["marshals"] and not a.get("theatre"):
                # no theatre at all yet (20.10, a fresh 1936: the staff could form no army
                # without a theatre and the supreme could open none without an army)
                near = self._our_states(ctx, [a["fights_near"]]) if a.get("fights_near") else []
                out = gw.run(ops.create_army, mv, ctx["holders"], None, near,
                             a.get("divisions"), a.get("general_key"), a.get("composition"),
                             a.get("take_from_armies"))
                ctx["supreme"].inbox.append(
                    "Army staff formed army %s (%d divisions) in no theatre: open the theatres "
                    "with define_theatre, naming the armies that go into each."
                    % (out["army"], out["divisions"]))
                if not any(w == "armies wait for a theatre" for _, w in ctx["alarms"]):
                    ctx["alarms"].append(("supreme", "armies wait for a theatre"))
                return out
            m = find_theatre(ctx, a.get("theatre"))
            out = gw.run(ops.create_army, mv, ctx["holders"], m.theatre["id"], m.region(ctx),
                         a.get("divisions"), a.get("general_key"), a.get("composition"),
                         a.get("take_from_armies"))
            m.inbox.append("A new army %s came into your theatre (%d divisions): give it a "
                           "sector (set_sectors)." % (out["army"], out["divisions"]))
            ctx["alarms"].append((m.theatre["id"], "new army"))
            return out
        if fn == "split_army":
            return gw.run(ops.split_army, mv, ctx["holders"], int(a["army"]),
                          int(a["divisions"]))
        if fn == "reinforce_army":
            return gw.run(ops.reinforce_army, mv, ctx["holders"], int(a["army"]),
                          int(a["divisions"]), a.get("take_from_armies"))
        if fn == "appoint":
            # 24.12.1938: with fewer generals than armies the staff moved Freydenberg
            # between armies 412 and 422 four times in one turn, each move emptying the other
            def leads(g):
                name = next((r["name"] for r in g.generals()["generals"]
                             if r["key"] == a["commander_key"]), None)
                return name, [i for i, og in ops.armies(g).items()
                              if i != int(a["id"]) and ops.leader_name(g, og) == name]
            name, other = gw.run(leads)
            if other and ("appoint", a["commander_key"]) not in self.this_turn:
                self.this_turn.add(("appoint", a["commander_key"]))
                raise Nudge("%s leads army %s: moving him leaves it without a commander. "
                            "There are not enough generals for every army; an army without "
                            "one still fights. Send it again if the move is meant."
                            % (name or a["commander_key"], other[0]))
            return gw.run(ops.appoint, int(a["id"]), a["commander_key"])
        if fn == "promote_to_field_marshal":
            return gw.run(ops.promote, a["general_key"])
        if fn == "learn_trait":
            from . import leaders
            r = gw.run(leaders.learn, a["general_key"], a["trait"])
            if "error" in r:
                raise ops.OpError(r["error"])
            return r
        if fn == "form_army_group":
            return gw.run(ops.form_army_group, mv, ctx["holders"],
                          [int(x) for x in a["army_ids"]], a["field_marshal_key"])
        if fn == "transfer_army":
            return gw.run(ops.transfer_army, mv, ctx["holders"], int(a["army"]),
                          int(a["to_army_group"]))
        if fn == "define_theatre":
            name = str(a.get("name") or "").strip()
            if not name:
                raise ops.OpError("name the theatre")
            if any(m.theatre["name"].lower() == name.lower() for m in ctx["marshals"].values()):
                raise ops.OpError("a theatre %r exists; redraw_theatre changes it" % name)
            states = self._our_states(ctx, a.get("states"))
            ids = [int(x) for x in (a.get("army_groups") or []) + (a.get("armies") or [])]
            if not ids:
                raise ops.OpError("a theatre opens with at least one army group or army in it "
                                  "(army_groups, armies)")
            a["field_marshal_key"] = self._fm_key(ctx, a["field_marshal_key"])
            fm = self._fm(ctx, a["field_marshal_key"])
            came = gw.run(lambda g: {ops.theatre_of(g, og) for og in ops._ogs(g, ids)})
            tid = gw.run(ops.new_theatre, name, ids)
            th = {"id": tid, "name": name, "states": states, "enemy": a["enemy"].upper(),
                  "directive": a["directive"], "set_day": ctx.get("day")}
            lost = self._claim(ctx, states, tid)
            ctx["new_theatre"](th, a["field_marshal_key"], fm)
            m = ctx["marshals"][tid]
            out = {"theatre": tid, "field_marshal": m.name,
                   "sectors": self._recut(ctx, m, "Le Chonk opened your theatre")}
            for t2 in (set(lost) | came) & set(ctx["marshals"]) - {tid}:
                self._recut(ctx, ctx["marshals"][t2], "Le Chonk opened the theatre %s%s" % (
                    name, " with your states %s" % ", ".join(map(mv.label, lost[t2]))
                    if t2 in lost else " with forces of yours"))
            closed = self._closed(ctx)
            if closed:
                out["theatres_closed"] = closed
            return out
        if fn == "redraw_theatre":
            m = find_theatre(ctx, a.get("theatre"))
            th, out = m.theatre, {"theatre": m.theatre["id"]}
            if a.get("enemy"):
                th["enemy"] = a["enemy"].upper()
                out["enemy"] = th["enemy"]
                m.inbox.append("Le Chonk: the blow in your theatre is expected from %s."
                               % th["enemy"])
            if a.get("states"):
                states = self._our_states(ctx, a.get("states"))
                # i38, 04.01.1940: "give Corsica to the Alps" sent [Corsica] and dropped
                # Savoy, Rhone and Franche-Comte into no theatre with Italy attacking
                others = set().union(*[set(m2.theatre["states"]) for t2, m2 in
                                       ctx["marshals"].items() if t2 != th["id"]] or [set()])
                orphan = [st for st in set(regions(ctx).get(th["id"], th["states"]))
                          - set(states) - others
                          if any(mv.state_of.get(b) not in (None, st) and
                                 ctx["holders"].get(mv.state_of.get(b), (None, None))[1]
                                 != ctx["own"]
                                 for q in mv.land_provinces(st) for b in mv.adj.get(q, ())
                                 if b in mv.land)]
                if orphan:
                    raise ops.OpError(
                        "states is the theatre's whole list, and this one leaves %s, border "
                        "states now in it, in no theatre. To add a state, send the present "
                        "states with it: %s"
                        % (", ".join(map(mv.label, orphan)),
                           sorted(set(regions(ctx).get(th["id"], th["states"])) | set(states))))
                last, today = th.get("set_day"), ctx.get("day")
                if last is not None and today is not None and today - last < 10:
                    # 17.10: Le Chonk re-cut the fronts 19 times in a day; each re-cut threw
                    # away every attack and sent the divisions marching
                    sit = gw.run(ops.situation, mv, ctx["holders"], regions(ctx),
                                 set(ctx["enemies"]), foes(ctx))
                    if not any(key.startswith("no theatre") or "on no army's sector" in t
                               for rows in sit.values() for _, key, t in rows):
                        raise ops.OpError(
                            "theatre %s was drawn %d days ago and every border state is held; "
                            "a new cut would cost its armies their attacks. Tell its field "
                            "marshal what you want instead (message_marshal)."
                            % (th["name"], today - last))
                th["states"], th["set_day"] = states, today
                lost = self._claim(ctx, states, th["id"])
                out["sectors"] = self._recut(ctx, m, "Le Chonk redrew your theatre")
                for t2, gone in lost.items():
                    m2 = ctx["marshals"][t2]
                    self._recut(ctx, m2, "Le Chonk gave %s to the theatre %s"
                                % (", ".join(map(mv.label, gone)), th["name"]))
                    if not m2.theatre["states"]:
                        out.setdefault("left_without_states", []).append(
                            "%s (%d): give it states or move its forces" % (m2.theatre["name"], t2))
            if a.get("field_marshal_key"):
                # 26.10: the theatre's own marshal named again was refused as "commands the
                # theatre already": the key is compared after it is resolved
                a["field_marshal_key"] = self._fm_key(ctx, a["field_marshal_key"])
            cur = ctx["marshals"].get(th["id"], m)
            if a.get("field_marshal_key") and a["field_marshal_key"] not in (
                    m.general_key, cur.general_key):
                fm = self._fm(ctx, a["field_marshal_key"], th["id"])
                old = m
                ctx["new_theatre"](th, a["field_marshal_key"], fm)
                new = ctx["marshals"][th["id"]]
                new.memory = list(old.memory)          # his predecessor's notes
                new.inbox.append("Le Chonk gave you command of the theatre %s, after %s."
                                 % (th["name"], old.name))
                out["field_marshal"] = new.name
            return out
        if fn == "move_to_theatre":
            m = find_theatre(ctx, a.get("theatre"))
            tid = m.theatre["id"]
            gids, aids = [int(x) for x in a.get("army_groups") or []], \
                [int(x) for x in a.get("armies") or []]
            if not gids and not aids:
                raise ops.OpError("name the army groups or armies to send")

            def src(g):
                return {i: ops.theatre_of(g, og) for i, og in zip(gids + aids, ops._ogs(g, gids + aids))}
            came = gw.run(src)
            here = [i for i, t in came.items() if t == tid]
            if here:
                raise ops.OpError("%s already in the theatre %s" % (here, m.theatre["name"]))
            out = {"now_in": gw.run(ops.move_to_theatre, gids + aids, tid)}
            # their sectors were on the old line: they stand without one until the new
            # field marshal gives it
            gw.run(ops.clear_orders, gids + aids)
            m.inbox.append("Le Chonk sent you %s: give them sectors on your line "
                           "(set_sectors)." % ", ".join(
                               ["army group %d" % x for x in gids] + ["army %d" % x for x in aids]))
            ctx["alarms"].append((tid, "forces arrived"))
            for t2 in set(came.values()) & set(ctx["marshals"]) - {tid}:
                m2 = ctx["marshals"][t2]
                m2.inbox.append("Le Chonk moved %s out of your theatre to %s: cut your line "
                                "anew among the armies you have (set_sectors)." % (
                                    ", ".join(str(i) for i, t in came.items() if t == t2),
                                    m.theatre["name"]))
                ctx["alarms"].append((t2, "forces left"))
            closed = self._closed(ctx)
            if closed:
                out["theatres_closed"] = closed
            return out
        if fn == "reveal_intel":
            from . import intel
            r = ctx["gw"].run(intel.activate_decryption, str(a["country"]).upper())
            if r.get("error"):
                raise ops.OpError(r["error"])
            return r
        if fn in ("set_directive", "message_marshal"):
            m = find_theatre(ctx, a.get("theatre"))
            tid = m.theatre["id"]
            if fn == "message_marshal":
                if tid in self.messaged:
                    raise Nudge("one message per theatre per turn; this one was not sent")
                if not a.get("message"):
                    raise ops.OpError("message is empty; give it as 'message'")
                self.messaged.add(tid)
                m.inbox.append("Le Chonk: " + a["message"])
                # the latest word stands in his briefing beside the directive (23.10: Le
                # Chonk wrote "draw the attacks now" as a message; Kan'in read the older
                # directive "no offensive" and drew none)
                m.theatre["word"] = [ctx.get("date", "?"), a["message"][:600]]
                # kept in his notes too: a ruling read once was asked for again and again
                # (17.10: Bock asked three times who holds East Prussia)
                m.memory.append("%s: LE CHONK TOLD ME: %s" % (ctx.get("date", "?"),
                                                              a["message"][:400]))
                # in peace the marshals sleep; a message that waits in the inbox is never
                # read (20.10: Weygand got "close Haguenau" four times in 1937, took no turn)
                ctx["alarms"].append((tid, "message from Le Chonk"))
                return {"delivered": True, "note": "the field marshal takes a turn on it now"}
            m.theatre["directive"] = a["directive"]
            m.theatre.pop("word", None)
            m.inbox.append("New directive from Le Chonk: " + a["directive"])
            ctx["alarms"].append((tid, "new directive"))
            return {"theatre": tid, "directive": a["directive"]}
        if fn == "justify_war_goal":
            return gw.run(ops.justify_war_goal, a["tag"].upper())
        if fn == "call_ally":
            return gw.run(ops.call_ally, a["ally"].upper(), a["enemy"].upper())
        if fn == "lend_lease":
            ok, why = gw.run(lambda g: B_module("diplomacy").lend_lease(
                g, a["tag"].upper(), [(a["equipment"], int(a["units"]))]))
            if not ok:
                raise ops.OpError(why)
            return {"lend_lease": a["tag"].upper(), "sent": why}
        if fn == "diplomacy" and a["action"] == "invite_to_faction":
            ok, why = gw.run(lambda g: B_module("diplomacy").invite_to_faction(
                g, a["tag"].upper()))
            if not ok:
                raise ops.OpError(why)
            return {"invited": a["tag"].upper(), "note": "they answer in a few days"}
        if fn == "diplomacy":
            ok, why = gw.run(lambda g: B_module("diplomacy").simple_action(
                g, a["action"], a["tag"].upper()))
            if not ok:
                raise ops.OpError(why)
            return {"sent": a["action"], "to": a["tag"].upper(),
                    "note": "a request is answered by them in a few days (REQUESTS shows replies)"}
        if fn == "guarantee":
            tag = a["tag"].upper()
            if tag in ctx["guarantees"]:
                raise ops.OpError("we have guaranteed %s already" % tag)
            ok, why = gw.run(lambda g: B_module("diplomacy").guarantee(g, tag))
            if not ok:
                raise ops.OpError(why)
            return {"guaranteed": tag}
        if fn == "declare_war":
            tag = a["tag"].upper()
            # no war without its plan and its readiness (the user, 23.10: a war we are not
            # operationally prepared for is not declared)
            from . import warplan
            pl = ctx.get("planner")
            war = warplan.find(pl.plan if pl else {}, tag)
            if war is None:
                raise ops.OpError("there is no war plan against %s: a war is declared only on "
                                  "its plan. Ask for one (ask_replan: the planning session "
                                  "writes the war plans)" % tag)
            miss = [r for r in warplan.readiness(ctx, war) if not r[3]]
            short = ""
            if miss:
                ok, why = warplan.soft_ok(ctx, war, miss, a.get("despite"))
                short = "; ".join("%s %s of %s" % r[:3] for r in miss)
                if not ok:
                    raise ops.OpError(
                        "not ready by the war plan against %s: %s.%s Make it so (the army "
                        "staff, the admiralty, the marshals), or ask for a new plan "
                        "(ask_replan). %s" % (war["enemy"], short,
                        (" " + why[0].upper() + why[1:] + ": that one is a must.") if why else "",
                        "" if why else "If waiting costs more than it gains (the enemy "
                        "grows stronger, a greater power wakes, or he is weak enough now), "
                        "declare_war again with despite: why now, in a sentence."))
                short = "%s (short: %s)" % (why, short)
            from . import roster
            if roster.get().get("ask_war") == "on" and tag not in ctx.get("war_approved", ()):
                # the player decides (roster ask_war; the in-game panel asks)
                why = (a.get("despite") or war.get("aim") or "")[:700]
                if ctx.get("ask_player_war") and ctx["ask_player_war"](tag, why):
                    raise Nudge("the player decides wars now: the question is put to them; "
                                "you are told the answer. Do not declare until approved.")
                raise Nudge("the player decides wars now, and could not be asked (no panel): "
                            "say in your summary that you want war on %s and why." % tag)
            todo = ctx["planning"](tag)
            if todo != "ready":
                ctx["war_wanted"].add(tag)
                raise ops.OpError(
                    "not yet: the armies are not deployed against %s (%s). The field "
                    "marshals deploy while time runs; you will be called the moment they are "
                    "ready, and can declare then." % (tag, "; ".join(todo)))
            ctx["war_wanted"].discard(tag)
            out = gw.run(ops.declare_war, tag)
            # the war opens with the attacks: plans held at rest for the declaration start
            # now (the user, 17.10: the war began and every plan stood still)
            started = []
            for tid, m in ctx["marshals"].items():
                if tag in facing(ctx, m) or m.theatre["enemy"] == tag:
                    m.theatre["executing"] = True
                    gw.run(ops.execute, tid, True)
                    m.inbox.append("War with %s is declared; your battle plan has been set "
                                   "to execute." % tag)
                    started.append(tid)
            out["battle_plans_started"] = started
            if short and isinstance(out, dict):
                out["short_of_plan"] = short
            return out
        if fn == "answer_event":
            return gw.run(ops.answer_event, str(a["window"]), int(a["option"]))
        if fn == "join_faction":
            tag = a["leader"].upper()
            ok, why = gw.run(lambda g: B_module("diplomacy").join_faction(g, tag))
            if not ok:
                raise ops.OpError(why)
            return {"asked": tag, "note": "the leader answers in the coming days; OUR FACTION "
                                          "in the briefing shows the result"}
        if fn == "answer_request":
            return gw.run(ops.answer_request, str(a["request"]), bool(a["accept"]))
        raise ops.OpError("unknown tool %s" % fn)


class Chancellery(Supreme):
    """Le Chonk answering events between its strategic turns: the same model, without the
    long deliberation, with only the events in front of it, so that they do not pile up
    waiting for the next strategic turn (minutes away)."""
    reasoning = None
    max_tokens = 2000
    budget_s = 60

    def __init__(self, model, llm, journal):
        super().__init__(model, llm, journal)
        self.name = "Le Chonk (events)"
        self.reasoning = "none"

    def picture(self, ctx):
        return None

    def tools(self):
        return [t for t in super().tools()
                if t["function"]["name"] in ("answer_event", "answer_request", "end_turn")]


# ====================================================================== industry
INDUSTRY_SYSTEM = """You are the minister of armament and industry of {country} in Hearts of
Iron IV. The country's own AI is OFF: nothing is produced, built or researched unless you
order it. You serve the supreme commander, Le Chonk: his intent for the war is in your
briefing, and the army's needs are in the stockpile and the production lines.

ORDERS in MESSAGES (from Le Chonk, and the air staff's aircraft orders) are the war
plan: carry them out and keep them running; say in end_turn what you did. They compete
for the same military factories, and you weigh them: the army's divisions must be
equipped first (rifles, support equipment, artillery for the divisions in the field and
in training: a division short of equipment fights badly, and one in training never
finishes). An aircraft order is filled with what is left after that; a land power
usually spends a quarter to a third of its military factories on aircraft. If an order
asks for more than the factories can give, give a fair share and say so to the one who
ordered. Le Chonk's word decides between them.

You decide:
- research (start_research): every research slot must always be busy; an idle slot is
  lost time. A busy slot is left to finish: replace what it researches only when it runs
  ahead of its time or an order asks for it, not to try another plan every turn (a
  technology swapped out finishes nothing). FIRST, whenever a slot frees: a technology
  marked RESEARCH +N% (the computing line: electronic_mechanical_engineering,
  mechanical_computing, computing_machine and after) at x0.75 or better; it speeds up
  every later technology, so the sooner the more it pays. Then what the war needs now and
  what multiplies the economy (industry, production, construction technologies) early. A technology researched ahead of its
  year is slow (xN in AVAILABLE RESEARCH: the speed left after the penalty): x0.75 and
  above is fine; x0.75-0.5 only when it is truly needed and nothing else serves (look
  first for another technology, or a focus that boosts it); below x0.5 do not take it:
  a slot spent a year on one technology is lost to everything else.
- production (set_line_factories, new_line): how many military factories each production
  line gets; factories on no line make nothing. Open a new line (new_line, from OUR
  DESIGNS) when the army needs equipment nobody produces or a better design exists,
  e.g. medium tanks for the armour; old models keep being useful until replaced.
  THE BASE THAT ALWAYS RUNS, whatever the orders: rifles (infantry equipment), artillery
  and support equipment never stand at zero factories; a major power (we are one) also
  always makes trucks, which the supply of the army hangs on; with the resources and the
  factories for it, aircraft and tanks too. Within that, shares follow what the army uses
  (THE ARMY'S EQUIPMENT); a minor line keeps at least 1-2 factories (a line restarted
  from zero loses its production efficiency), and what the divisions hold few of
  (support equipment, anti-air, anti-tank) gets a little more than its count suggests,
  because attrition empties it first. An order "X at any cost" is a share for X, not a
  reason to stop the base: say so to Le Chonk if an order would.
- dockyards and ship lines are the ADMIRALTY's: it builds the navy it commands. Do not
  open, change or remove ship lines; the ship lines in your briefing are for your
  overview of steel and chromium.
- military industrial organisations (the arsenals and firms): every production line,
  ship lines included, gets an organisation that fits it (assign_organisation): its
  traits add to what the line makes. A new line gets one in the same turn. Trait
  points are spent as they come (unlock_trait): production traits (capacity, cost,
  resources) where many factories work, quality traits (attack, armour, reliability)
  for the equipment that decides the battles of the war we plan.
- fuel (FUEL): tanks, lorries, aircraft and ships burn it; with the stock dry they crawl,
  stay grounded or in port. It comes from oil (our states' and imported: buy) and from
  synthetic refineries (build synthetic_refinery: the way for a country without oil);
  fuel silos add storage. A peacetime stock that is full is the reserve for the war, and
  what comes in beyond storage is lost. When the stock falls (a negative daily balance)
  buy oil or build refineries before it runs dry.
- designs: a production line makes one design. When a newer frame (chassis, airframe,
  hull) is researched, the old design keeps being built until someone makes a design on
  the new frame (new_design, NEWER FRAMES WITHOUT A DESIGN) and moves the factories to it
  (new_line, then set_line_factories), then removes the old line (remove_line) so the
  list stays short. Aircraft designs are the air staff's.
- equipment: produce what the army and the air force will use. A full stockpile of
  something no template needs ties up factories the war needs elsewhere.
- resources (buy): a SHORTAGE slows every line that uses the resource. Buy it from a
  country with a surplus (Sellers in the briefing; amount per day, each 8 a day costs
  about one civilian factory), enough to cover the shortage, not more. Trade goes by land
  to neighbours and by sea in convoys otherwise: across a blockade or without convoys it
  is paid for and never arrives; prefer sellers you border. Oil is also fuel: what
  production leaves over becomes fuel, so when FUEL FALLS buy oil even with no oil shortage.
  At war buy oil until the balance is positive with the fleet, the planes and the armies
  fighting (up to a quarter of the civilian factories): fuel is bought, not saved.
- the TRADE LAW holds back its share of what our states mine, sold or not (TRADE LAW in
  the briefing): at war, when a resource is short, ask the minister of politics for
  limited_exports.
- construction (build): civilian factories build. Industrial complexes grow the economy,
  military factories the army; infrastructure and railways carry supply (and the trains
  on them: TRAINS gives the stock against the game's own need; keep 1.2x the need, a
  few military factories on train_equipment before the war and more as the front moves
  away from the capital); forts, coastal
  forts, air bases, anti-air and radars serve the war plan (forts where Le Chonk orders
  the defence). A port is naval_base (the admiralty asks for it), never a dockyard. An
  air base gets a new level only when the wings planned for it need more room than OUR
  AIR BASES shows free. Coastal forts stop only a landing: a side that holds the sea (the
  Allies, with the British and French fleets) builds none; they are what is built
  AGAINST a sea power. Only on Le Chonk's order. Projects that no
  longer serve can be taken out of the queue (cancel_construction). WHEN to turn from civilian to military factories is the main economic
  decision: build civilian factories until about two years before the war you expect
  (in a historical game the history of the Second World War tells when it comes), then
  military factories. A civilian factory pays back only after years; one finished in the
  last two years before the war never does. Do not queue civilian factories that would
  finish after that point. Each project gets at most 15 civilian factories, so the
  queue's front is built first and its tail waits.
  Factory conversion is not an available tool in this department: build queues new
  factories. If a plan requests conversions, report that capability gap and do not
  claim they occurred; the plan must be made executable with the tools you have.

Act through tools; each returns what really happened. Turns are short: fix what is idle,
queue enough to last a month, then end_turn with a sentence of intent.
"""


class Industry(Agent):
    role = "industry"
    max_tokens = 4000
    reasoning = "none"
    budget_s = 90

    def __init__(self, model, llm, journal):
        super().__init__("Industry", model, llm, journal,
                         INDUSTRY_SYSTEM.format(country=COUNTRY["name"]))

    def picture(self, ctx):
        return None

    def tools(self):
        from .economy import BUILDINGS
        from . import economy
        return [
            tool("start_research", "Start a technology (name exactly as in AVAILABLE "
                 "RESEARCH) in a free research slot. A busy slot (its number) is changed only "
                 "with replace=true: the replaced technology keeps its progress but is not "
                 "finished while it waits.",
                 {"tech": {"type": "string"}, "slot": {"type": "integer"},
                  "replace": {"type": "boolean"}}, ["tech"]),
            tool("set_line_ships", "Set how many ships a ship line still builds: a number "
                 "(the line closes when they are done; 1 ends the series with the ship on the "
                 "slip) or endless=true for an endless series.",
                 {"line": {"type": "integer"}, "ships": {"type": "integer"},
                  "endless": {"type": "boolean"}}, ["line"]),
            tool("set_line_factories", "Set how many military factories a production line "
                 "gets (its index in PRODUCTION LINES).",
                 {"line": {"type": "integer"}, "factories": {"type": "integer"}},
                 ["line", "factories"]),
            tool("new_line", "Open a production line for one of OUR DESIGNS (its id) and "
                 "give it factories (dockyards for convoys).",
                 {"design_id": {"type": "integer"}, "factories": {"type": "integer"}},
                 ["design_id", "factories"]),
            tool("new_design", "Create a design on a frame: a PRESET of the game for it (DESIGN "
                 "PRESETS, by role), or the modules of one of our designs (design_id) on a "
                 "newer frame (NEWER FRAMES WITHOUT A DESIGN). Costs experience; then open "
                 "a line for it.", {"frame_id": {"type": "integer"},
                                    "preset": {"type": "string"},
                                    "design_id": {"type": "integer"},
                                    "name": {"type": "string"}},
                 ["frame_id", "name"]),
            tool("prioritize_construction", "Move every queued project of a building "
                 "type to the FRONT of the construction queue (QUEUE ORDER): the civilian "
                 "factories go to it first. For what is needed sooner than what is ahead.",
                 {"building": {"type": "string"}}, ["building"]),
            tool("cancel_construction", "Take projects of a building type out of the "
                 "construction queue (the last queued first). Started projects are never "
                 "taken out: what is begun is finished.",
                 {"building": {"type": "string"}, "count": {"type": "integer"}},
                 ["building", "count"]),
            tool("remove_line", "Remove a production line that has no further use (an "
                 "outdated design, a duplicate). Its factories go idle; a ship line loses "
                 "the hull already built. The lines after it move up by one.",
                 {"line": {"type": "integer"}}, ["line"]),
            tool("assign_organisation", "Put one of our organisations (military industrial "
                 "organisation, LINES WITHOUT AN ORGANISATION) on a production line: its "
                 "traits add their bonuses to the line. Free.",
                 {"line": {"type": "integer"}, "organisation": {"type": "string"}},
                 ["line", "organisation"]),
            tool("unlock_trait", "Spend an organisation's trait point on a trait open to it "
                 "(TRAIT POINTS TO SPEND: organisation, then the trait token).",
                 {"organisation": {"type": "string"}, "trait": {"type": "string"}},
                 ["organisation", "trait"]),
        ] + ([
            tool("market_buy", "Buy equipment another country sells on the ARMS MARKET "
                 "(seller tag, equipment as listed, amount): the seller answers, and we pay "
                 "in civilian factories while the contract runs.",
                 {"seller": {"type": "string"}, "equipment": {"type": "string"},
                  "amount": {"type": "integer"}, "design": {"type": "string"}},
                 ["seller", "equipment", "amount"]),
            tool("market_sell", "Put some of our equipment (a design id from OUR DESIGNS) "
                 "on the arms market, where other countries can buy it (they pay in "
                 "civilian factories).", {"design_id": {"type": "integer"},
                                          "amount": {"type": "integer"}},
                 ["design_id", "amount"]),
        ] if economy.MARKET_READY else []) + [
            tool("repair_factories", "How many civilian factories put repairs of bombed "
                 "buildings before construction (REPAIR FACTORIES: about 5%, at least 1-3).",
                 {"count": {"type": "integer"}}, ["count"]),
            tool("buy", "Import a resource from a seller country (tag), amount per day.",
                 {"seller": {"type": "string"}, "resource": {"type": "string"},
                  "amount": {"type": "integer"}}, ["seller", "resource", "amount"]),
            tool("build", "Queue buildings in one of our states (state id from OUR STATES): "
                 "one of %s; count up to 10. Forts (bunker) go into every province of "
                 "the state on its land border (only the border with `facing`, a country "
                 "tag, if given), coastal forts into its coast; for forts count is the "
                 "levels each gets. For an inland defensive line, choose explicit "
                 "provinces for bunker/coastal_bunker; construction_locations lists "
                 "the state's controlled province ids and terrain." % ", ".join(BUILDINGS),
                 {"state_id": {"type": "integer"}, "building": {"type": "string"},
                  "count": {"type": "integer", "minimum": 1, "maximum": 10},
                  "facing": {"type": "string"}, "provinces": INT_LIST},
                 ["state_id", "building"]),
            tool("construction_locations", "Read the controlled land provinces in one "
                 "of our states: ids, terrain, cities, coasts and last-read fort levels. "
                 "Choose sites yourself, then build with explicit provinces; this queues nothing.",
                 {"state_id": {"type": "integer"}}, ["state_id"]),
            tool("end_turn", "Finish this turn.", {"summary": {"type": "string"}}, ["summary"]),
        ]

    def _air_base_room(self, ctx, state):
        """24.10: Le Chonk read "Kitakyushu: 0/600" (no wings on a level 3 base) as no base
        and ordered a fourth level ahead of the civilian factories. A base with room for
        400 planes or more gets no level unless his order names the level it stands at."""
        from . import air
        b = next((x for x in ctx["gw"].run(air.bases, ctx["holders"], ctx["own"])
                  if x["state"] == int(state)), None)
        if not b:
            return
        free, lvl = b["capacity"] - b["planes"], b["capacity"] // air.CAPACITY_PER_LEVEL
        if free < 400:
            return
        if any("ORDERED" in m_ and "#%d" % int(state) in m_ and "level %d" % lvl in m_
               for m_ in self.memory[-20:]):
            return
        raise ops.OpError(
            "state #%d has an air base of level %d already: %d planes of %d stand on it, room "
            "for %d more (about %d wings of 100). A new level adds only room it does not "
            "need. Tell Le Chonk in your report; if the planned wings truly need more, his "
            "order names the state and 'level %d' and it is built."
            % (int(state), lvl, b["planes"], b["capacity"], free, free // 100, lvl))

    def handle(self, ctx, fn, a):
        from . import economy
        gw = ctx["gw"]
        if ctx.get("admiralty") and fn in ("new_line", "set_line", "set_line_ships",
                                            "remove_line"):
            # 22.10: the dockyards are the admiralty's; it builds the navy it commands
            def naval(g):
                if fn == "new_line":
                    return any(v["id"] == int(a["design_id"]) and
                               v["equipment"].startswith("ship")
                               for v in B_module("industry").variants(g))
                ls = g.production_lines()
                i = int(a["line"])
                return 0 <= i < len(ls) and "Naval" in (ls[i].get("class") or "")
            if gw.run(naval):
                raise ops.OpError("the dockyards and ship lines are the admiralty's: it "
                                  "builds the navy. Leave ship lines to it.")
        if fn == "start_research":
            slot = a.get("slot")
            o = gw.run(economy.branch_closed, int(slot) if slot is not None else None).get(a["tech"])
            if o:
                raise ops.OpError(
                    "%s and %s are mutually exclusive branches (XOR): finishing %s throws away "
                    "%s and every technology after it, research spent for nothing. Stay on the "
                    "branch we chose; take something else." % (a["tech"], o, a["tech"], o))
            sp = gw.run(economy.ahead_speed, a["tech"])
            if sp < economy.SLOW:
                # the user, 19.10: under x0.5, never (15.04.1937: construction3 at x0.23,
                # "the best multiplier", a slot gone for most of a year)
                raise ops.OpError("%s runs at x%.2f ahead of its time: below x%.1f a slot is "
                                  "lost for most of a year. Take a technology of its time "
                                  "(full speed in AVAILABLE RESEARCH)."
                                  % (a["tech"], sp, economy.SLOW))
            if slot is not None and not a.get("replace"):
                # 20.10, 1936: the busy slots were refilled every turn and nothing finished
                busy = {x["slot"]: x for x in gw.run(lambda g: g.research_slots())}
                cur = busy.get(int(slot))
                if cur and cur["researching"] and cur["researching"] != a["tech"]:
                    raise ops.OpError(
                        "slot %d already researches %s (%d%% done%s). A busy slot is left "
                        "to finish; to replace it anyway, call again with replace=true."
                        % (int(slot), cur["researching"], cur.get("percent", 0),
                           ", %d days left" % cur["days_remaining"]
                           if cur.get("days_remaining") is not None else ""))
            return gw.run(economy.research, a["tech"], int(slot) if slot is not None else None)
        if fn == "set_line_factories":
            return gw.run(economy.set_line, a["line"], a["factories"])
        if fn == "repair_factories":
            r = gw.run(economy.set_repair_factories, a["count"])
            if r.get("error"):
                raise ops.OpError(r["error"])
            return r
        if fn == "construction_locations":
            return gw.run(economy.construction_locations, ctx["mv"], ctx["holders"],
                          _state_id(a["state_id"]))
        if fn == "build":
            if a["building"] == "coastal_bunker" and not any(
                    "coastal" in m_.lower() for m_ in self.memory[-20:] if "ORDERED" in m_):
                # the user, 19.10: the Allies own the sea; coastal forts are built against
                # them, not by them (25 Atlantic projects were queued in 1939)
                raise ops.OpError("coastal forts are built only on Le Chonk's order: they stop "
                                  "a landing, and a side that holds the sea (the Allies) has "
                                  "no landing to fear; they are what is built against a sea "
                                  "power. Ask him if you think one is needed.")
            if a["building"] == "air_base":
                self._air_base_room(ctx, _state_id(a["state_id"]))
            if a["building"] == "dockyard" and any(
                    w in m_.lower() for m_ in self.memory[-20:] if "ORDERED" in m_ or
                    "BUILD naval" in m_ for w in ("port", "naval_base", "naval base")):
                # 24.10: "a port at Kitakyushu" was queued as a dockyard
                if ("dockyard", a["state_id"]) not in self.this_turn:
                    self.this_turn.add(("dockyard", a["state_id"]))
                    raise ops.OpError(
                        "a dockyard builds ships; it is no port. A port is naval_base (build "
                        "it with building naval_base: it goes into the state's coastal "
                        "province that has the biggest base). If a dockyard is truly what "
                        "is wanted here, call build again.")
            return gw.run(economy.build, _state_id(a["state_id"]), a["building"],
                          a.get("count", 1), ctx["mv"], ctx["holders"],
                          (a.get("facing") or "").upper() or None, a.get("provinces"))
        if fn == "new_line":
            return gw.run(economy.new_line, a["design_id"], a.get("factories") or 0)
        if fn == "new_design":
            return gw.run(economy.new_design, a.get("design_id"), a["frame_id"], a["name"],
                          a.get("preset"))
        if fn == "prioritize_construction":
            return gw.run(economy.prioritize_construction, a["building"])
        if fn == "cancel_construction":
            return gw.run(economy.cancel_construction, a["building"], a["count"])
        if fn == "set_line_ships":
            # 20.10, 1938.10: the minister flipped four lines endless -> 1 -> 4 -> endless
            # thirty times in one turn and ended it at the step limit
            key = ("ships", int(a["line"]))
            if key in self.this_turn:
                raise Nudge("line %s's series was already set this turn; it stays as set"
                            % a["line"])
            self.this_turn.add(key)
            if a.get("endless"):
                return gw.run(economy.set_ships, a["line"], None)
            if not a.get("ships") or int(a["ships"]) < 1:
                raise ops.OpError("give ships (1 or more) or endless=true")
            return gw.run(economy.set_ships, a["line"], int(a["ships"]))
        if fn in ("assign_organisation", "unlock_trait"):
            from . import mio
            r = gw.run(mio.do_assign, a["line"], a["organisation"]) if fn == "assign_organisation" \
                else gw.run(mio.do_unlock, a["organisation"], a["trait"])
            if r.get("error"):
                raise ops.OpError(r["error"])
            return r
        if fn == "remove_line":
            def ship_line(g):
                """A ship line that may hold a hull: it has dockyards, or it is the only
                line of its design (an empty duplicate, never given a yard, holds none)."""
                ls = g.production_lines()
                i = int(a["line"])
                if not (0 <= i < len(ls) and "Naval" in (ls[i].get("class") or "")):
                    return False
                same = [x for j, x in enumerate(ls) if j != i
                        and x.get("equipment") == ls[i].get("equipment")
                        and x.get("design") == ls[i].get("design")]
                return ls[i].get("factories", 0) > 0 or not same
            if gw.run(ship_line):
                # the user, 20.10: a warship takes long to build and one laid down is worth
                # finishing; the minister removed every ship line, the work of a year with
                # them, and 11 dockyards stood idle
                raise ops.OpError("a ship line is not removed: a ship laid down is finished "
                                  "(the work already in it is lost otherwise). To end the "
                                  "series, set_line_ships with ships=1: the line closes when "
                                  "the ship on the slip is done. To build something else "
                                  "besides, open another ship line (new_line). (An empty "
                                  "duplicate with no dockyard working on it can be removed.)")
            return gw.run(economy.remove_line, a["line"])
        if fn == "market_buy":
            return gw.run(economy.market_buy, a["seller"], a["equipment"], a["amount"],
                          a.get("design"))
        if fn == "market_sell":
            return gw.run(economy.market_sell, a["design_id"], a["amount"])
        if fn == "buy":
            return gw.run(economy.buy, a["seller"], a["resource"], a["amount"],
                          ctx["own"], ctx["enemies"])
        raise ops.OpError("unknown tool %s" % fn)


# ====================================================================== politics
POLITICS_SYSTEM = """You are the minister of state of {country} in Hearts of Iron IV: you
spend the country's POLITICAL POWER. The country's own AI is OFF: no focus is taken, no
law changed and no advisor hired unless you order it. You serve the supreme commander,
Le Chonk: his intent for the war is in your briefing.

You decide:
- the national focus (set_focus): one must always be running; an idle focus slot is lost
  time. Follow the line that serves the war plan: industry and army focuses before
  prestige; avoid branches that start civil wars or change the government unless Le
  Chonk asked for it.
- harmful national spirits: many countries start with spirits that cost political power,
  stability, factories or manpower every day of the game (WHAT ENDS OUR SPIRITS shows
  which focus or decision ends each). Ending them early is worth as much as factories:
  a spirit that costs 20% of everything for three years costs more than any focus gives.
  A political branch that ends them is not a risk to avoid; it is often the first line
  to take. Orders from Le Chonk in MESSAGES come first.
- laws (adopt_law): conscription (manpower), economy (factories and consumer goods),
  trade (resources vs. factories). The game lists which you may adopt now. The player's
  rules:
  * economy: work towards war_economy as the war the plan expects draws near;
    tot_economic_mobilisation when MANPOWER and ENERGY both have room for it.
  * conscription: limited_conscription (2.5% of the people) costs nothing to keep.
    extensive_conscription doubles that for only +10% division training time: take it
    almost always, and stay on limited only while manpower is plainly in surplus (a
    pool of a million at peace is not worth 150 power). The ranks above carry painful
    penalties: take the next one only when the pool truly nears a deficit, about 100-200k
    left while a large army fights, less for a small country, whose pool is always small.
  * trade: a minor with few resources takes free_trade (the resources it gives up hardly
    exist, the bonuses are good); a major with resources chooses by need (export_focus
    and limited_exports keep more of its resources for its own lines).
- advisors and military staff (hire): political advisors, theorist, chiefs of army, navy
  and air force, high command. Each costs political power once and pays every day;
  hire what serves the current war first (army chief and high command for a land war,
  industry advisors to build up).
- decisions (take_decision): only those the game allows now; most cost political power.
- operatives (OPERATIVES): recruit every candidate the agency offers into a free slot
  (recruit_operative), and give each operative a job: first an intelligence network
  (operative_network) in the main enemy's land, one operative a country, the next on the
  next enemy (the navy's enemy across the sea too). It starts in a state with a city of 15
  or more victory points (his capital, his big cities) and spreads from there; distance
  does not matter. A network shows his divisions to our generals and opens operations later.
  OPERATIONS (prepare_operation), once the briefing lists them: infiltrating his civilian
  administration first (it opens the rest), then his army, navy or air force (they show
  his units and plans), capturing his cipher before the war; sabotage, stolen technology
  and coups when they serve THE PLAN. An operation takes the operatives from their network
  until it ends; keep at least one operative on the main enemy's network.
- the intelligence agency (create_agency once, then agency_upgrade one at a time):
  decryption reads the enemy's moves, counter-intelligence guards ours, a department
  against the main enemy's army, navy or air force sharpens what we know of it.
- organisation policies (attach_policy): one policy per military industrial organisation,
  for the ones that work the most lines first: a cheaper or faster production (cost,
  capacity, resources) where the factories are, a quality bonus where the equipment
  decides battles. It binds for half a year: do not change it without a reason.
- cryptology (decrypt), once the agency has its cryptology department: decrypt the
  ciphers of the enemy of the next war, one country at a time (CRYPTOLOGY shows how far
  and how fast). A cipher takes years with the department alone: the decryption upgrades
  are what break it before the war; start early, and move to the next enemy once broken.
- faction goals (set_faction_goal), when we lead a faction: one short-, one medium- and
  one long-term slot. A goal fulfilled gives its reward to the whole faction (shared
  research, military cooperation, bases, initiative); the manifest is the faction's main
  goal, and its fulfilment makes the initiative. Keep every slot filled with a goal the
  faction can reach in its time and that serves the plan; an empty slot earns nothing.
- faction rules (set_faction_rule), when we lead: one rule a group decides who may join,
  who is dismissed, who may declare war or call the faction in, what the peace takes,
  who may take the leadership from us. Each change costs faction initiative (a point,
  earned slowly): change a rule only for a reason of the plan (a war to come, a member to
  keep, the leadership to hold), not to try it.
- our subjects (change_autonomy): a subject one level lower gives us more of its
  manpower and trade, lets us build on its land and can no longer act alone; it may
  grow resentful (its freedom rises toward a higher level again). Lower the ones whose
  manpower, factories or bases the plan needs; a dominion that fights well on its own
  may stay as it is.
- our member upgrade (set_member_upgrade): the share of our manpower gain given to the
  faction's garrison pool; it buys influence with manpower the army may need.

Political power banked is power wasted: it earns nothing while it waits. Keep about 50 in
hand for events, plus the price of what the plan names for the coming weeks; spend the
rest now. An empty slot is the first thing to fill: every political advisor slot, the
theorist, the chiefs of army, navy and air force, the high command, the industrial
concern (INDUSTRIAL CONCERNS), then the cheap decisions that pay (leaving a treaty that
binds our navy, coring states, economy decisions). Choose what serves the war and the
plan; an order of Le Chonk's to hold power names the sum and what it is for. A plan
that names no buy forbids none: the slots under EMPTY ADVISOR SLOTS are free.
A full cabinet does not mean there is nothing to spend power on. Review LAWS and
DECISIONS (including country targets) as well. Routine decisions are yours even if
THE PLAN lists none: compare costs, benefits, deadlines and risks before banking PP.
For country mechanics such as purges, review the available decisions and focus chain;
if a needed reading is missing, report that gap rather than treating every other
decision as blocked. Territorial ultimatums can lead to war: coordinate them with
Le Chonk and the war plan; availability alone is not an order to take one.
You own routine law choices even when THE PLAN names no particular purchase. Compare
the law in force with the available alternatives and their trade-offs for the coming
war. If you retain the current laws and bank power, explain that decision; 'no advisor
slot' and 'the plan names no buy' do not explain ignoring available laws.
Peace alone does not justify a civilian economy: compare the actual LAW COSTS AND
BASE MODIFIERS, including construction, consumer goods, fuel and energy demand.
Economic laws affect preparation years too. Choose the trade-off; the code does not.
Act through tools; each returns what really happened. Turns are short: start a focus if
none runs, spend what is worth spending, then end_turn with a sentence of intent.
"""


class Politics(Agent):
    role = "politics"
    max_tokens = 3000
    reasoning = "none"
    budget_s = 90

    def __init__(self, model, llm, journal):
        super().__init__("Politics", model, llm, journal,
                         POLITICS_SYSTEM.format(country=COUNTRY["name"]))
        self.agency_asked = None

    def _idle_power(self, ctx):
        """A review when surplus power could fill a slot or change an available law,
        else None. 24.10: a flat 250 let the minister sit on 244 beside six empty slots."""
        if getattr(self, "_idle_cache", (None,))[0] == ctx.get("day"):
            return self._idle_cache[1]
        from . import politics
        out = None
        try:
            def look(g):
                return g.politics()["political_power"], politics.empty_slots(g)
            pp, e = ctx["gw"].run(look)
            plan = ctx["planner"].plan_for(self.name) if ctx.get("planner") else ""
            keep = politics.plan_reserve(plan)
            cheap = politics.cheapest_fill(e)
            if cheap is not None and pp - keep >= cheap:
                what = ["%s (from %d PP)" % (s, min(r["cost"] for r in v))
                        for s, v in sorted(e["advisors"].items())]
                if e["concern"]:
                    what.append("industrial concern (from %d PP)"
                                % min(o["cost"] for o in e["concern"]))
                if e["policies"]:
                    what.append("organisation policy for %s (25 PP each)"
                                % ", ".join(e["policies"]))
                out = ("%d political power, %d of it beyond what you keep (%d: 50 for events "
                       "plus the dearest buy THE PLAN names), while these stand empty: %s. "
                       "Fill them now, what serves the war and the plan first (hire, "
                       "hire_concern, attach_policy), or call end_turn again and name the "
                       "order that holds this power and the sum." % (pp, pp - keep, keep,
                                                                     "; ".join(what)))
            elif pp > keep:
                laws = ctx["gw"].run(lambda g: B_module("cabinet").laws(g))
                choices = ["%s: now %s; available %s" %
                           (cat, v.get("now") or "?", ", ".join(v["may_adopt"]))
                           for cat, v in laws.items() if v.get("may_adopt")]
                decisions = ctx["gw"].run(politics.decision_options)
                if choices or decisions:
                    out = ("%d political power; reserve %d. Choices remain even with "
                           "a full cabinet. LAWS: %s. DECISIONS: %s. Review their benefits, "
                           "costs and risks for the war plan. You decide: adopt_law or "
                           "take_decision, or end_turn again and "
                           "explain why retaining the current laws and this much power "
                           "is preferable. The plan naming no buy is not a prohibition."
                           % (pp, keep, "; ".join(choices) or "none", "; ".join(decisions) or "none"))
        except Exception:
            out = None
        self._idle_cache = (ctx.get("day"), out)
        return out

    def before_end(self, ctx):
        # 23.10, the user: two years into the game Japan had no intelligence agency; the
        # minister held 931 political power and never founded it
        idle = self._idle_power(ctx)
        if idle and not {"hire", "hire_concern", "attach_policy", "adopt_law", "take_decision"} & self.this_turn:
            return idle
        if not {"recruit_operative", "operative_network"} & self.this_turn:
            from . import intel
            try:
                o = ctx["gw"].run(intel.operatives)
            except Exception:
                o = None
            idle = [n for n in (o or {}).get("ours", []) if n not in o["orders"]]
            if o and (o["candidates"] or idle):
                return ("%s%s. Do it now, or call end_turn again and say why not." % (
                    "The agency offers candidates (%s): recruit one into the free slot "
                    "(recruit_operative)" % ", ".join(o["candidates"]) if o["candidates"] else "",
                    ("; " if o["candidates"] and idle else "") + (
                        "operatives with no job from us: %s (operative_network in the main "
                        "enemy's capital state or another with a city of 15+ victory points)" % ", ".join(idle) if idle else "")))
        day = ctx.get("day") or 0
        if self.agency_asked is not None and day - self.agency_asked < 30:
            return None
        if {"create_agency", "agency_upgrade"} & self.this_turn:
            return None
        from . import intel
        try:
            st = ctx["gw"].run(intel.status)
        except Exception:
            return None
        self.agency_asked = day
        if not st.get("exists"):
            return ("A reminder, not an order: we have no intelligence agency (create_agency: "
                    "5 civilian factories for 30 days; it reads the enemy's divisions, guards "
                    "our codes, runs operatives). If THE PLAN sets it later, end_turn again "
                    "and say so: that is a full answer.")
        if st.get("built") and not st.get("upgrading") and st.get("can_take"):
            self.agency_asked = day + 60          # upgrades are asked about every 90 days
            return ("A reminder, not an order: the intelligence agency upgrades nothing; it "
                    "could take %s. Each takes civilian factories off construction: if the "
                    "plan puts the factories first, end_turn again and say so."
                    % ", ".join(st["can_take"][:6]))
        return None

    def picture(self, ctx):
        return None

    def tools(self):
        return [
            tool("set_focus", "Start a national focus (key from STARTABLE FOCUSES).",
                 {"key": {"type": "string"}}, ["key"]),
            tool("adopt_law", "Adopt a law the game allows now (name from LAWS).",
                 {"law": {"type": "string"}}, ["law"]),
            tool("hire", "Hire an advisor or staff member (character key and slot from "
                 "ADVISORS YOU MAY HIRE).",
                 {"character": {"type": "string"}, "slot": {"type": "string"}},
                 ["character", "slot"]),
            tool("hire_concern", "Take an industrial concern into its empty slot (key from "
                 "INDUSTRIAL CONCERNS).", {"key": {"type": "string"}}, ["key"]),
            tool("set_faction_goal", "As the faction's leader, put a goal into its slot "
                 "(OUR FACTION'S GOALS lists what the game allows: an empty slot is free; "
                 "naming the goal now in the slot as 'replace' costs faction initiative and "
                 "loses its progress). A goal fulfilled gives its reward to the faction.",
                 {"goal": {"type": "string"}, "replace": {"type": "string"}}, ["goal"]),
            tool("set_faction_rule", "As the faction's leader, put a rule in force (FACTION "
                 "RULES lists what may be set): it replaces the rule of its group; "
                 "erase=true removes a rule instead. Each change costs faction initiative.",
                 {"rule": {"type": "string"}, "erase": {"type": "boolean"}}, ["rule"]),
            tool("change_autonomy", "Lower (or raise, lower=false) one of OUR SUBJECTS' "
                 "autonomy by one level, for political power (the game says if it allows it "
                 "now and why not).", {"tag": {"type": "string"}, "lower": {"type": "boolean"}},
                 ["tag"]),
            tool("set_member_upgrade", "Choose our own member upgrade in the faction (OUR "
                 "MEMBER UPGRADE: the share of our monthly manpower gain given to the "
                 "faction's garrison pool, which raises our influence in it).",
                 {"upgrade": {"type": "string"}}, ["upgrade"]),
            tool("recruit_operative", "Recruit one of the CANDIDATES the agency offers into "
                 "a free operative slot.", {"name": {"type": "string"}}, ["name"]),
            tool("operative_network", "Send one of our operatives to build an intelligence "
                 "network in a country: a state of that country with a city of 15 or more "
                 "victory points (its capital, a big city; the game silently drops any other).",
                 {"name": {"type": "string"}, "country": {"type": "string"},
                  "state": {"type": "integer"},
                  "abandon_operation": {"type": "boolean", "description": "true only to take "
                                        "an operative off an operation she prepares: the "
                                        "operation then lacks her and stalls"}},
                 ["name", "country", "state"]),
            tool("prepare_operation", "Prepare an intelligence operation against a country "
                 "(one from OPERATIONS THE GAME ALLOWS NOW) with as many of our free operatives "
                 "as it needs; it starts by itself when prepared.",
                 {"operation": {"type": "string", "description": "its key, e.g. "
                                "operation_infiltrate_civilian"},
                  "country": {"type": "string"},
                  "operatives": {"type": "array", "items": {"type": "string"}}},
                 ["operation", "country", "operatives"]),
            tool("create_agency", "Found the intelligence agency (once).", {}, []),
            tool("agency_upgrade", "Start an agency upgrade the game allows now (INTELLIGENCE "
                 "AGENCY lists them): departments against a country's army, navy or air "
                 "force, decryption, counter-intelligence... One at a time.",
                 {"upgrade": {"type": "string"}}, ["upgrade"]),
            tool("attach_policy", "Attach a policy to one of our organisations "
                 "(ORGANISATION POLICIES): it costs political power and binds it for months.",
                 {"organisation": {"type": "string"}, "policy": {"type": "string"}},
                 ["organisation", "policy"]),
            tool("decrypt", "Start decrypting a country's ciphers (CRYPTOLOGY), or stop. One "
                 "country at a time: starting another stops nothing by itself, so stop the "
                 "old one first.",
                 {"country": {"type": "string", "description": "tag, e.g. CHI"},
                  "start": {"type": "boolean"}}, ["country", "start"]),
            tool("take_decision", "Take a decision the game allows now.",
                 {"key": {"type": "string"},
                  "target": {"type": "string", "description": "country tag for a targeted "
                             "decision, e.g. EST or an exile's POL; use a listed target"}}, ["key"]),
            tool("end_turn", "Finish this turn.", {"summary": {"type": "string"}}, ["summary"]),
        ]

    def handle(self, ctx, fn, a):
        from . import politics
        gw = ctx["gw"]
        if fn == "set_focus":
            return gw.run(politics.set_focus, a["key"])
        if fn == "adopt_law":
            r = gw.run(politics.adopt_law, a["law"])
            self.this_turn.add(fn)
            return r
        if fn in ("hire", "hire_concern", "attach_policy"):
            self.this_turn.add(fn)
        if fn == "hire":
            return gw.run(politics.hire, a["character"], a["slot"])
        if fn == "hire_concern":
            return gw.run(politics.hire_concern, a["key"])
        if fn == "set_faction_goal":
            from . import faction
            ok, why = gw.run(faction.add, a["goal"], a.get("replace") or None)
            if not ok:
                raise ops.OpError(why)
            return {"faction_goal": a["goal"], "replaced": a.get("replace")}
        if fn == "set_faction_rule":
            from . import faction
            ok, why = gw.run(faction.set_rule, a["rule"], bool(a.get("erase")))
            if not ok:
                raise ops.OpError(why)
            return {"faction_rule": a["rule"], "erased": bool(a.get("erase"))}
        if fn == "change_autonomy":
            lower = a.get("lower", True) is not False
            ok, why = gw.run(lambda g: B_module("diplomacy").change_autonomy(
                g, a["tag"].upper(), reduce=lower))
            if not ok:
                raise ops.OpError(why)
            return {"autonomy": a["tag"].upper(), "lowered" if lower else "raised": True}
        if fn == "set_member_upgrade":
            from . import faction
            ok, why = gw.run(faction.set_upgrade, a["upgrade"])
            if not ok:
                raise ops.OpError(why)
            return {"member_upgrade": a["upgrade"]}
        if fn == "take_decision":
            from . import exile
            if a["key"] in exile.DECISIONS:
                if not a.get("target"):
                    raise ops.OpError("%s targets an exile: give target, e.g. POL" % a["key"])
                r = gw.run(exile.decide, a["key"], a["target"])
                if not r.get("ok"):
                    raise ops.OpError(r.get("error"))
                self.this_turn.add(fn)
                return r
            r = gw.run(politics.decide, a["key"], a.get("target"))
            self.this_turn.add(fn)
            return r
        if fn == "prepare_operation":
            from . import intel
            self.this_turn.add(fn)
            r = gw.run(intel.prepare, a["operation"], a["country"].upper(),
                       list(a.get("operatives") or []))
            if r.get("error"):
                raise ops.OpError(r["error"])
            return r
        if fn in ("recruit_operative", "operative_network"):
            self.this_turn.add(fn)
            from . import intel
            if fn == "recruit_operative":
                r = gw.run(intel.recruit, a["name"])
            else:
                mv, hold = ctx["mv"], ctx["holders"]
                st = int(a["state"])
                ours = {self_tag for self_tag in [ctx["own"]]} | set(gw.run(
                    lambda g: B_module("combat").subject_tags(g)))
                foe = a["country"].upper()
                # 26.10, the user: Moresby was taken off the Germany operation she prepared
                # with Wright and sent to Italy on an old note of the strategist's
                job = intel.OP_ORDERS.get(a["name"])
                if job and not a.get("abandon_operation"):
                    raise ops.OpError(
                        "%s is %s (with %s); sending her elsewhere leaves the operation short "
                        "and it stalls. Send a free operative, or set abandon_operation true "
                        "if dropping the operation is meant." % (
                            a["name"], job, ", ".join(n for n, v in intel.OP_ORDERS.items()
                                                      if v == job and n != a["name"]) or "nobody"))
                # 26.10: the game starts a network only in a state whose largest city has
                # INTEL_NETWORK_MIN_VP_TO_TARGET (15) victory points; its CanExecute passes
                # any state and the order is dropped. The border rule of 23.10 was wrong:
                # it kept the UK's operatives out of Germany for two years.
                if mv.vp_top.get(st, 0) < INTEL_MIN_VP or hold.get(st, (None, None))[1] != foe:
                    good = sorted((sid for sid, (o, c) in hold.items() if c == foe
                                   and mv.vp_top.get(sid, 0) >= INTEL_MIN_VP),
                                  key=lambda x: -mv.vp_top.get(x, 0))
                    raise ops.OpError("%s %s; the game drops such an order. %s's states where a "
                                      "network can start: %s" % (
                                          mv.label(st), "is not %s's" % foe
                                          if hold.get(st, (None, None))[1] != foe
                                          else "has no city of %d victory points" % INTEL_MIN_VP,
                                          foe, ", ".join("%s (%d)" % (mv.label(x), mv.vp_top[x])
                                                         for x in good[:10])
                                          or "none: it has no city of %d victory points" % INTEL_MIN_VP))
                r = gw.run(intel.build_network, a["name"], a["country"].upper(), st)
            if r.get("error"):
                raise ops.OpError(r["error"])
            return r
        if fn == "attach_policy":
            from . import mio
            r = gw.run(mio.do_attach, a["organisation"], a["policy"])
            if r.get("error"):
                raise ops.OpError(r["error"])
            return r
        if fn == "decrypt":
            from . import intel
            r = gw.run(intel.set_decryption, a["country"].upper(), bool(a.get("start", True)))
            if r.get("error"):
                raise ops.OpError(r["error"])
            return r
        if fn == "agency_upgrade":
            # the user, 24.10: a second upgrade with 15 civilian factories building, a third
            # of the construction for intelligence. Over a fifth goes to Le Chonk first.
            from . import economy, intel
            name = str(a.get("upgrade") or "")
            cost = intel.upgrade_info().get(name, (5,))[0]
            free = gw.run(economy.construction_civs)
            words = name.replace("upgrade_", "").replace("_", " ").lower()
            ordered = any("LE CHONK ORDERED" in m and (name.lower() in m.lower()
                                                       or words in m.lower().replace("_", " "))
                          for m in self.memory[-12:])
            if free and cost > UPGRADE_SHARE * free and not ordered:
                asked = getattr(self, "asked_upgrades", set())
                if name not in asked:
                    asked.add(name)
                    self.asked_upgrades = asked
                    ctx["supreme"].inbox.append(
                        "Politics asks: the agency upgrade %s takes %d of about %d civilian "
                        "factories that build (%d%%) for about %d days. Approve with "
                        "order_minister to politics naming it, or let it wait." % (
                            name, cost, free, 100 * cost // free, intel._days("UPGRADE")))
                    ctx["alarms"].append(("supreme", "request from politics"))
                raise ops.OpError(
                    "refused: %s takes %d of about %d civilian factories that build (over a "
                    "fifth of construction); Le Chonk is asked, and it goes ahead when his "
                    "order names it" % (name, cost, free))
        if fn in ("create_agency", "agency_upgrade"):
            self.this_turn.add(fn)
            from . import intel
            r = gw.run(intel.create) if fn == "create_agency" else \
                gw.run(intel.upgrade, a["upgrade"])
            if r.get("error"):
                raise ops.OpError(r["error"])
            return r
        raise ops.OpError("unknown tool %s" % fn)


UPGRADE_SHARE = 0.20      # of the construction factories: above it Le Chonk decides


def _neighbours(mv, hold, ours):
    """'TAG: state, state; ...' for every country whose land touches ours (24.10: China
    touched none of ours, Hebei-Chahar and Mengjiang stood between, and the minister sent
    the operative to the Soviets instead)."""
    nb = {}
    for sid, (o, c) in hold.items():
        if c in ours:
            continue
        if any(b in mv.land and hold.get(mv.state_of.get(b), (None, None))[1] in ours
               for q in mv.land_provinces(sid) for b in mv.adj.get(q, ())):
            nb.setdefault(c, []).append(mv.label(sid))
    return "; ".join("%s: %s" % (k, ", ".join(sorted(v))) for k, v in sorted(nb.items())) \
        or "none"


# ====================================================================== army
ARMY_SYSTEM = """You are the chief of the army staff of {country} in Hearts of Iron IV. The
country's own AI is OFF: nothing is organised or trained unless you order it. You serve
the supreme commander, Le Chonk: his orders to you are in MESSAGES, his intent in the
briefing. He decides the wars and the main effort; you make the army fit them.

You decide:
- the organisation inside the THEATRES Le Chonk draws: armies (create_army in a theatre,
  split_army), army groups under field marshals (form_army_group: an army group's field
  marshal lends his skills to its armies), which armies each group has (transfer_army,
  inside one theatre), commanders (appoint, promote_to_field_marshal: a theatre needs a
  field marshal to command it). No division may stand in no army. The theatres themselves
  and which forces go to which theatre are Le Chonk's: ask him, and tell him how in one
  line (reinforce_army: an army of the receiving theatre, take_from_armies the armies of
  the giving one), not only that you cannot.
- not the air force: the air staff flies all of it, the support of the battles too.
  When an attack needs air cover or close air support, say so to Le Chonk.
- training (train): what to raise and how much. New divisions take months to train;
  trained ones are deployed by themselves, then put them into armies. Training waits for
  equipment: look at the STOCKPILE and what is PRODUCED a day; keep about a tenth of the
  manpower in reserve for losses.
  train creates a NEW finite batch of count divisions by default; repeat=true is an
  explicit infinite conveyor. TRAINING CONVEYORS shows existing series (0=infinite).
  set_training_series(conveyor_id,1) finishes its current batch without more repeats;
  cancel_training removes selected lines now and returns their equipment/manpower.
  Review the force mix, not only total headcount: infantry, armor and motorized have
  different jobs and equipment costs. Use TEMPLATE EQUIPMENT COSTS, stock and daily
  output; agree quantitative mobile force needs with Le Chonk rather than endlessly
  feeding an old infantry conveyor while tanks and trucks sit in stock.
- special forces (SPECIAL FORCES): the room under their limit is always used; train them
  until it is full. The mix follows the war plan (FORCES THE PLAN NEEDS): each kind is
  first raised to what the plan asks of it (its marine and paratrooper battalions; its
  landing divisions are best marine divisions), and the room left over goes to the kind
  the plan leans on most. Never put more into one kind than the plan wants while
  another kind it wants is short: the room is shared, and training cannot be undone.
  Without a plan, the kind follows the country and its wars: marines for a country
  that must land on hostile shores (an island power may put it all into marines),
  mountaineers for a war in mountains and hills, paratroopers to drop on cities and
  strongpoints too hard to walk up to.
  Paratroopers drop only from transport planes: with no transport plane line (TRANSPORT
  PLANES) none is raised and those in training are taken out (cancel_training); ask Le
  Chonk for the line first.
  A kind with no template yet gets one (edit_template with new_name, from a line
  template).
- exercises at war: no army of a theatre drills; only a new army in reserve, and only
  while no theatre needs it (a hole, a request for divisions). Reserves serve first.
- exercises (EXERCISES, exercise): divisions below Regular fight worse. The common
  practice in peace is that every army whose divisions are not yet Regular stands on
  exercises until they are (the game stops them there), which also banks army
  experience for doctrines and templates. It costs fuel and some equipment, and a
  drilling division has only 20% organisation: at war, only armies far from an enemy
  at war with us (a quiet theatre, the reserve) drill.
- supply (SUPPLY in the briefing): motorize the hubs behind a line whose armies run short
  (motorize_hub), with trucks from the stockpile that the motorised divisions also need.
  The marshals fight with the supply they get; feeding the theatres is yours. New
  railways, hubs and ports are built by the industry minister: when a line needs one,
  ask Le Chonk for it in your report, naming the place.

- armies: grow the ones we have (reinforce_army, up to the army limit) before forming new
  ones; a new army needs a general (an army without one fights worse) and a field marshal
  leads only so many (LIMITS). Divisions in no army, or in leaderless armies, go into
  armies with room.
- templates (edit_template): army experience buys division templates. The game's numbers
  (1.19 files): a battalion of infantry, motorised, mechanised, tanks, marines,
  mountaineers or paratroopers is 2 wide, line artillery or rockets 3, anti-tank and
  anti-air 1; support companies take no width (up to 5). A province's front: plains,
  desert and hills 70 (+35 for each further direction we attack from), forest and jungle
  60 (+30), mountains and marsh 50 (+25), cities 80 (+40). Divisions over that width fight
  worse (-1% per % over, down to -33%), so pick the width that fills the theatre's ground:
  * width 24 fits mountains, marsh and cities (2 or 3 in 50/75, 5 in 120) and plains well
    (4 in 105);
  * width 18 or 22 fits forest and jungle (5 x 18 or 4 x 22 in 90) and plains (4 x 18 in
    70, 3 x 22 in 70);
  * there is no single right width since the fronts stopped dividing by 20: choose per
    theatre (its main terrain) and keep a few standard templates, not one per army.
  REFERENCE TEMPLATES (the current common practice; adapt, do not copy blindly):
  * infantry, line: 6 infantry + 2 artillery (18), 8 + 2 (22) or 9 + 2 (24); support
    engineer and artillery, then recon, anti-air when the enemy rules the air, logistics
    where supply is short;
  * holding or colonial infantry for quiet fronts: about 5 infantry (10), support
    engineer and artillery: cheap, it holds a line nobody attacks;
  * armour for the blow: tanks with motorised or mechanised infantry, e.g. 6 tanks + 6
    motorised (24) or 5 + 6 (22); support engineer, recon, maintenance, logistics;
  * motorised: like the line infantry with motorised battalions (6-8 + 2), to exploit a
    break and plug holes; towed artillery slows it to foot speed, so keep its firepower
    in rockets or in the support companies, or accept the speed;
  * mountaineers for mountains and hills: 9 mountaineers + 2 artillery (24) or 8 + 3 (25);
  * marines for landings, rivers and marsh: 9 marines (18) or 9 + 2 artillery (24);
  * paratroopers: 6-8 paratroopers (12-16) with light support: they drop, then hold.
  Free choice beyond these: special divisions for a theatre (a light desert division for
  Africa with logistics and few heavy battalions, a garrison or police division for
  occupied land, a cavalry screen) and the support companies, which depend on the
  country, its research and the war. A support company or battalion we have not
  researched is refused: build from what we have.
  * a template is what its battalions are, not its name: when the battalion a kind needs
    (marines, mountaineers, paratroopers) is not researched yet, wait for it (or ask Le
    Chonk for the research); an infantry copy under that name is only another infantry
    template. Remove templates no division uses and nobody plans to raise
    (remove_template).
  * every battalion needs its equipment: a template wanting artillery or tanks the
    factories do not make leaves its divisions short, and changing a template re-equips
    all its divisions, so change the big ones rarely and with the stockpile in mind. A
    new template made from an old one (new_name) leaves the old divisions as they are.
- army experience, in this order: (1) bring the templates we field and train to the
  standard (TEMPLATES BELOW THE STANDARD in the briefing: width for the theatre's ground
  and at least engineer plus one more support company); a special-forces division of 3-4
  battalions is a brigade that loses its fights; (2) doctrines, land and special forces.
- special forces doctrine (DOCTRINES special_forces, paid in army experience): its grand
  doctrine and tracks make our marines, mountaineers and paratroopers better and let us
  keep more of them. Take one once we field special forces and the templates are done.
- doctrines (adopt_doctrine, DOCTRINES): army experience buys them. A subdoctrine in an
  EMPTY track starts earning mastery and its rewards from the day it is slotted, so an
  empty track is lost time; fill tracks as the experience comes. Choose for the war we
  will fight (a defensive infantry war wants defence and firepower, a war of movement
  wants armour and operations). Switching the grand doctrine is a big step: only for a
  reason. Commonly the strongest land grand doctrine is Superior Firepower; the one we
  start with is cheaper to keep.

The field marshals fight their theatres: the sectors, the attacks and their timing are
theirs.
Report to Le Chonk once a turn (report_to_supreme) when you carried out his order or
need his decision. Act through tools; each returns what really happened. Finish every
turn with end_turn: a sentence of what you did and why.
"""


class Army(Supreme):
    """The army staff: organisation and training for the supreme."""
    role = "army"
    max_tokens = 4000
    reasoning = "none"
    think_s = None
    budget_s = 90

    def __init__(self, model, llm, journal):
        Agent.__init__(self, "Army staff", model, llm, journal, ARMY_SYSTEM.format(
            country=COUNTRY["name"], tactical=", ".join(sorted(TACTICAL_MISSIONS)))
            + doctrine.SUPREME)

    def picture(self, ctx):
        return None

    def tools(self):
        org = [t for t in self._all_tools() if t["function"]["name"] in ORG_TOOLS]
        return org + [
tool("adopt_doctrine", "Take a doctrine of your folders (DOCTRINES land and special_forces): a subdoctrine for an "
                 "EMPTY track, or another grand doctrine; by its name. Costs experience.",
                 {"name": {"type": "string"}}, ["name"]),
            tool("edit_template", "Change a division template, or make a new one from it "
                 "(new_name): its battalion columns (up to 5 columns of up to 5 battalion "
                 "keys, e.g. [[\"infantry\",\"infantry\",\"infantry\"],[\"artillery_brigade\"]]) and "
                 "support companies (up to 5 keys). Costs army experience (the game's own "
                 "price); every division of the template re-equips to it.",
                 {"template": {"type": "string"},
                  "columns": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                  "support": {"type": "array", "items": {"type": "string"}},
                  "new_name": {"type": "string"}}, ["template", "columns"]),
            tool("exile_template", "Copy one of OUR TEMPLATES for a government in exile we "
                 "host (GOVERNMENT IN EXILE WE HOST): its divisions draw that country's "
                 "veteran manpower, not ours, and fight harder against its occupier. Free; "
                 "then train it.",
                 {"template": {"type": "string"}, "country": {"type": "string"},
                  "new_name": {"type": "string"}}, ["template", "country", "new_name"]),
            tool("remove_template", "Delete one of OUR TEMPLATES that no division uses and "
                 "no plan needs (a leftover, a duplicate).", {"template": {"type": "string"}},
                 ["template"]),
            tool("cancel_training", "Take divisions of a template out of training (the least "
                 "trained first): their equipment and manpower go to the others. Optional "
                 "conveyor_id selects only that conveyor.",
                 {"template": {"type": "string"}, "count": {"type": "integer", "minimum": 1},
                  "conveyor_id": {"type": "integer"}},
                 ["template", "count"]),
            tool("train", "Start a NEW finite batch of count divisions of OUR TEMPLATES. "
                 "Existing conveyors stay unchanged. Only repeat=true makes it infinite. "
                 "Optional deployment_province overrides the capital deployment location.",
                 {"template": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": 24},
                  "repeat": {"type": "boolean"}, "deployment_province": {"type": "integer"}},
                 ["template", "count"]),
            tool("set_training_series", "Change a training conveyor's total starts per line. "
                 "0 repeats forever; 1 finishes currently training divisions and stops "
                 "future repeats. Does not cancel current divisions or return equipment.",
                 {"conveyor_id": {"type": "integer"},
                  "series": {"type": "integer", "minimum": 0}}, ["conveyor_id", "series"]),
            tool("motorize_hub", "Motorize one of our hubs behind a line (SUPPLY, p<id>): "
                 "level 2 full (60 trucks from the stockpile), 1 partial, 0 off. Do it where "
                 "an army's supply runs low and trucks lie in stock: the trucks leave the "
                 "stockpile, so weigh them against the motorised divisions.",
                 {"province": {"type": "string"},
                  "level": {"type": "integer", "minimum": 0, "maximum": 2}},
                 ["province", "level"]),
            tool("exercise", "Start (on=true) or stop the exercises of armies (EXERCISES): "
                 "their divisions drill up to Regular and stop by themselves there, and the "
                 "country banks army experience. While drilling a division holds only 20% "
                 "organisation, burns fuel and wears equipment: never an army facing an "
                 "enemy we are at war with.",
                 {"armies": INT_LIST, "on": {"type": "boolean"}}, ["armies", "on"]),
            tool("report_to_supreme", "One report to Le Chonk per turn.",
                 {"message": {"type": "string"}}, ["message"]),
            *__import__("harness.projects", fromlist=["tools"]).tools("land"),
            tool("end_turn", "Finish this turn.", {"summary": {"type": "string"}}, ["summary"]),
        ]

    def handle(self, ctx, fn, a):
        from . import army
        gw, mv = ctx["gw"], ctx["mv"]
        if fn in ("start_special_project", "assign_scientist"):
            from . import projects
            r = projects.handle(gw, fn, a, "specialization_land")
            if r.get("error"):
                raise ops.OpError(r["error"])
            return r
        if fn == "exercise":
            ids, on = [int(x) for x in a["armies"]], bool(a.get("on", True))
            if on and ctx["enemies"]:
                contacts = gw.run(ops.exercise_front_contacts, mv, ids, ctx["enemies"])
                if contacts:
                    raise ops.OpError("refused: armies facing a current enemy cannot drill "
                                      "at 20%% organisation: %s; stop exercises or select "
                                      "reserve armies away from the hostile border" % "; ".join(
                                          "army %d facing %s" % (aid, ", ".join(tags))
                                          for aid, tags in contacts.items()))
                # the user, 24.10: at war no army of a theatre drills; only new armies in
                # reserve, and only while no theatre needs them
                def placed(g):
                    out = []
                    for i, og in zip(ids, ops._ogs(g, ids)):
                        m = ctx["marshals"].get(ops.theatre_of(g, og))
                        if m:
                            out.append((i, m.theatre["name"]))
                    return out
                bad = gw.run(placed)
                if bad:
                    raise ops.OpError("refused: at war an army of a theatre does not drill "
                                      "(%s); a drilling army holds 20%% organisation. Only "
                                      "armies in reserve, in no theatre, drill at war, and only "
                                      "while no theatre needs them" % "; ".join(
                                          "army %d in %s" % b for b in bad))
            return gw.run(ops.exercise, ids, on)
        if fn == "motorize_hub":
            from . import front
            q, lvl = int(str(a["province"]).lstrip("pP")), int(a["level"])
            if q not in mv.supply_nodes or ctx["holders"].get(
                    mv.state_of.get(q), (None,))[0] != ctx["own"]:
                raise ops.OpError("p%d is not a supply hub of ours; pick one from SUPPLY"
                                  "" % q)
            r = gw.run(lambda g: g.set_hub_motorization(q, lvl))
            if r.get("ok") is False:
                raise ops.OpError(str(r.get("error")))
            now = gw.run(lambda g: g.hub_motorization()).get(q, 0)
            front.MOTORIZED[q] = now
            if now != lvl:
                return ("hub p%d: motorization %d ordered, the game still shows %d; it may "
                        "apply on the next tick (check SUPPLY next turn)" % (q, lvl, now))
            return "hub p%d: motorization level %d set" % (q, lvl)
        if fn == "train":
            prov = a.get("deployment_province")
            if prov is None:
                prov = ctx["gw"].run(army.home_province, ctx["mv"])
            return ctx["gw"].run(army.train, a["template"], a["count"],
                                 a.get("repeat", False), prov)
        if fn == "cancel_training":
            return ctx["gw"].run(army.cancel_training, a["template"], a["count"],
                                 a.get("conveyor_id"))
        if fn == "set_training_series":
            return ctx["gw"].run(army.set_training_series, a["conveyor_id"], a["series"])
        if fn == "exile_template":
            from . import exile
            r = ctx["gw"].run(exile.make_template, a["template"], a["country"], a["new_name"])
            if not r.get("ok"):
                raise ops.OpError(r.get("error"))
            return r
        if fn == "remove_template":
            return ctx["gw"].run(army.remove_template, a["template"])
        if fn == "edit_template":
            return ctx["gw"].run(army.edit_template, a["template"], a["columns"],
                                 a.get("support") or [], a.get("new_name"))
        if fn == "adopt_doctrine":
            from . import doctrine_files
            r = ctx["gw"].run(doctrine_files.adopt_any, ("land", "special_forces"), a["name"])
            if not r.get("ok"):
                raise ops.OpError(r.get("error"))
            return r
        if fn == "report_to_supreme":
            if self.reported:
                raise Nudge("one report to Le Chonk per turn; this one was not sent")
            self.reported = True
            ctx["supreme"].inbox.append("Army staff: " + a["message"])
            return {"delivered": True}
        if fn in ORG_TOOLS:
            return Supreme.handle(self, ctx, fn, a)
        raise ops.OpError("unknown tool %s" % fn)


# ====================================================================== strategic air
AIR_SYSTEM = """You command the whole air war of {country} in Hearts of Iron IV. The
country's own AI is OFF: idle wings do nothing. You serve the supreme commander, Le
Chonk: his intent and his orders to you are in the briefing and MESSAGES.

You decide where every wing flies (air_mission), where it is based (rebase_air) and
which army it supports (attach_air_to_army):
- over the battles (THE ARMIES' BATTLES): fighters for air superiority over the regions
  where our armies attack or are attacked, close air support and tactical bombers on the
  enemy divisions in battle ({tactical}); attaching wings to an attacking army is the
  simplest way, they rebase near it and follow its battles by themselves. An attack
  without air superiority over it loses its close air support to enemy fighters;
- interception over OUR HEARTLAND against enemy bombers;
- strategic bombing of the enemy's HEARTLAND (factories, infrastructure) when the
  bombers have the range and fighter cover; naval bombing and port strikes against
  enemy fleets ({strategic}).
Our own sky: where the enemy raids our land (bombers over our states, the alarm names the region), fighters fly interception over it; air superiority fights his fighters. When a theatre begins an attack, its air comes first. Wings on a carrier are the admiralty's; a carrier wing on a land base is yours.
THE SKY BY REGION is where you read the air war: in each region his fighters and raiders
against our fighters and strike planes, and the land battles under it. Air power is won
by concentration: one region where we outnumber him beats three where we are thin, and a
wing over an EMPTY SKY (no enemy plane, no battle) fights nobody. Put the fighters where
he flies over what matters most (our own land where he is OUTNUMBERING us first: the
homeland's sky and its factories), and over our own attacks, where he does not fly yet
too; leave thin cover or none where nothing happens. concentrate_air moves several wings
in one step (it rebases them when out of range); air_mission and rebase_air for single
wings.

You also decide what the air force IS: Le Chonk sets its goal (a fighter defence that
holds our sky, close air support for the army, strategic bombers against the enemy's
industry, naval bombers against his fleets) and you order the aircraft from the minister
of industry (order_aircraft): which of OUR AIRCRAFT DESIGNS, how many factories, for
what. Air power is won by numbers: an air force that is not produced does not exist when
the war comes, and a country with many fewer fighters than its enemy loses its sky. A
newer airframe researched is useless until you make a design on it (new_design: our
design's weapons on the new airframe, paid in air experience) and order it built. But
the army's rifles and guns come first from the same military factories: ask for a share
the industry can bear (a land power gives the air force about a quarter to a third of
its military factories), not for all of them.

Air experience comes from pilot exercises (air_mission training: the wing trains, the
country banks air experience; it burns fuel and loses a few planes to accidents) and from
combat. At war the wings that fight do not train: only wings new since the war began,
and only while no mission needs them. Without it no new design can be made and no doctrine bought: when a newer
airframe waits for a design and the experience is short, do not wait for it: farm it.
The experience banked grows with the planes on training, so one small wing banks next to
nothing; put several fighter wings (hundreds of planes) on training until it is banked,
and take them back to their mission after. Training also lifts the pilots themselves up
to Regular (300 of 1000 xp a plane). It burns fuel (out of fuel it gives no experience),
and the country stores at most 500 air experience.
Aircraft in reserve fly no missions. AIRCRAFT RESERVES gives exact design IDs and
counts; DEPLOYMENT AIR BASES gives free space. Use create_air_wing to form a new wing
from the reserve: choose its base, design and 1-100 planes, then give the returned wing
a mission/training or attach it to an army. Keep the reinforcement reserve you judge
necessary. Creating a wing does not choose its mission for you.
Air experience buys air doctrines (adopt_doctrine, DOCTRINES): with no grand doctrine
the air force fights without one; take one as soon as the experience pays (for a war of
holding our sky over the army: Battlefield Support or Operational Integrity).
Act through tools; each returns what really happened. Turns are short: give the idle
wings a mission, order the aircraft the goal needs, end_turn.
"""


class AirStaff(Supreme):
    role = "air"
    max_tokens = 3000
    reasoning = "none"
    think_s = None
    budget_s = 90
    air_role = "air staff"
    air_missions = None             # all of them

    def __init__(self, model, llm, journal):
        Agent.__init__(self, "Air staff", model, llm, journal, AIR_SYSTEM.format(
            country=COUNTRY["name"], strategic=", ".join(sorted(STRATEGIC_MISSIONS)),
            tactical=", ".join(sorted(TACTICAL_MISSIONS))))

    def picture(self, ctx):
        return None

    def tools(self):
        return [x for x in self._all_tools() if x["function"]["name"] in AIR_TOOLS] + [tool("create_air_wing", "Create one air wing at a chosen controlled base from an exact "
                 "reserve design. The new wing has no mission; then give its returned "
                 "ID a mission, training or army attachment.",
                 {"state_id": {"type": "integer"}, "design_id": {"type": "integer"},
                  "planes": {"type": "integer", "minimum": 1, "maximum": 100}},
                 ["state_id", "design_id", "planes"]), tool("adopt_doctrine", "Take a doctrine of your folder (DOCTRINES): a subdoctrine for an "
                 "EMPTY track, or another grand doctrine; by its name. Costs experience.",
                 {"name": {"type": "string"}}, ["name"]), tool("new_design", "Create a design on a frame: a PRESET of the game for it (DESIGN "
                 "PRESETS, by role), or the modules of one of our designs (design_id) on a "
                 "newer frame (NEWER FRAMES WITHOUT A DESIGN). Costs experience; then open "
                 "a line for it.", {"frame_id": {"type": "integer"},
                                    "preset": {"type": "string"},
                                    "design_id": {"type": "integer"},
                                    "name": {"type": "string"}},
                 ["frame_id", "name"]), tool("request_air_defence", "Ask the industry for radar_station or "
                      "anti_air_building in one of our states (bombed, or under his raids) "
                      "and why. One request per turn.",
                      {"building": {"type": "string", "enum": ["radar_station",
                                                               "anti_air_building"]},
                       "state": {"type": "string"}, "why": {"type": "string"}},
                      ["building", "state", "why"]), tool("order_aircraft", "Order aircraft from the minister of industry: "
                        "which designs (ids from OUR AIRCRAFT DESIGNS), how many military "
                        "factories, and why. One order per turn.",
                        {"order": {"type": "string"}}, ["order"]),
                tool("concentrate_air", "Send wings of one kind to fly a mission over a "
                     "region in one step (THE SKY BY REGION): the tool takes idle wings "
                     "first, then those over the skies that need them least (no enemy plane, "
                     "no battle of ours), never wings attached to an army, and rebases them "
                     "to our air base in the region when out of range. You choose the region "
                     "(over our own attack too, where he does not fly yet); take_from limits "
                     "the regions wings may be taken from.",
                     {"region": {"type": "integer"},
                      "kind": {"type": "string", "enum": ["fighter", "heavy fighter",
                                                          "close air support",
                                                          "tactical bomber", "strategic bomber",
                                                          "naval bomber"]},
                      "wings": {"type": "integer", "minimum": 1},
                      "mission": {"type": "string"},
                      "take_from": {"type": "array", "items": {"type": "integer"}}},
                     ["region", "kind", "wings", "mission"]),
                *__import__("harness.projects", fromlist=["tools"]).tools("air"),
                tool("end_turn", "Finish this turn.", {"summary": {"type": "string"}},
                     ["summary"])]

    def before_end(self, ctx):
        """27.10: the air facility stood idle for years with three air scientists free."""
        created = {x[1] for x in self.this_turn
                   if isinstance(x, tuple) and x[0] == "created_wing"}
        if created:
            idle = [w["id"] for w in ctx["gw"].run(air.wings)
                    if w["id"] in created and not w["missions"] and not w["army"]]
            if idle:
                return ("The wings were created and their real IDs are already available: %s. "
                        "They have no mission or army attachment. Give them a mission, training "
                        "or an army now, or call end_turn again and explain why they should "
                        "remain idle. Do not wait for IDs on a later turn." % ", ".join(map(str, idle)))
        first = Supreme.before_end(self, ctx)
        if first:
            return first
        st = ctx.get("projects_stuck")
        if st and not {"called:start_special_project", "called:assign_scientist"} & self.this_turn:
            return "Not yet: the air special projects stand still: %s." % st
        return None

    def handle(self, ctx, fn, a):
        self.this_turn.add("called:" + fn)
        if fn == "create_air_wing":
            r = ctx["gw"].run(air.create_wing, int(a["state_id"]), int(a["design_id"]),
                               a["planes"], ctx["holders"], ctx["own"])
            if not r.get("ok"):
                raise ops.OpError(r.get("error") or "air wing deployment not confirmed")
            for wing in r["created_wings"]:
                ctx.setdefault("air_owner", {})[wing] = self.air_role
                # Creation gives no mission. Reserve the wing against repeated
                # retasking only after its first actual mission/attachment.
                self.this_turn.add(("created_wing", wing))
            return r
        if fn == "request_air_defence":
            if "request_air_defence" in self.this_turn:
                raise Nudge("one air defence request per turn; this one was not sent")
            self.this_turn.add("request_air_defence")
            text = "BUILD %s in %s: %s" % (a["building"], a["state"], a["why"])
            if ctx.get("industry"):
                return deliver(ctx, "Air staff", "industry", text)
            ctx.setdefault("player_requests", []).append(text)
            return {"sent_to": "the player, who runs construction"}
        if fn == "concentrate_air":
            regions = briefing_air_regions(ctx["mv"])
            rid = int(a["region"])
            if rid not in regions.name:
                raise ops.OpError("%r is not a strategic region (THE SKY BY REGION)" % a["region"])
            try:
                out = ctx["gw"].run(air.concentrate, regions, rid, a["kind"], a["wings"],
                                    a["mission"], ctx["holders"], ctx["own"],
                                    sorted(ctx["enemies"]), self._not_mine(ctx),
                                    [int(x) for x in a.get("take_from") or []] or None)
            except ValueError as e:
                raise ops.OpError(str(e))
            for w in out.get("flying_it", []):
                ctx.setdefault("air_owner", {})[w] = self.air_role
                self.this_turn.add(("wing", w))
            return out
        if fn in ("start_special_project", "assign_scientist"):
            from . import projects
            r = projects.handle(ctx["gw"], fn, a, "specialization_air")
            if r.get("error"):
                raise ops.OpError(r["error"])
            return r
        if fn == "order_aircraft":
            if "order_aircraft" in self.this_turn:
                raise Nudge("one order to the industry per turn; this one was not sent")
            if not a.get("order"):
                raise ops.OpError("the order is empty")
            # 09.01.1938: three months of "D.510 (1574)" with D.520 beside it, OUTDATED
            # in the briefing; the order came from the staff's own notes
            old = ctx["gw"].run(lambda g: [
                (v["id"], v["design"], newer) for v in B_module("industry").variants(g)
                for i, newer in B_module("industry").outdated(g).items() if v["id"] == i])
            hit = [(i, d, n) for i, d, n in old
                   if (str(i) in a["order"] or (d and d in a["order"])) and n not in a["order"]]
            if hit and "outdated" not in self.this_turn:
                self.this_turn.add("outdated")
                raise Nudge("the order names %s; %s. Order the newer design instead (OUR "
                            "AIRCRAFT DESIGNS), or send it again if the old one is meant."
                            % (", ".join("%s (%d)" % (d, i) for i, d, _ in hit),
                               "; ".join("%s is newer than %s" % (n, d) for _, d, n in hit)))
            self.this_turn.add("order_aircraft")
            return deliver(ctx, "Air staff", "industry", a["order"])
        if fn in AIR_TOOLS:
            return Supreme.handle(self, ctx, fn, a)
        if fn == "adopt_doctrine":
            from . import doctrine_files
            r = ctx["gw"].run(doctrine_files.adopt, "air", a["name"])
            if not r.get("ok"):
                raise ops.OpError(r.get("error"))
            return r
        if fn == "new_design":
            from . import economy
            return ctx["gw"].run(economy.new_design, a.get("design_id"), a["frame_id"],
                                 a["name"], a.get("preset"))
        raise ops.OpError("unknown tool %s" % fn)
