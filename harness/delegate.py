"""Hand one branch of the country to the model while the player plays the rest.

The user, 22.10: many players leave the navy alone; the player runs the country and the
admiralty runs the fleets and the dockyards. The game is never paused by us: the player
plays at their own speed, and the staff takes a turn every few game days.

usage: python3 -m harness.delegate navy [--models haiku-sub] [--every 7]
                                        [--intent "..."] [--dry]
"""
import argparse
import os
import time

from . import admiralty, agents, navy
from .agents import Journal
from .bridge import Gateway
from .bridge import module as B
from .llm import LLM, ROOT, load_env
from .mapview import MapView
from .run import CHEAP, LECHONK, SUBSCRIPTION, day_of


def context(gw, mv, sea, intent):
    d = gw.run(lambda g: g.date())
    return {"gw": gw, "mv": mv, "sea": sea, "own": agents.COUNTRY["tag"],
            "date": d["text"], "day": day_of(d), "holders": gw.run(mv.holders),
            "enemies": gw.run(lambda g: B("combat").enemy_tags(g)),
            "player_intent": intent, "alarms": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("branch", choices=["navy"])
    ap.add_argument("--models", choices=sorted(CHEAP) + ["lechonk"], default="haiku-sub")
    ap.add_argument("--every", type=int, default=7, help="game days between turns")
    ap.add_argument("--intent", default="", help="what the player wants from the branch")
    ap.add_argument("--dry", action="store_true", help="print the briefing and stop")
    ap.add_argument("--turns", type=int, default=0, help="stop after this many turns (0: never)")
    args = ap.parse_args()
    from .ownership import acquire
    writer_lock = acquire()
    os.set_inheritable(writer_lock, False)
    load_env()
    model = LECHONK if args.models == "lechonk" else CHEAP[args.models]
    provider = "mistral" if args.models == "lechonk" else ("claude" if args.models in SUBSCRIPTION else "openrouter")
    gw = Gateway()
    mv = MapView()
    sea = navy.SeaRegions(mv)
    info = gw.run(lambda g: B("session").info(g))
    agents.COUNTRY.update(name=info.get("my_country") or info["my_tag"], tag=info["my_tag"])
    if args.dry:
        print(admiralty.briefing(context(gw, mv, sea, args.intent)))
        return
    journal = Journal(os.path.join(ROOT, "logs", "delegate-%s-%d.jsonl"
                                   % (args.branch, time.time())))
    staff = admiralty.Admiralty(model, LLM(provider), journal)
    last = None
    print("the admiralty of %s takes the navy; a turn every %d game days"
          % (agents.COUNTRY["name"], args.every), flush=True)
    try:
        while True:
            ctx = context(gw, mv, sea, args.intent)
            if last is None or ctx["day"] - last >= args.every:
                last = ctx["day"]
                text = admiralty.briefing(ctx)
                journal.write(agent=staff.name, date=ctx["date"], briefing=text)
                print("%s admiralty turn" % ctx["date"], flush=True)
                summary = staff.turn(ctx, text)
                print("  -> %s" % (summary or "(no summary)"), flush=True)
                for r in ctx.get("player_requests", []):
                    print("  !! THE ADMIRALTY ASKS YOU: %s" % r, flush=True)
                done = locals().get("done", 0) + 1
                if args.turns and done >= args.turns:
                    break
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        gw.close()


if __name__ == "__main__":
    main()
