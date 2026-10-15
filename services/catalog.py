"""
One place that decides, for any scenario id, whether it runs on a real lab machine
or on the scripted shell, and that builds the persona's view of the scenario.

  - Lab scenarios come from the lab's catalog (ids like "dns-override").
  - Scripted scenarios are the YAML files in scenarios/ (ids like "dns_failure").
  - A lab scenario can declare `legacy_id`: with LAB_REPLACES_SIM on, it replaces that
    scripted scenario, and old ids still work (they map to the lab version).
  - LAB_PUBLIC controls whether lab scenarios are offered to public visitors. Requests
    made directly to gunicorn inside the container can always use them, for testing.
"""
import services.lab as lab
import services.scenario as sim
from config import LAB_REPLACES_SIM, LAB_PUBLIC


def lab_allowed(request):
    """Public traffic arrives through the container's nginx, which adds X-Helpdesk-Edge.
    Direct requests to gunicorn (127.0.0.1:5002, inside the container) are local testing."""
    if not lab.enabled():
        return False
    if LAB_PUBLIC:
        return True
    return not request.headers.get("X-Helpdesk-Edge") and request.remote_addr in ("127.0.0.1", "::1")


def _labs(include_lab):
    return lab.scenarios() if include_lab and lab.enabled() else {}


def scenario_ids(include_lab):
    """Ids for the scenario picker, lab scenarios first."""
    labs = _labs(include_lab)
    replaced = {s.get("legacy_id") for s in labs.values()} if LAB_REPLACES_SIM else set()
    return list(labs) + [i for i in sorted(sim.list_scenarios()) if i not in replaced]


def entries(include_lab):
    """Rich entries for UIs that can show more than an id."""
    labs = _labs(include_lab)
    out = []
    for sid, s in labs.items():
        out.append({"id": sid, "title": s["title"], "description": s.get("summary", ""),
                    "difficulty": s.get("difficulty"), "tier": s.get("tier"), "kind": "lab",
                    "consoles": s.get("consoles", []), "tags": s.get("tags", [])})
    for sid in scenario_ids(include_lab):
        if sid in labs:
            continue
        try:
            y = sim.load_scenario(sid)
        except Exception:
            continue
        out.append({"id": sid, "title": y.get("title", sid), "description": y.get("description", "").strip(),
                    "difficulty": y.get("difficulty"), "tier": 1, "kind": "sim",
                    "consoles": ["shell"], "system": y.get("system_template")})
    return out


def resolve(scenario_id, include_lab):
    """Returns (kind, short_id, scenario) where kind is 'lab' or 'sim'."""
    labs = _labs(include_lab)
    if scenario_id in labs:
        return "lab", scenario_id, labs[scenario_id]
    if LAB_REPLACES_SIM:
        for sid, s in labs.items():
            if s.get("legacy_id") == scenario_id:
                return "lab", sid, s
    return "sim", scenario_id, sim.load_scenario(scenario_id)


def lab_scenario(short_id):
    s = lab.scenarios().get(short_id)
    if not s:
        raise FileNotFoundError(f"No lab scenario {short_id}")
    return s


def persona_scenario(ticket):
    """The scenario as the persona prompt expects it. For lab tickets this includes what
    the user would notice on the real machine right now (from the lab's live checks)."""
    if not ticket.get("lab_session_id"):
        return sim.load_scenario(ticket["scenario_id"])
    s = lab_scenario(ticket["scenario_id"])
    observations = None
    try:
        observations = lab.status(ticket["lab_session_id"]).get("user_view")
    except Exception:
        pass
    t = s.get("ticket", {})
    return {
        "title": s["title"],
        "initial_ticket": {"subject": t.get("subject", s["title"]), "body": t.get("body", "")},
        "reporter": t.get("reporter"),
        "persona_facts": t.get("persona_facts", []),
        "observations": observations,
        "remote_access": sorted(s.get("access", {})),
    }
