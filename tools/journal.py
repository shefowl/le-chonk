"""Print a run journal as a readable feed: who woke, why, what they did, what happened.

usage: journal.py [logs/run-XXXX.jsonl]   (default: the newest)
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else max(
        glob.glob(os.path.join(ROOT, "logs", "run-*.jsonl")), key=os.path.getmtime)
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        who, d = r.get("agent", ""), r.get("date", "")
        if "wake" in r:
            print("\n== %s %s wakes: %s" % (d, who, r["wake"]))
        elif "event" in r:
            print("** %s %s" % (r["event"], json.dumps(r.get("theater"), ensure_ascii=False)))
        elif "action" in r:
            o = r["outcome"]
            res = o.get("result") if o.get("ok") else ("REFUSED " + str(o.get("refused") or o.get("error")))
            print("   %s -> %s %s\n      = %s" % (who, r["action"],
                  json.dumps(r["args"], ensure_ascii=False)[:400],
                  json.dumps(res, ensure_ascii=False)[:400]))
        elif r.get("thought") or r.get("reasoning"):
            if r.get("reasoning"):
                print("   %s reasons (%.1fs): %s" % (who, r.get("latency_s", 0),
                                                  r["reasoning"][:800].replace("\n", " ")))
            if r.get("thought"):
                print("   %s says: %s" % (who, str(r["thought"])[:600]))
        elif "error" in r:
            print("!! %s %s" % (who, r["error"]))


if __name__ == "__main__":
    main()
