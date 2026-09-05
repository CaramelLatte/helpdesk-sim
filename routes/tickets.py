from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
import db
import services.zammad as zammad
import services.persona as persona_svc
import services.scenario as scenario_svc
import services.scheduler as scheduler
import services.llm as llm

tickets_bp = Blueprint("tickets", __name__)


@tickets_bp.route("/api/tickets", methods=["POST"])
def create_ticket():
    """
    Starts a new training session.
    Body (JSON): { "scenario_id": "dns_failure" }
    Returns the local ticket ID and the Zammad ticket ID.
    """
    data = request.get_json()
    scenario_id = data.get("scenario_id")
    if not scenario_id:
        return jsonify({"error": "scenario_id is required"}), 400

    try:
        scenario = scenario_svc.load_scenario(scenario_id)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404

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

    articles = zammad.get_ticket_articles(ticket["zammad_ticket_id"])
    public = [a for a in articles if not a.get("internal", False)]

    messages = []
    for article in public:
        messages.append({
            "role": "user" if article.get("sender") == "Customer" else "operator",
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

    zammad.set_ticket_resolution(ticket["zammad_ticket_id"], state=state, group=group)

    return jsonify({
        "zammad_ticket_id": ticket["zammad_ticket_id"],
        "state": state,
        "group": group,
    })


@tickets_bp.route("/api/tickets/<int:ticket_id>/close", methods=["POST"])
def close_ticket(ticket_id):
    """
    Triggers grading for a training session the operator has finished working.
    Does NOT push any state to Zammad — the operator is expected to have already
    closed or reassigned the ticket in the Zammad UI before calling this.
    Body (JSON, optional): { "resolution_notes": "Diagnosed bad DNS, escalated to network team." }
    """
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    if ticket["status"] != "open":
        return jsonify({"error": f"Ticket is already {ticket['status']}"}), 400

    data = request.get_json() or {}
    resolution_notes = data.get("resolution_notes", "")

    # Read actual Zammad ticket state — don't push anything.
    z_ticket = zammad.get_ticket(ticket["zammad_ticket_id"])
    state_name = zammad.get_ticket_state_name(z_ticket["state_id"])
    group_name = zammad.get_group_name(z_ticket["group_id"])

    # Fetch articles for timing metrics.
    articles = zammad.get_ticket_articles(ticket["zammad_ticket_id"])

    # Determine which resolution path was taken based on shell discoveries.
    discoveries = db.get_all_discoveries(ticket_id)
    scenario = scenario_svc.load_scenario(ticket["scenario_id"])
    resolution = scenario_svc.get_resolution_path(ticket["scenario_id"], discoveries)

    # Post the full shell command log as a single internal note so the Zammad
    # ticket record has the diagnostic trail attached without cluttering the live thread.
    _post_command_log_note(ticket, db.get_command_log(ticket_id))

    # Run the grading model.
    result = _run_grading(
        ticket, scenario, discoveries, resolution, resolution_notes,
        state_name, group_name, articles
    )

    db.update_ticket(ticket_id, status="graded")

    return jsonify({
        "zammad_state": state_name,
        "zammad_group": group_name,
        "resolution_path": resolution,
        "grading": result,
    })


@tickets_bp.route("/api/tickets", methods=["GET"])
def list_tickets():
    """Lists all open training tickets."""
    tickets = db.get_open_tickets()
    return jsonify({"tickets": tickets})


@tickets_bp.route("/api/scenarios", methods=["GET"])
def list_scenarios():
    """Lists available scenario IDs."""
    return jsonify({"scenarios": scenario_svc.list_scenarios()})
    

@tickets_bp.route("/api/admin/cleanup", methods=["POST"])
def manual_cleanup():
    """
    Immediately deletes all graded/closed tickets older than CLEANUP_AFTER_HOURS
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
                 zammad_state, zammad_group, articles):
    """Builds the grading prompt, calls the grading model, and saves the result."""
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

    grading_prompt = f"""You are evaluating an IT help desk training exercise. This is a CONSTRAINED SIMULATION. Grade the operator strictly against what was possible within the defined scenario — do not apply real-world expectations that exceed its scope.

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

TIMING METRICS:
  Time to first response: {timing['ttfr']}
  Total session time: {timing['session_time']}
  Persona follow-ups sent unprompted: {persona['follow_up_count'] if persona else 'unknown'}

OPERATOR NOTES AT CLOSE: {resolution_notes or '(none provided)'}

TICKET CONVERSATION THREAD (user = simulated end user, operator = trainee):
{thread_str}

COMMANDS RUN (in order):
{commands_str}

OPTIMAL DIAGNOSTIC PATH FOR THIS SCENARIO:
{chr(10).join('  ' + c for c in optimal_path)}

GRADING GUIDANCE:
- DIAGNOSTIC_ACCURACY: score based on the system-determined resolution and discoveries above. Do not contradict the system's determination.
- DIAGNOSTIC_EFFICIENCY: compare only against AVAILABLE COMMANDS and the OPTIMAL PATH. Do not penalize for omitting commands that weren't in the scenario.
- ESCALATION_JUDGMENT: if this was a direct-fix scenario, penalize unnecessary escalation. If this was an escalation scenario, penalize attempting a fix. If the resolution modifier is 0.9+, assume judgment was correct.
- CUSTOMER_SERVICE: the simulated user's technical literacy was {literacy_desc}. Evaluate language calibration against that level specifically. Do not suggest technical phrasing (IP addresses, config file paths, error codes) as improvements if the user was non-technical — that would be the wrong direction.
- "Closing the loop" means different things by resolution type:
    * Direct fix: confirm with the user in plain, non-technical language that the issue is resolved and briefly what was done.
    * Escalation: inform the user the ticket is being passed to the appropriate team, what they can expect next, and a rough timeline. Do NOT suggest relaying raw technical findings (nameserver values, log excerpts) to the end user — those go to the receiving team, not the customer.
- If the user declined remote access or preferred to be guided through steps verbally, do not penalize reduced shell activity. Verbal guidance is a valid T1 resolution.
- Be specific in feedback. Reference actual messages or commands. Vague praise or criticism is not useful to the trainee.

Please evaluate on these dimensions and give a score from 0–10 for each:
1. Diagnostic accuracy — Did they correctly identify the root cause, or correctly recognize it exceeded T1 scope?
2. Diagnostic efficiency — Was their command sequence logical given the AVAILABLE COMMANDS?
3. Escalation judgment — Did they escalate or resolve appropriately for this scenario type?
4. Response timeliness — Based on timing metrics and persona follow-up count.
5. Customer service — Professionalism, empathy, language calibrated to this user's technical level, and appropriate loop closure for the resolution type.

Then give an overall score from 0–100 and 2–3 sentences of specific, actionable feedback.

Format your response as:
DIAGNOSTIC_ACCURACY: <score>/10
DIAGNOSTIC_EFFICIENCY: <score>/10
ESCALATION_JUDGMENT: <score>/10
RESPONSE_TIMELINESS: <score>/10
CUSTOMER_SERVICE: <score>/10
OVERALL: <score>/100
FEEDBACK: <your feedback>"""

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
            elif line.startswith("FEEDBACK:"):
                rubric["feedback"] = line.split(":", 1)[1].strip()
    except Exception:
        pass

    overall = float(rubric.get("overall_score", "0").split("/")[0]) if "overall_score" in rubric else 0.0

    db.save_grading_result(
        ticket["id"],
        overall,
        rubric,
        resolution["id"],
        __import__("config").GRADING_MODEL,
    )

    return rubric


def _format_conversation(articles):
    """
    Formats the public ticket thread into a readable transcript for the grading prompt.
    Internal notes (shell logs) are excluded — the grading model gets those separately
    under COMMANDS RUN and shouldn't conflate them with the customer conversation.
    ai_user articles are the persona; all other authors are the operator.
    """
    ai_user_id = scheduler.get_ai_user_id()
    public = [a for a in articles if not a.get("internal", False)]
    if not public:
        return "  (no conversation — operator did not communicate with the user)"

    lines = []
    for article in public:
        speaker = "user" if article.get("created_by_id") == ai_user_id else "operator"
        body = (article.get("body") or "").strip()
        lines.append(f"[{speaker}]: {body}")

    return "\n".join(lines)


def _compute_timing(ticket, articles):
    """
    Derives SLA-style timing metrics from Zammad article timestamps and local DB.
    Returns human-readable strings for the grading prompt.
    """
    ai_user_id = scheduler.get_ai_user_id()
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
        if article.get("created_by_id") != ai_user_id:
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
