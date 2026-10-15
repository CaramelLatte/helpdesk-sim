"""
Ticket lifecycle for lab-backed scenarios (a real machine instead of the scripted shell).

Same shape as the scripted flow in tickets.py: roll a persona, have the persona model
write the opening message, create the Zammad ticket, and at close read Zammad's
state and grade. The differences:

  - Creation also starts a lab session (a clone of a frozen template with the
    scenario's faults injected). The persona writes its opening message while the
    machine boots, so the wait mostly overlaps.
  - The operator works on the machine through real consoles. The console URLs carry
    a per-session token, returned only to whoever created the ticket.
  - At close, the lab measures the machine and returns an evidence report (ground
    truth, end state vs. post-injection state, every action taken, file changes,
    terminal transcript) and a deterministic outcome. The grading model gets that
    report plus the ticket conversation and timing, and is told the outcome is
    authoritative, exactly like resolution paths in the scripted flow.
"""
import re
from flask import jsonify
import db
import services.zammad as zammad
import services.persona as persona_svc
import services.llm as llm
import services.lab as lab
from config import ZAMMAD_URL, GRADING_MODEL

DEFAULT_RUBRIC = [
    {"id": "diagnostic_accuracy", "label": "Diagnostic accuracy"},
    {"id": "diagnostic_efficiency", "label": "Diagnostic efficiency"},
    {"id": "escalation_judgment", "label": "Escalation judgment"},
    {"id": "change_safety", "label": "Change safety"},
    {"id": "response_timeliness", "label": "Response timeliness"},
    {"id": "customer_service", "label": "Customer service"},
]


def create_lab_ticket(short_id, scenario):
    traits = persona_svc.roll_persona()
    try:
        sess = lab.start_session(short_id, ref="helpdesk-sim")
    except lab.LabUnavailable as e:
        return jsonify({"error": str(e)}), 503

    t = scenario.get("ticket", {})
    persona_view = {
        "title": scenario["title"],
        "initial_ticket": {"subject": t.get("subject", scenario["title"]), "body": t.get("body", "")},
        "reporter": t.get("reporter"),
        "persona_facts": t.get("persona_facts", []),
        "remote_access": sorted(scenario.get("access", {})),
    }
    system_prompt = persona_svc.build_system_prompt({"current_mood": "calm", **traits}, persona_view)
    try:
        opening_message = llm.generate_persona_message(
            system_prompt,
            [{"role": "user", "content": (
                "Write your initial help desk ticket message describing your problem. "
                "Write only the message body — no subject line, no greeting like 'Dear Support'."
            )}]
        )
        status = lab.wait_ready(sess["session"])
    except Exception as e:
        lab.destroy(sess["session"])
        msg = str(e) if isinstance(e, lab.LabUnavailable) else "Could not start the session."
        return jsonify({"error": msg}), 503

    z_ticket = zammad.create_ticket(subject=persona_view["initial_ticket"]["subject"], body=opening_message)
    ticket_id = db.create_ticket(z_ticket["id"], short_id, lab_session_id=sess["session"])
    db.create_persona(ticket_id, traits)

    return jsonify({
        "ticket_id": ticket_id,
        "zammad_ticket_id": z_ticket["id"],
        "scenario": short_id,
        "persona_traits": traits,
        "zammad_url": f"{ZAMMAD_URL}/#ticket/zoom/{z_ticket['id']}",
        "lab": {
            "session_id": sess["session"],
            "consoles": lab.console_urls(sess["session"], sess["token"], status.get("access", {})),
            "access": status.get("access", {}),
            "machines": [{"hostname": m["hostname"], "role": m["role"]} for m in status.get("machines", [])],
        },
    }), 201


def lab_commands_help(ticket):
    """What the old line-by-line shell panel shows under "Available commands"."""
    try:
        st = lab.status(ticket["lab_session_id"])
        term = st.get("access", {}).get("terminal", {})
        host, user = term.get("hostname", "the machine"), term.get("user", "helpdesk")
    except Exception:
        host, user = "the machine", "helpdesk"
    return [
        f"This is a real Linux machine ({host}). You are '{user}' and have sudo.",
        "Any command works. Each line runs on its own; the working directory is remembered.",
        "Interactive programs (top, vim, less) need the full terminal; use forms like `top -b -n1`.",
        "Everything you run is recorded and used for grading, along with the machine's end state.",
    ]


def close_lab_ticket(ticket, resolution_notes):
    z_ticket = zammad.get_ticket(ticket["zammad_ticket_id"])
    state_name = zammad.get_ticket_state_name(z_ticket["state_id"])
    group_name = zammad.get_group_name(z_ticket["group_id"])
    articles = zammad.get_ticket_articles(ticket["zammad_ticket_id"])

    # The lab's outcome table speaks in terms of what the operator did with the ticket.
    if state_name == "closed":
        action = {"type": "closed"}
    elif group_name != "Users":
        action = {"type": "escalated", "group": group_name}
    else:
        action = {"type": "none"}

    try:
        report = lab.finish(ticket["lab_session_id"], action)
    except Exception:
        try:
            report = lab.report(ticket["lab_session_id"])   # e.g. already finished by the idle reaper
        except Exception as e:
            return jsonify({"error": f"Could not collect the lab evidence: {e}"}), 503

    db.save_lab_report(ticket["id"], report["outcome"]["id"], report)
    _post_lab_note(ticket, report)
    result = _grade(ticket, report, resolution_notes, state_name, group_name, articles)
    db.update_ticket(ticket["id"], status="graded")

    outcome = report["outcome"]
    return jsonify({
        "zammad_state": state_name,
        "zammad_group": group_name,
        "resolution_path": {"id": outcome["id"], "label": outcome["label"],
                            "score_modifier": outcome["score"]},
        "grading": result,
        "lab": {
            "outcome": outcome,
            "facts": {k: report["facts"][k] for k in ("fixed", "symptoms_fixed", "collateral", "verified")},
            "checks": [{"desc": c["desc"], "kind": c["kind"], "before": c["baseline"], "after": c["final"]}
                       for c in report["checks"] if c["kind"] != "info"],
            "debrief": report.get("debrief", ""),
        },
    })


def _post_lab_note(ticket, report):
    """Attach the machine-side timeline and outcome to the Zammad ticket as an internal note."""
    text = report.get("text", "")
    start = text.find("--- ACTIONS TAKEN")
    end = text.find("--- EVIDENCE ITEMS")
    actions = text[start:end].strip() if start >= 0 else "(no actions recorded)"
    o = report["outcome"]
    body = (f"=== Lab session {report['session']} ===\n\n{actions}\n\n"
            f"Outcome: {o['label']} [{o['id']}], score modifier {o['score']}")
    try:
        zammad.add_article(ticket["zammad_ticket_id"], body, internal=True)
    except Exception as e:
        print(f"[lab] could not post evidence note: {e}")


def _grade(ticket, report, resolution_notes, zammad_state, zammad_group, articles):
    """Builds the lab grading prompt, calls the grading model, parses and saves the result."""
    from routes.tickets import _compute_timing, _format_conversation   # shared with the scripted flow

    persona = db.get_persona(ticket["id"])
    rubric = (lab.track() or {}).get("rubric") or DEFAULT_RUBRIC
    timing = _compute_timing(ticket, articles)
    literacy = persona["technical_literacy"] if persona else 5.0
    if literacy < 3:
        literacy_desc = "very low — completely non-technical, any jargon will confuse them"
    elif literacy < 6:
        literacy_desc = "low to moderate — understands plain language but not technical terms"
    elif literacy < 8:
        literacy_desc = "moderate — some technical familiarity, brief explanations of terms are fine"
    else:
        literacy_desc = "fairly high — comfortable with technical terminology"

    if zammad_state == "closed":
        zammad_context = f"Operator marked the ticket CLOSED (group: {zammad_group})."
    elif zammad_group != "Users":
        zammad_context = f"Operator reassigned the ticket to '{zammad_group}' (state: {zammad_state}): an escalation."
    else:
        zammad_context = (f"Ticket left in state '{zammad_state}', group '{zammad_group}': the operator neither "
                          "closed nor escalated it.")

    dims = "\n".join(f"{i}. {d['label']}" for i, d in enumerate(rubric, 1))
    fmt = "\n".join(f"{d['id'].upper()}: <score>/10" for d in rubric)
    modifier = report["outcome"]["score"]

    grading_prompt = f"""You are evaluating an IT help desk training exercise worked on a REAL machine.

The trainee talked to a simulated end user through a ticket and worked on a real (disposable) Linux
machine through a terminal{' and the user’s remote desktop' if 'gui' in report.get('access', {}) else ''}.
A fault was injected on purpose. The lab measured the machine before and after and recorded every
action. Treat the lab report's END STATE, FACTS and DETERMINISTIC OUTCOME as authoritative: they are
measurements, not opinions. Your job is to judge how well the work was done, not to re-decide the outcome.

{report.get('text', '')}

=== TICKET SIDE (helpdesk system) ===
ZAMMAD WORKFLOW CONTEXT: {zammad_context}
TIMING: time to first response {timing['ttfr']}; total session {timing['session_time']};
        unprompted follow-ups from the user: {persona['follow_up_count'] if persona else 'unknown'}
OPERATOR NOTES AT CLOSE: {resolution_notes or '(none provided)'}

CONVERSATION THREAD (user = simulated end user, operator = trainee):
{_format_conversation(articles)}

GRADING GUIDANCE:
- DIAGNOSTIC ACCURACY: did they find the actual root cause from the ground truth, or correctly recognise it was beyond T1?
  Use the evidence items, the action timeline and what they wrote to the user / in their notes.
- DIAGNOSTIC EFFICIENCY: compare the action timeline with the ideal resolution. On a real machine many paths are
  valid: equivalent tools (dig vs nslookup, GUI vs nmcli) are equally good. Penalise aimless or repeated commands,
  restarts before investigation, and long detours, not style.
- ESCALATION JUDGMENT: expected resolution type is in the ground truth. Correct escalation with evidence is FULL
  credit when escalation is expected. Escalating a T1-fixable problem, or "fixing" something T1 must not touch, is poor judgment.
- CHANGE SAFETY: use the guard checks, FILE CHANGES and anti-patterns. Reward minimal, targeted changes and verifying the
  fix; penalise collateral damage, unnecessary changes (e.g. resetting a working password), weakening security, editing
  production code/config, or deleting data.
- RESPONSE TIMELINESS: from the timing metrics and follow-up count.
- CUSTOMER SERVICE: the user's technical literacy was {literacy_desc}; judge language against that. Identity verification
  matters before account changes. Closing the loop: for a fix, confirm in plain language it works (ideally ask the user to
  check); for an escalation, say who has it now and what happens next, without dumping raw technical findings on the user.
- CROSS-CHECK CLAIMS: if the operator told the user (or wrote in notes) that something was checked, changed or fixed,
  confirm it against the actions and end state. Unsupported claims are a serious problem; say so specifically.
- The OVERALL score should be consistent with the outcome's score modifier ({modifier}): it should rarely exceed
  {int(modifier * 100) + 5}.
- Be specific: quote actual commands (with their timestamps) and messages. No vague praise.

Score each dimension from 0 to 10:
{dims}
Then an overall score from 0 to 100 and 3-5 sentences of specific, actionable feedback.

Format your response exactly as:
{fmt}
OVERALL: <score>/100
FEEDBACK: <your feedback>"""

    system_prompt = ("You are an experienced IT service desk lead grading a trainee. Be honest, specific and "
                     "constructive. Do not inflate scores. Machine measurements in the lab report are facts.")
    raw = llm.generate_grading(system_prompt, grading_prompt, max_tokens=4096)

    result = {"raw": raw}
    keys = {d["id"].upper(): d["id"] for d in rubric}
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        clean = re.sub(r"[*`#]", "", line).strip()   # markdown emphasis, keep underscores
        m = re.match(r"^([A-Z_]+)\s*:\s*(.*)$", clean)
        if not m:
            continue
        key, val = m.groups()
        if key in keys:
            result[keys[key]] = val.strip()
        elif key == "OVERALL":
            result["overall_score"] = val.strip()
        elif key == "FEEDBACK":
            original = line.split(":", 1)[1] if ":" in line else val
            result["feedback"] = "\n".join([original.strip()] + lines[i + 1:]).strip()
            break
    try:
        overall = float(re.match(r"[\d.]+", result.get("overall_score", "0")).group())
    except (AttributeError, ValueError):
        overall = 0.0
    result["rubric_labels"] = {d["id"]: d["label"] for d in rubric}
    result["rubric_order"] = [d["id"] for d in rubric]   # JSON objects lose key order
    db.save_grading_result(ticket["id"], overall, result, report["outcome"]["id"], GRADING_MODEL)
    return result
