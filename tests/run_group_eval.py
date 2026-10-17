"""Group-filter accuracy against the REAL model (needs Azure OpenAI in .env).
Feeds samples/group_feed.json through groups.ingest on a throw-away database and compares each outcome with
the expected one. Add real (anonymised) group traffic to the sample file to measure precision/recall properly.

    .venv\\Scripts\\python tests\\run_group_eval.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["WA_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "g.db")
from app import groups, llm, seed  # noqa: E402

if not llm.configured():
    sys.exit("Azure OpenAI not configured: fill in .env first.")

seed.run(reset=True)
feed = json.load(open(os.path.join(os.path.dirname(__file__), "..", "samples", "group_feed.json"), encoding="utf-8"))
want = {"ignore": {"ignored"}, "requirement": {"surfaced"}, "listing": {"surfaced"}, "duplicate": {"duplicate"}}
ok = tp = fp = fn = llm_calls = 0
for m in feed:
    r = groups.ingest("rohit", m["group"], m["sender"], m["text"])
    good = r["outcome"] in want[m["expect"]]
    ok += good
    surfaced, should = r["outcome"] == "surfaced", m["expect"] in ("requirement", "listing")
    tp += surfaced and should
    fp += surfaced and not should
    fn += (not surfaced) and should
    print(f'{"✓" if good else "✗"} expected {m["expect"]:11} got {r["outcome"]:9} | {m["text"][:62]!r}')
    if not good and r.get("alert"):
        print("    alert:", r["alert"][:120])
print(f"\nOutcome accuracy {ok}/{len(feed)} = {100 * ok / len(feed):.0f}%   "
      f"precision {tp}/{tp + fp}   recall {tp}/{tp + fn}")
