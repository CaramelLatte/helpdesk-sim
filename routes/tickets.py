from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
import db
import services.zammad as zammad
import services.persona as persona_svc
import services.scenario as scenario_svc
import services.scheduler as scheduler
import services.llm as llm
import services.lab as lab
import services.catalog as catalog
import services.checks as checks
import routes.lab_tickets as lab_tickets

tickets_bp = Blueprint("tickets", __name__)


@tickets_bp.route("/api/tickets", methods=["POST"])
def create_ticket():
    """
    Starts a new training session.
    Body (JSON): { "scenario_id": "dns_failure", "mode": "full" }
      mode "full" (default): persona + Zammad ticket + grading at close.
      mode "practice": no persona, no Zammad ticket, no model calls. Returns a written brief;
        close returns the programmatic check, and grading is optional (POST .../grade).
    Returns the local ticket ID (and the Zammad ticket ID in full mode).
    """
    data = request.get_json() or {}
    scenario_id = data.get("scenario_id")
    if not scenario_id:
        return jsonify({"error": "scenario_id is required"}), 400
    mode = data.get("mode", "full")
    if mode not in ("full", "practice"):
        return jsonify({"error": "mode must be 'full' or 'practice'"}), 400

    try:
        kind, short_id, scenario = catalog.resolve(scenario_id, catalog.lab_allowed(request))
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    if kind == "lab":
        return lab_tickets.create_lab_ticket(short_id, scenario, practice=(mode == "practice"))

    if mode == "practice":
        ticket_id = db.create_ticket(0, scenario_id, mode="practice")   # 0 = no Zammad ticket
        return jsonify({
            "ticket_id": ticket_id,
            "mode": "practice",
            "scenario": scenario_id,
            "brief": catalog.briefing("sim", scenario),
        }), 201

    # Roll a fresh persona and build the opening message.
    traits = persona_svc.roll_persona()
    system_prompt = persona_svc.build_system_prompt({"current_mood": "calm", **traits}, scenario)

    initial_ticket = scenario["initial_ticket"]

    # Ask the LLM to generate the opening ticket body in the persona's voice.
    # We prime it with the scenario description as the "situation" and ask for the
    # opening message — this way even the initial ticket reflects the personality traits.
    opening_message = llm.generate_persona_message(
        system_prompt,
        [{"role": "user", "content": (
            "Write your initial help desk ticket message describing your problem. "
            "Write only the message body — no subject line, no greeting like 'Dear Support'."
        )}]
    )

    # Create the ticket in Zammad.
    z_ticket = zammad.create_ticket(
        subject=initial_ticket["subject"],
        body=opening_message,
    )
    z_id = z_ticket["id"]

    # Persist everything locally.
    ticket_id = db.create_ticket(z_id, scenario_id)
    db.create_persona(ticket_id, traits)

    return jsonify({
        "ticket_id": ticket_id,
        "zammad_ticket_id": z_id,
        "scenario": scenario_id,
        "persona_traits": traits,
        "zammad_url": f"{__import__('config').ZAMMAD_URL}/#ticket/zoom/{z_id}",
        "mode": "full",
    }), 201


@tickets_bp.route("/api/tickets/<int:ticket_id>", methods=["GET"])
def get_ticket(ticket_id):
    """Returns the current state of a training ticket."""
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404

    persona = db.get_persona(ticket_id)
    discoveries = list(db.get_all_discoveries(ticket_id))

    return jsonify({
        "ticket": ticket,
        "persona": persona,
        "discoveries": discoveries,
    })


@tickets_bp.route("/api/tickets/<int:ticket_id>/thread", methods=["GET"])
def get_thread(ticket_id):
    """
    Returns the public conversation thread in a format ready for UI display.
    Internal notes (shell logs) are excluded.
    Each message has: role ('user'|'operator'), body, and timestamp.
    Poll this endpoint every few seconds while a ticket is open to show live replies.
    """
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    if ticket["mode"] == "practice":
        return jsonify({"error": "Practice tickets have no conversation"}), 409

    articles = zammad.get_ticket_articles(ticket["zammad_ticket_id"])
    public = [a for a in articles if not a.get("internal", False)]

    messages = []
    for article in public:
        messages.append({
            "role": "user" if zammad.is_persona_article(article) else "operator",
            "body": (article.get("body") or "").strip(),
            "timestamp": article.get("created_at"),
            "article_id": article.get("id"),
        })

    return jsonify({
        "ticket_id": ticket_id,
        "status": ticket["status"],
        "messages": messages,
    })


@tickets_bp.route("/api/tickets/<int:ticket_id>/reply", methods=["POST"])
def post_reply(ticket_id):
    """
    Posts an operator message to the ticket thread.
    Body (JSON): { "message": "Have you tried restarting the network adapter?" }
    The persona scheduler will detect this and generate a reply automatically.
    """
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    if ticket["status"] != "open":
        return jsonify({"error": "Ticket is not open"}), 400
    if ticket["mode"] == "practice":
        return jsonify({"error": "Practice tickets have no conversation"}), 409

    data = request.get_json()
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    article = zammad.add_article_as_operator(ticket["zammad_ticket_id"], message)

    return jsonify({
        "article_id": article.get("id"),
        "body": message,
        "timestamp": article.get("created_at"),
    }), 201


@tickets_bp.route("/api/tickets/<int:ticket_id>/state", methods=["PATCH"])
def set_state(ticket_id):
    """
    Sets the ticket's resolution state in Zammad without triggering grading.
    Call this before /close to record whether the operator resolved or escalated.

    Body (JSON) — one or both fields:
      { "state": "closed" }                       direct fix — resolved by T1
      { "group": "Network Team" }                 escalation — reassign to another group
      { "state": "open", "group": "Dev Team" }    escalation with explicit state
    """
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    if ticket["status"] != "open":
        return jsonify({"error": "Ticket is not open"}), 400

    data = request.get_json() or {}
    state = data.get("state")
    group = data.get("group")

    if not state and not group:
        return jsonify({"error": "Provide at least one of: state, group"}), 400

    if ticket["mode"] == "practice":
        # No Zammad ticket: keep the choice on the local row, with Zammad's defaults
        # (new tickets are state open, group Users) for whatever isn't given.
        db.update_ticket(ticket_id, resolution_state=state or "open", resolution_group=group or "Users")
    else:
        zammad.set_ticket_resolution(ticket["zammad_ticket_id"], state=state, group=group)

    return jsonify({
        "zammad_ticket_id": ticket["zammad_ticket_id"],
        "state": state,
        "group": group,
    })


@tickets_bp.route("/api/tickets/<int:ticket_id>/close", methods=["POST"])
def close_ticket(ticket_id):
    """
    Ends a training session the operator has finished working.
    Does NOT push any state to Zammad — the operator is expected to have already
    closed or reassigned the ticket (Zammad UI or PATCH .../state) before calling this.
    Body (JSON, optional): { "resolution_notes": "Diagnosed bad DNS, escalated to network team." }

    Full mode: returns the programmatic check and the grading model's review.
    Practice mode: returns only the check; POST .../grade asks for the review afterwards.
    """
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    if ticket["status"] != "open":
        return jsonify({"error": f"Ticket is already {ticket['status']}"}), 400

    data = request.get_json() or {}
    # The web UI sends "notes"; the CLI client sends "resolution_notes".
    resolution_notes = data.get("resolution_notes") or data.get("notes") or ""

    practice = ticket["mode"] == "practice"
    if practice:
        db.update_ticket(ticket_id, resolution_notes=resolution_notes)
    state_name, group_name = _resolution_choice(ticket)

    if ticket.get("lab_session_id"):
        return lab_tickets.close_lab_ticket(ticket, resolution_notes, state_name, group_name)

    scenario, discoveries, resolution = _sim_resolution(ticket, state_name, group_name)

    if practice:
        db.update_ticket(ticket_id, status="closed", closed_at=lab_tickets._sqlite_now())
        return jsonify(_sim_result(db.get_ticket(ticket_id), scenario, discoveries, resolution,
                                   state_name, group_name, None))

    # Fetch articles for timing metrics.
    articles = zammad.get_ticket_articles(ticket["zammad_ticket_id"])

    # Post the full shell command log as a single internal note so the Zammad
    # ticket record has the diagnostic trail attached without cluttering the live thread.
    _post_command_log_note(ticket, db.get_command_log(ticket_id))

    # Run the grading model.
    result = _run_grading(
        ticket, scenario, discoveries, resolution, resolution_notes,
        state_name, group_name, articles
    )

    db.update_ticket(ticket_id, status="graded", closed_at=lab_tickets._sqlite_now())
    return jsonify(_sim_result(db.get_ticket(ticket_id), scenario, discoveries, resolution,
                               state_name, group_name, result))


@tickets_bp.route("/api/tickets/<int:ticket_id>/grade", methods=["POST"])
def grade_practice(ticket_id):
    """
    Practice mode only: send a closed attempt (commands or lab report, resolution choice,
    notes) to the grading model for a review. Conversation dimensions are left out.
    Returns the same shape as /close in full mode.
    """
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    if ticket["mode"] != "practice":
        return jsonify({"error": "Full-mode tickets are graded at close"}), 400
    if ticket["status"] != "closed":
        msg = "Close the ticket first" if ticket["status"] == "open" else "This attempt is already graded"
        return jsonify({"error": msg}), 400

    if ticket.get("lab_session_id"):
        return lab_tickets.grade_practice(ticket)

    state_name, group_name = _resolution_choice(ticket)
    scenario, discoveries, resolution = _sim_resolution(ticket, state_name, group_name)
    result = _run_grading(ticket, scenario, discoveries, resolution, ticket.get("resolution_notes") or "",
                          state_name, group_name, [], practice=True)
    db.update_ticket(ticket_id, status="graded")
    return jsonify(_sim_result(ticket, scenario, discoveries, resolution, state_name, group_name, result))


def _resolution_choice(ticket):
    """(state, group) the operator chose: read from Zammad for full tickets, from the local
    row for practice tickets (set by PATCH .../state)."""
    if ticket["mode"] == "practice":
        return ticket.get("resolution_state") or "open", ticket.get("resolution_group") or "Users"
    z_ticket = zammad.get_ticket(ticket["zammad_ticket_id"])
    return (zammad.get_ticket_state_name(z_ticket["state_id"]),
            zammad.get_group_name(z_ticket["group_id"]))


def _sim_resolution(ticket, state_name, group_name):
    """Which resolution path the shell discoveries reached (deterministic, so it can be
    recomputed when a practice attempt is graded later)."""
    discoveries = db.get_all_discoveries(ticket["id"])
    scenario = scenario_svc.load_scenario(ticket["scenario_id"])
    escalated = group_name != "Users" or state_name != "closed"
    resolution = scenario_svc.get_resolution_path(ticket["scenario_id"], discoveries, escalated=escalated)
    return scenario, discoveries, resolution


def _sim_result(ticket, scenario, discoveries, resolution, state_name, group_name, grading):
    """Response for close and grade. `grading` is None for a practice close."""
    return {
        "mode": ticket["mode"],
        "zammad_state": state_name,
        "zammad_group": group_name,
        "resolution_path": resolution,
        "check": checks.sim_check(ticket, scenario, resolution, discoveries),
        "grading": grading,
    }


@tickets_bp.route("/api/tickets", methods=["GET"])
def list_tickets():
    """Lists all open training tickets."""
    tickets = db.get_open_tickets()
    return jsonify({"tickets": tickets})


@tickets_bp.route("/api/scenarios", methods=["GET"])
def list_scenarios():
    """Lists available scenario IDs (lab scenarios first when the lab is offered)."""
    return jsonify({"scenarios": catalog.scenario_ids(catalog.lab_allowed(request))})


@tickets_bp.route("/api/catalog", methods=["GET"])
def scenario_catalog():
    """Scenario list with titles, difficulty and which consoles each one offers."""
    return jsonify({"scenarios": catalog.entries(catalog.lab_allowed(request))})
    

@tickets_bp.route("/api/admin/cleanup", methods=["POST"])
def manual_cleanup():
    """
    Immediately deletes all tickets (any status) older than CLEANUP_AFTER_HOURS
    from both Zammad and the local database. Useful before a demo.
    """
    from flask import current_app
    scheduler.run_cleanup_now(current_app._get_current_object())
    return jsonify({"status": "cleanup complete"})


def _post_command_log_note(ticket, command_log):
    """Attaches the full shell session as a single internal note when the ticket closes."""
    if not command_log:
        return
    lines = ["=== Diagnostic Shell Log ===\n"]
    for entry in command_log:
        lines.append(f"$ {entry['command']}")
        lines.append(entry["output"])
        if entry["revealed"]:
            lines.append(f"  [revealed: {', '.join(entry['revealed'])}]")
        lines.append("")
    zammad.add_article(ticket["zammad_ticket_id"], "\n".join(lines), internal=True)


def _run_grading(ticket, scenario, discoveries, resolution, resolution_notes,
                 zammad_state, zammad_group, articles, practice=False):
    """Builds the grading prompt, calls the grading model, and saves the result.
    practice=True: there was no conversation, so only the technical dimensions are scored."""
    command_log = db.get_command_log(ticket["id"])
    persona = db.get_persona(ticket["id"])
    optimal_path = scenario.get("optimal_path", [])

    commands_run = [f"  {e['command']}" for e in command_log]
    commands_str = "\n".join(commands_run) if commands_run else "  (no diagnostic commands run)"

    timing = _compute_timing(ticket, articles)

    # Classify resolution type for close-the-loop guidance.
    # Zammad state is workflow context only — the resolution path is the authoritative determination.
    if zammad_state == "closed":
        zammad_context = f"Operator marked the ticket CLOSED in Zammad (group: {zammad_group})."
    elif zammad_group != "Users":
        zammad_context = f"Operator reassigned the ticket to group '{zammad_group}' (state: {zammad_state}). This indicates an escalation."
    else:
        zammad_context = (
            f"Ticket state in Zammad: {zammad_state}, group: {zammad_group}. "
            "This may mean the operator forgot to update the Zammad state, or used a UI path "
            "that didn't update it. Do not use this to contradict the system-determined resolution below."
        )

    # Translate persona technical literacy to a grader-readable description.
    literacy = persona["technical_literacy"] if persona else 5.0
    if literacy < 3:
        literacy_desc = "very low — completely non-technical, any jargon will confuse them"
    elif literacy < 6:
        literacy_desc = "low to moderate — understands plain language but not technical terms"
    elif literacy < 8:
        literacy_desc = "moderate — some technical familiarity, brief explanations of terms are fine"
    else:
        literacy_desc = "fairly high — comfortable with technical terminology"

    thread_str = _format_conversation(articles)

    # The parts that depend on there having been a conversation with the simulated user.
    if practice:
        mode_note = ("PRACTICE MODE: the trainee worked this scenario from a written brief, with no simulated end user "
                     "and no ticket conversation, to practise the technical work. Do not score or comment on customer "
                     "service, communication or timeliness. Steps that need the user (verifying their identity, asking them to "
                     "test, telling them it's fixed) were impossible: do not lower any score for their absence. You may "
                     "mention them in one short sentence as a reminder for the full simulation.\n\n")
        ticket_side = f"""OPERATOR NOTES AT CLOSE: {resolution_notes or '(none provided)'}"""
        cs_guidance = ""
        notes_check = """- CROSS-CHECK THE NOTES: resolution notes are the ticket's permanent record and must be true. A claim that something was checked, changed, fixed or verified must appear in COMMANDS RUN. If any claim is unsupported, quote it in FEEDBACK; invented technical work caps DIAGNOSTIC_ACCURACY at 3. Lower OVERALL to match."""
        dims = """1. Diagnostic accuracy — Did they correctly identify the root cause, or correctly recognize it exceeded T1 scope?
2. Diagnostic efficiency — Was their command sequence logical given the AVAILABLE COMMANDS?
3. Escalation judgment — Did they escalate or resolve appropriately for this scenario type?"""
        fmt = """DIAGNOSTIC_ACCURACY: <score>/10
DIAGNOSTIC_EFFICIENCY: <score>/10
ESCALATION_JUDGMENT: <score>/10
OVERALL: <score>/100
NOTES_INVENTED_TECHNICAL_WORK: <yes|no>
FEEDBACK: <your feedback>"""
    else:
        mode_note = ""
        ticket_side = f"""TIMING METRICS:
  Time to first response: {timing['ttfr']}
  Total session time: {timing['session_time']}
  Persona follow-ups sent unprompted: {persona['follow_up_count'] if persona else 'unknown'}

OPERATOR NOTES AT CLOSE: {resolution_notes or '(none provided)'}

TICKET CONVERSATION THREAD (user = simulated end user, operator = trainee):
{thread_str}"""
        cs_guidance = f"""- CUSTOMER_SERVICE: the simulated user's technical literacy was {literacy_desc}. Evaluate language calibration against that level specifically. Do not suggest technical phrasing (IP addresses, config file paths, error codes) as improvements if the user was non-technical — that would be the wrong direction.
- "Closing the loop" means different things by resolution type:
    * Direct fix: confirm with the user in plain, non-technical language that the issue is resolved and briefly what was done.
    * Escalation: inform the user the ticket is being passed to the appropriate team, what they can expect next, and a rough timeline. Do NOT suggest relaying raw technical findings (nameserver values, log excerpts) to the end user — those go to the receiving team, not the customer.
- If the user declined remote access or preferred to be guided through steps verbally, do not penalize reduced shell activity. Verbal guidance is a valid T1 resolution.
"""
        notes_check = """- CROSS-CHECK THE NOTES: resolution notes are the ticket's permanent record and must be true. Check every claim in OPERATOR NOTES AT CLOSE against the thread and COMMANDS RUN:
    * A claim about what the user said, reported, confirmed or agreed to must appear in a user message in the thread. Permission to make a change is NOT confirmation that the fix worked.
    * A claim that something was checked, changed, fixed or verified must appear in COMMANDS RUN (or, for verbal guidance, in the thread).
    * If any claim is unsupported, quote it in FEEDBACK. An invented user confirmation caps CUSTOMER_SERVICE at 4; invented technical work caps DIAGNOSTIC_ACCURACY at 3. Lower OVERALL to match.
- A direct fix the user was never told about is an unclosed loop: CUSTOMER_SERVICE at most 7."""
        dims = """1. Diagnostic accuracy — Did they correctly identify the root cause, or correctly recognize it exceeded T1 scope?
2. Diagnostic efficiency — Was their command sequence logical given the AVAILABLE COMMANDS?
3. Escalation judgment — Did they escalate or resolve appropriately for this scenario type?
4. Response timeliness — Based on timing metrics and persona follow-up count.
5. Customer service — Professionalism, empathy, language calibrated to this user's technical level, and appropriate loop closure for the resolution type."""
        fmt = """DIAGNOSTIC_ACCURACY: <score>/10
DIAGNOSTIC_EFFICIENCY: <score>/10
ESCALATION_JUDGMENT: <score>/10
RESPONSE_TIMELINESS: <score>/10
CUSTOMER_SERVICE: <score>/10
OVERALL: <score>/100
NOTES_INVENTED_USER_CONFIRMATION: <yes|no>
NOTES_INVENTED_TECHNICAL_WORK: <yes|no>
FEEDBACK: <your feedback>"""

    grading_prompt = f"""{mode_note}You are evaluating an IT help desk training exercise. This is a CONSTRAINED SIMULATION. Grade the operator strictly against what was possible within the defined scenario — do not apply real-world expectations that exceed its scope.

SIMULATION CONSTRAINTS:
- The shell environment is deterministic and pre-scripted. Only the commands in AVAILABLE COMMANDS exist. Anything else returns "command not found."
- The operator cannot free-form edit files. System state can only change through explicit fix-action commands that appear in AVAILABLE COMMANDS (e.g. resetting a lockout, clearing a print queue, remapping a drive). If no fix-action command exists for the root cause, the valid T1 options were: (a) walk the user through making the change themselves, or (b) escalate. Do not penalize the operator for not performing write operations that had no corresponding command.
- The operator had no access to live infrastructure, real servers, or external tools beyond the defined command set.
- This is a T1 exercise. The operator is not expected to have network admin access, server credentials, or write access to production systems.

SCENARIO: {scenario['title']}
ISSUE REPORTED: {scenario.get('initial_ticket', {}).get('subject', 'Unknown')}
SCENARIO DESCRIPTION: {scenario.get('description', '(none)')}

AVAILABLE COMMANDS IN THIS SCENARIO:
{chr(10).join('  - ' + c for c in scenario.get('available_commands', []))}

--- SYSTEM-DETERMINED RESOLUTION (authoritative) ---
The system has evaluated the operator's shell evidence and determined the resolution.
Score DIAGNOSTIC_ACCURACY and ESCALATION_JUDGMENT against this determination, not against Zammad state.

  Resolution: {resolution['label']}
  Score modifier: {resolution['score_modifier']} (1.0 = full credit, 0.2 = minimal credit)
  Discoveries made: {', '.join(discoveries) if discoveries else '(none)'}

ZAMMAD WORKFLOW CONTEXT (supplementary only):
  {zammad_context}

---

{ticket_side}

COMMANDS RUN (in order):
{commands_str}

OPTIMAL DIAGNOSTIC PATH FOR THIS SCENARIO:
{chr(10).join('  ' + c for c in optimal_path)}

GRADING GUIDANCE:
- DIAGNOSTIC_ACCURACY: score based on the system-determined resolution and discoveries above. Do not contradict the system's determination.
- DIAGNOSTIC_EFFICIENCY: compare only against AVAILABLE COMMANDS and the OPTIMAL PATH. Do not penalize for omitting commands that weren't in the scenario.
- ESCALATION_JUDGMENT: if this was a direct-fix scenario, penalize unnecessary escalation. If this was an escalation scenario, penalize attempting a fix. If the resolution modifier is 0.9+, assume judgment was correct.
{cs_guidance}{notes_check}
- Be specific in feedback. Reference actual messages or commands. Vague praise or criticism is not useful to the trainee.

Please evaluate on these dimensions and give a score from 0–10 for each:
{dims}

Then give an overall score from 0–100 and 2–3 sentences of specific, actionable feedback.

Format your response as:
{fmt}"""

    system_prompt = ("You are an experienced IT help desk trainer. "
                     "Provide honest, specific, constructive evaluations. "
                     "Do not inflate scores. The system has already determined the resolution type "
                     "from evidence — your job is to evaluate execution quality, not re-litigate the outcome.")

    raw = llm.generate_grading(system_prompt, grading_prompt)

    rubric = {"raw": raw}
    try:
        for line in raw.splitlines():
            if line.startswith("DIAGNOSTIC_ACCURACY:"):
                rubric["diagnostic_accuracy"] = line.split(":")[1].strip()
            elif line.startswith("DIAGNOSTIC_EFFICIENCY:"):
                rubric["diagnostic_efficiency"] = line.split(":")[1].strip()
            elif line.startswith("ESCALATION_JUDGMENT:"):
                rubric["escalation_judgment"] = line.split(":")[1].strip()
            elif line.startswith("RESPONSE_TIMELINESS:"):
                rubric["response_timeliness"] = line.split(":")[1].strip()
            elif line.startswith("CUSTOMER_SERVICE:"):
                rubric["customer_service"] = line.split(":")[1].strip()
            elif line.startswith("OVERALL:"):
                rubric["overall_score"] = line.split(":")[1].strip()
            elif line.startswith("NOTES_INVENTED_USER_CONFIRMATION:"):
                rubric["notes_invented_user_confirmation"] = line.split(":")[1].strip().lower().startswith("y")
            elif line.startswith("NOTES_INVENTED_TECHNICAL_WORK:"):
                rubric["notes_invented_technical_work"] = line.split(":")[1].strip().lower().startswith("y")
            elif line.startswith("FEEDBACK:"):
                rubric["feedback"] = line.split(":", 1)[1].strip()
    except Exception:
        pass

    _enforce_caps(rubric, resolution, zammad_state, zammad_group, articles, command_log, practice)

    overall = float(rubric.get("overall_score", "0").split("/")[0]) if "overall_score" in rubric else 0.0

    db.save_grading_result(
        ticket["id"],
        overall,
        rubric,
        resolution["id"],
        __import__("config").GRADING_MODEL,
    )

    return rubric



def _parse_ts(ts):
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    # SQLite timestamps have no timezone — treat them as UTC to match Zammad's aware datetimes.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _score(value):
    try:
        return int(float(str(value).split("/")[0]))
    except (TypeError, ValueError):
        return None


def _cap(rubric, key, limit, reason, notes):
    current = _score(rubric.get(key))
    if current is None or current <= limit:
        return 0
    rubric[key] = f"{limit}/10"
    notes.append(f"{key.replace('_', ' ').capitalize()} capped at {limit}/10: {reason}.")
    return current - limit


def _enforce_caps(rubric, resolution, zammad_state, zammad_group, articles, command_log, practice=False):
    """
    The model reliably notices these problems but doesn't hold to numeric caps, so the
    caps are applied here. Each rubric point removed costs 2 overall points (five items
    make up 100). Adjustments are listed in rubric["adjustments"] and in the feedback.
    """
    notes = []
    removed = 0
    if rubric.get("notes_invented_user_confirmation"):
        removed += _cap(rubric, "customer_service", 4,
                        "the notes claim the user confirmed something the thread doesn't show", notes)
    if rubric.get("notes_invented_technical_work"):
        removed += _cap(rubric, "diagnostic_accuracy", 3,
                        "the notes claim checks or fixes that the command log doesn't show", notes)

    # Direct fix the user was never told about: no operator message after the last command
    # that revealed something (the fix or its verification). Later idle commands don't count.
    # Not in practice mode: there was no user to tell.
    direct_fix = (not practice and zammad_state == "closed" and zammad_group == "Users"
                  and resolution.get("score_modifier", 0) >= 0.9)
    revealing = [e for e in command_log if e.get("revealed")]
    if direct_fix and revealing:
        last_cmd = _parse_ts(revealing[-1].get("executed_at"))
        told = any(
            not zammad.is_persona_article(a) and not a.get("internal", False)
            and (_parse_ts(a.get("created_at")) or last_cmd) > last_cmd
            for a in articles
        )
        if not told:
            removed += _cap(rubric, "customer_service", 7,
                            "the user was never told the issue was fixed", notes)

    if not notes:
        return
    overall = _score(rubric.get("overall_score"))
    if overall is not None:
        rubric["overall_score"] = f"{max(0, overall - 2 * removed)}/100"
    rubric["adjustments"] = notes
    rubric["feedback"] = (rubric.get("feedback", "") + " " + " ".join(notes)).strip()

def _format_conversation(articles):
    """
    Formats the public ticket thread into a readable transcript for the grading prompt.
    Internal notes (shell logs) are excluded — the grading model gets those separately
    under COMMANDS RUN and shouldn't conflate them with the customer conversation.
    Customer articles are the persona; Agent articles are the operator.
    """
    public = [a for a in articles if not a.get("internal", False)]
    if not public:
        return "  (no conversation — operator did not communicate with the user)"

    lines = []
    for article in public:
        speaker = "user" if zammad.is_persona_article(article) else "operator"
        body = (article.get("body") or "").strip()
        lines.append(f"[{speaker}]: {body}")

    return "\n".join(lines)


def _compute_timing(ticket, articles):
    """
    Derives SLA-style timing metrics from Zammad article timestamps and local DB.
    Returns human-readable strings for the grading prompt.
    """
    public = [a for a in articles if not a.get("internal", False)]

    def parse_ts(ts):
        if not ts:
            return None
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        # SQLite timestamps have no timezone — treat them as UTC to match Zammad's aware datetimes.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    ticket_created = parse_ts(ticket.get("created_at"))

    # Time to first operator response.
    ttfr_str = "no operator response recorded"
    for article in public:
        if not zammad.is_persona_article(article):
            first_op_time = parse_ts(article.get("created_at"))
            if first_op_time and ticket_created:
                delta = (first_op_time - ticket_created).total_seconds()
                ttfr_str = _fmt_duration(delta)
            break

    # Total session time: first article to last article.
    session_str = "unknown"
    if len(public) >= 2:
        first = parse_ts(public[0].get("created_at"))
        last = parse_ts(public[-1].get("created_at"))
        if first and last:
            session_str = _fmt_duration((last - first).total_seconds())

    return {"ttfr": ttfr_str, "session_time": session_str}


def _fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
