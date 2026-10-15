"""
Background job that drives proactive persona behavior.

Every SCHEDULER_INTERVAL_SECONDS, this job:
  1. Checks each open ticket for new operator comments it hasn't responded to yet.
     If found, generates a persona reply and posts it to Zammad.
  2. Checks if the persona should proactively follow up because the ticket has gone quiet.
     Follow-up interval is derived from the persona's patience trait.

This runs inside the Flask process using APScheduler's BackgroundScheduler,
so there's no separate process to manage — just start the Flask app and it runs.
"""
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
import db
import services.zammad as zammad
import services.persona as persona_svc
import services.catalog as catalog
import services.llm as llm
import services.lab as lab
from config import SCHEDULER_INTERVAL_SECONDS, CLEANUP_AFTER_HOURS

# Populated on startup with the Zammad user ID of our api account,
# so we can tell apart our own articles from the operator's articles.
_ai_user_id = None


def set_ai_user_id(user_id):
    global _ai_user_id
    _ai_user_id = user_id


def get_ai_user_id():
    return _ai_user_id


def start(app):
    """Call once at Flask startup to start the background scheduler."""
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=lambda: _check_tickets(app),
        trigger="interval",
        seconds=SCHEDULER_INTERVAL_SECONDS,
        id="persona_poll",
        replace_existing=True,
    )
    if CLEANUP_AFTER_HOURS > 0:
        scheduler.add_job(
            func=lambda: _cleanup_old_tickets(app),
            trigger="interval",
            hours=6,
            id="ticket_cleanup",
            replace_existing=True,
        )
    scheduler.start()
    return scheduler


def _check_tickets(app):
    """Runs inside the scheduler's background thread."""
    with app.app_context():
        for ticket in db.get_open_tickets():
            try:
                _process_ticket(ticket)
            except Exception as e:
                # Don't let one bad ticket crash the loop for all others.
                print(f"[scheduler] Error processing ticket {ticket['id']}: {e}")


def _process_ticket(ticket):
    ticket_id = ticket["id"]
    persona = db.get_persona(ticket_id)
    if not persona:
        return

    articles = zammad.get_ticket_articles(ticket["zammad_ticket_id"])
    # Filter to public (non-internal) articles only — the shell command logs are internal.
    public_articles = [a for a in articles if not a.get("internal", False)]

    if not public_articles:
        return

    last_article = public_articles[-1]
    last_author_id = last_article.get("created_by_id")
    last_article_id = last_article["id"]

    operator_replied = (
        last_author_id != _ai_user_id
        and last_article_id != ticket.get("last_operator_article_id")
    )

    if operator_replied:
        # The operator posted something new. Generate a persona reply.
        _reply_to_operator(ticket, persona, public_articles)
        db.update_ticket(ticket_id, last_operator_article_id=last_article_id)
    else:
        # Check if the persona should follow up because the ticket has gone quiet.
        _maybe_follow_up(ticket, persona, public_articles)


def _reply_to_operator(ticket, persona, public_articles):
    """Generates and posts a persona response to a new operator message."""
    ticket_id = ticket["id"]
    scenario = catalog.persona_scenario(ticket)
    system_prompt = persona_svc.build_system_prompt(persona, scenario)

    history = _build_history(public_articles)
    reply = llm.generate_persona_message(system_prompt, history)

    zammad.add_article(ticket["zammad_ticket_id"], reply, internal=False)
    db.update_ticket(ticket_id, last_persona_reply_at=_now())


def _maybe_follow_up(ticket, persona, public_articles):
    """Sends a proactive follow-up if the persona's patience interval has elapsed."""
    ticket_id = ticket["id"]
    last_reply = ticket.get("last_persona_reply_at")
    if not last_reply:
        return

    elapsed_minutes = (datetime.now(timezone.utc) - _parse_ts(last_reply)).total_seconds() / 60
    if elapsed_minutes < persona["follow_up_minutes"]:
        return

    # Time to follow up. Advance the mood first so the follow-up reflects it.
    new_mood = persona_svc.advance_mood(persona["current_mood"])
    db.update_persona(ticket_id, current_mood=new_mood, follow_up_count=persona["follow_up_count"] + 1)

    updated_persona = db.get_persona(ticket_id)
    scenario = catalog.persona_scenario(ticket)
    system_prompt = persona_svc.build_system_prompt(updated_persona, scenario)

    history = _build_history(public_articles)
    history.append({"role": "user", "content": (
        "You haven't heard back in a while. Send a follow-up message checking on the status of your ticket. "
        "Your tone should reflect your current mood."
    )})

    follow_up = llm.generate_persona_message(system_prompt, history)
    zammad.add_article(ticket["zammad_ticket_id"], follow_up, internal=False)
    db.update_ticket(ticket_id, last_persona_reply_at=_now())


def _build_history(public_articles):
    """
    Converts Zammad articles into the message history format the LLM expects.
    ai_user articles = persona's own prior messages → "assistant" role.
    All other articles = operator messages → "user" role (what the persona is responding to).
    This works correctly because operator replies now use a separate Zammad account.
    """
    history = []
    for article in public_articles:
        role = "assistant" if article.get("created_by_id") == _ai_user_id else "user"
        history.append({"role": role, "content": article.get("body", "")})
    return history


def _cleanup_old_tickets(app):
    """Deletes graded/closed tickets older than CLEANUP_AFTER_HOURS from Zammad and SQLite."""
    with app.app_context():
        tickets = db.get_tickets_older_than(CLEANUP_AFTER_HOURS)
        if not tickets:
            return
        print(f"[cleanup] Found {len(tickets)} ticket(s) to remove.")
        for ticket in tickets:
            try:
                zammad.delete_ticket(ticket["zammad_ticket_id"])
            except Exception as e:
                print(f"[cleanup] Zammad delete failed for ticket {ticket['id']}: {e}")
            if ticket.get("lab_session_id"):
                lab.destroy(ticket["lab_session_id"])   # normally gone already; harmless if so
            db.delete_ticket_cascade(ticket["id"])
            print(f"[cleanup] Removed ticket {ticket['id']} (Zammad #{ticket['zammad_ticket_id']})")


def run_cleanup_now(app):
    """Called by the manual admin endpoint to trigger cleanup immediately."""
    _cleanup_old_tickets(app)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts_str):
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
