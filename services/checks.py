"""
Programmatic pass/fail for a finished attempt, with no model involved.

It reuses what the grading model is already told is authoritative:
  - scripted scenarios: the resolution path chosen from shell discoveries (scenario.py)
  - lab scenarios: the lab's deterministic outcome and its before/after machine checks

Both produce the same shape, so the UI has one renderer and a new scenario of either kind
gets a check without extra authoring:

  {
    "passed": bool,                 outcome score >= PASS_SCORE
    "pass_score": 0.9,
    "outcome": {"id", "label", "score"},
    "items": [{"label", "ok", "status"}],   what the attempt did and didn't establish
    "debrief": str,                 what was actually wrong (shown after the attempt)
    "elapsed_seconds": int | None,  start of the ticket to close
  }
"""
from datetime import datetime, timezone
from config import PASS_SCORE


def _elapsed(ticket):
    try:
        start = datetime.fromisoformat(ticket["created_at"]).replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None
    end = datetime.now(timezone.utc)
    if ticket.get("closed_at"):
        end = datetime.fromisoformat(ticket["closed_at"]).replace(tzinfo=timezone.utc)
    return max(0, int((end - start).total_seconds()))


def _result(ticket, outcome, items, debrief):
    return {
        "passed": outcome["score"] >= PASS_SCORE,
        "pass_score": PASS_SCORE,
        "outcome": outcome,
        "items": items,
        "debrief": debrief,
        "elapsed_seconds": _elapsed(ticket),
    }


def _humanize(discovery_id):
    return discovery_id.replace("_", " ").capitalize()


def sim_check(ticket, scenario, resolution, discoveries):
    """Scripted shell: the path the discoveries reached, and which discoveries the
    best-scoring path needs (the ones still missing are what to look for next time)."""
    paths = scenario.get("resolution_paths", [])
    ideal = max(paths, key=lambda p: p["score_modifier"]) if paths else None
    items = []
    if ideal:
        for d in ideal.get("required_discoveries", []):
            found = d in discoveries
            items.append({"label": _humanize(d), "ok": found, "status": "found" if found else "missed"})
    outcome = {"id": resolution["id"], "label": resolution["label"],
               "score": float(resolution["score_modifier"])}
    debrief = (scenario.get("description") or "").strip()
    if ideal:
        debrief = f"{debrief}\n\nBest resolution: {ideal['label']}.".strip()
    return _result(ticket, outcome, items, debrief)


def lab_check(ticket, report):
    """Lab machine: the deterministic outcome, the headline facts and every non-info check."""
    o = report["outcome"]
    f = report.get("facts", {})
    items = [
        {"label": "Problem fixed on the machine", "ok": bool(f.get("fixed")),
         "status": "yes" if f.get("fixed") else "no"},
        {"label": "Fix verified afterwards", "ok": bool(f.get("verified")),
         "status": "yes" if f.get("verified") else "no"},
        {"label": "Nothing else broken", "ok": not f.get("collateral"),
         "status": "broken" if f.get("collateral") else "OK"},
    ]
    for c in report.get("checks", []):
        if c.get("kind") == "info":
            continue
        before, after = c.get("baseline"), c.get("final")
        if c["kind"] == "guard" and before and not after:
            status = "broken by you"
        elif c["kind"] != "guard" and not before and after:
            status = "fixed"
        else:
            status = "OK" if after else "still failing"
        items.append({"label": c["desc"], "ok": bool(after), "status": status})
    outcome = {"id": o["id"], "label": o["label"], "score": float(o["score"])}
    return _result(ticket, outcome, items, report.get("debrief", ""))
