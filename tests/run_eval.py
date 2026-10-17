"""Accuracy harness: runs samples/messages.json through the REAL model + normaliser and scores
field-level accuracy against the `expect` blocks. Needs Azure OpenAI configured in .env.
Add the client's real (anonymised) broker messages to samples/messages.json to measure the 90% target.

    .venv\\Scripts\\python tests\\run_eval.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import llm, normalize as N  # noqa: E402

if not llm.configured():
    sys.exit("Azure OpenAI not configured: fill in .env first.")

samples = json.load(open(os.path.join(os.path.dirname(__file__), "..", "samples", "messages.json"), encoding="utf-8"))
tot = ok = 0
for s in samples:
    t0 = time.time()
    out = llm.understand(s["text"], [], "LISTINGS: (none)\nOPEN REQUIREMENTS: (none)")["actions"]
    dt = time.time() - t0
    got = []
    for a in out:
        if a["intent"] == "save_listing":
            ch, low, _ = N.normalize_listing(a["listing"])
            got.append({"intent": "save_listing", **ch, "_low": low})
        elif a["intent"] == "save_requirement":
            got.append({"intent": "save_requirement", **N.normalize_requirement(a["requirement"])})
        else:
            got.append({"intent": a["intent"]})
    print(f'\n[{s["id"]}] {dt:.1f}s  expected {len(s["expect"])} action(s), got {len(got)}')
    for exp, g in zip(s["expect"], got + [{}] * len(s["expect"])):
        for k, v in exp.items():
            tot += 1
            hit = g.get(k) == v or (isinstance(v, (int, float)) and isinstance(g.get(k), (int, float)) and abs(g[k] - v) <= abs(v) * 0.01)
            ok += hit
            if not hit:
                print(f"   ✗ {k}: expected {v!r}, got {g.get(k)!r}")
    if len(got) != len(s["expect"]):
        tot += 1
        print("   ✗ wrong number of actions (split/merge error)")
print(f"\nField accuracy: {ok}/{tot} = {100 * ok / max(tot, 1):.1f}%  (target ≥ 90%)")
