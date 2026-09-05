"""
Thin wrapper around the Zammad REST API.
All HTTP calls go through here so the rest of the app never touches requests directly.
"""
import requests
from config import ZAMMAD_URL, ZAMMAD_API_KEY, OPERATOR_API_KEY


def _headers():
    return {"Authorization": f"Token token={ZAMMAD_API_KEY}"}


def _operator_headers():
    return {"Authorization": f"Token token={OPERATOR_API_KEY}"}


def _url(path):
    return f"{ZAMMAD_URL}/api/v1/{path}"


def get_current_user():
    """Returns the Zammad user record for the API token owner (our ai_user account)."""
    r = requests.get(_url("users/me"), headers=_headers())
    r.raise_for_status()
    return r.json()


def create_ticket(subject, body, customer_email=None):
    """
    Creates a ticket in Zammad.
    customer_email: if None, the ticket is filed under the ai_user account (Option A).
                    When Option B named personas are added, pass the persona's email here.
    Returns the full ticket dict from Zammad.
    """
    payload = {
        "title": subject,
        "group": "Users",
        "article": {
            "subject": subject,
            "body": body,
            "type": "note",
            "internal": False,
            "sender": "Customer",
        }
    }
    if customer_email:
        payload["customer"] = customer_email

    r = requests.post(_url("tickets"), headers=_headers(), json=payload)
    r.raise_for_status()
    return r.json()


def add_article(zammad_ticket_id, body, internal=False, sender=None):
    """
    Posts a message to an existing ticket.
    internal=False  → visible in the thread (persona reply or operator reply)
    internal=True   → internal note only (shell command logs, etc.)
    sender          → "Customer" for persona messages, "Agent" for operator messages.
                      Defaults to "Customer" for public articles, "Agent" for internal notes.
    Returns the created article dict.
    """
    if sender is None:
        sender = "Agent" if internal else "Customer"
    payload = {
        "ticket_id": zammad_ticket_id,
        "body": body,
        "internal": internal,
        "type": "note",
        "sender": sender,
    }
    r = requests.post(_url("ticket_articles"), headers=_headers(), json=payload)
    r.raise_for_status()
    return r.json()


def add_article_as_operator(zammad_ticket_id, body):
    """
    Posts a reply on behalf of the human operator, using the operator's own API token.
    This gives the article a distinct created_by_id so the scheduler can tell it apart
    from persona messages, which are always posted by the ai_user account.
    """
    payload = {
        "ticket_id": zammad_ticket_id,
        "body": body,
        "internal": False,
        "type": "note",
        "sender": "Agent",
    }
    r = requests.post(_url("ticket_articles"), headers=_operator_headers(), json=payload)
    r.raise_for_status()
    return r.json()


def set_ticket_resolution(zammad_ticket_id, state=None, group=None):
    """
    Sets the ticket state, group, or both in a single call.
    Use state="closed" for a direct fix; group="Network Team" for an escalation.
    Both can be passed together.
    """
    payload = {}
    if state:
        payload["state"] = state
    if group:
        payload["group"] = group
    r = requests.put(_url(f"tickets/{zammad_ticket_id}"), headers=_headers(), json=payload)
    r.raise_for_status()
    return r.json()


def get_ticket_articles(zammad_ticket_id):
    """Returns all articles for a ticket, sorted by id ascending."""
    r = requests.get(
        _url(f"ticket_articles/by_ticket/{zammad_ticket_id}"),
        headers=_headers()
    )
    r.raise_for_status()
    articles = r.json()
    return sorted(articles, key=lambda a: a["id"])


def update_ticket_state(zammad_ticket_id, state):
    """
    Updates the Zammad ticket state.
    Common states: 'open', 'closed', 'pending reminder'
    """
    r = requests.put(
        _url(f"tickets/{zammad_ticket_id}"),
        headers=_headers(),
        json={"state": state}
    )
    r.raise_for_status()
    return r.json()


def get_ticket(zammad_ticket_id):
    r = requests.get(_url(f"tickets/{zammad_ticket_id}"), headers=_headers())
    r.raise_for_status()
    return r.json()


def get_ticket_state_name(state_id):
    """Resolves a numeric state_id to its human-readable name (e.g. 'closed', 'open')."""
    r = requests.get(_url(f"ticket_states/{state_id}"), headers=_headers())
    r.raise_for_status()
    return r.json().get("name", f"state_{state_id}")


def delete_ticket(zammad_ticket_id):
    """Permanently deletes a ticket and all its articles from Zammad."""
    r = requests.delete(_url(f"tickets/{zammad_ticket_id}"), headers=_headers())
    r.raise_for_status()


def get_group_name(group_id):
    """Resolves a numeric group_id to its name (e.g. 'Users', 'Network Team')."""
    r = requests.get(_url(f"groups/{group_id}"), headers=_headers())
    r.raise_for_status()
    return r.json().get("name", f"group_{group_id}")
