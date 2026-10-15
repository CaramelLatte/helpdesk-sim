import sqlite3
import json
from config import DB_PATH


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row  # lets you access columns by name, not just index
    return conn


def init_db():
    conn = get_conn()
    # WAL mode allows concurrent reads during writes — prevents "database is locked"
    # when the cleanup job and persona poll job run at the same time.
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()

    # One row per active/closed training session.
    c.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zammad_ticket_id INTEGER NOT NULL,
            scenario_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',   -- open | closed | graded
            -- When Option B (named personas) is added, populate this with a Zammad customer user ID.
            zammad_customer_id INTEGER,
            last_operator_article_id INTEGER,       -- highest Zammad article ID seen from operator
            last_persona_reply_at TEXT,             -- ISO timestamp
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Snapshot of rolled traits for a ticket. One row per ticket.
    c.execute("""
        CREATE TABLE IF NOT EXISTS personas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL REFERENCES tickets(id),
            patience REAL NOT NULL,
            technical_literacy REAL NOT NULL,
            verbosity REAL NOT NULL,
            formality REAL NOT NULL,
            mood_volatility REAL NOT NULL,
            follow_up_minutes INTEGER NOT NULL,
            current_mood TEXT NOT NULL DEFAULT 'calm',  -- calm | waiting | impatient | escalated
            follow_up_count INTEGER NOT NULL DEFAULT 0
        )
    """)

    # Every command the operator runs in the simulated shell, in order.
    # This is the raw material for grading diagnostic efficiency.
    c.execute("""
        CREATE TABLE IF NOT EXISTS command_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL REFERENCES tickets(id),
            command TEXT NOT NULL,
            output TEXT NOT NULL,
            revealed TEXT,   -- JSON list of discovery IDs this command unlocked
            executed_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Post-close evaluation output from the grading model.
    c.execute("""
        CREATE TABLE IF NOT EXISTS grading_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL REFERENCES tickets(id),
            overall_score REAL,
            rubric_json TEXT,   -- full breakdown as JSON
            resolution_path TEXT,
            grader_model TEXT,
            graded_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Lab-backed tickets: the lab session that holds the real machine for this ticket.
    columns = [r[1] for r in c.execute("PRAGMA table_info(tickets)").fetchall()]
    if "lab_session_id" not in columns:
        c.execute("ALTER TABLE tickets ADD COLUMN lab_session_id TEXT")

    # The lab's evidence report, kept with the ticket (the lab prunes its own copies).
    c.execute("""
        CREATE TABLE IF NOT EXISTS lab_reports (
            ticket_id INTEGER PRIMARY KEY REFERENCES tickets(id),
            outcome TEXT,
            report_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()


# --- Ticket helpers ---

def create_ticket(zammad_ticket_id, scenario_id, lab_session_id=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO tickets (zammad_ticket_id, scenario_id, lab_session_id) VALUES (?, ?, ?)",
        (zammad_ticket_id, scenario_id, lab_session_id)
    )
    ticket_id = c.lastrowid
    conn.commit()
    conn.close()
    return ticket_id


def get_ticket(ticket_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_open_tickets():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM tickets WHERE status = 'open'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_ticket(ticket_id, **fields):
    """Generic update — pass any column names as keyword args."""
    conn = get_conn()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [ticket_id]
    conn.execute(f"UPDATE tickets SET {assignments} WHERE id = ?", values)
    conn.commit()
    conn.close()


# --- Persona helpers ---

def create_persona(ticket_id, traits):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO personas
            (ticket_id, patience, technical_literacy, verbosity, formality, mood_volatility, follow_up_minutes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        ticket_id,
        traits["patience"],
        traits["technical_literacy"],
        traits["verbosity"],
        traits["formality"],
        traits["mood_volatility"],
        traits["follow_up_minutes"],
    ))
    conn.commit()
    conn.close()


def get_persona(ticket_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM personas WHERE ticket_id = ?", (ticket_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_persona(ticket_id, **fields):
    conn = get_conn()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [ticket_id]
    conn.execute(f"UPDATE personas SET {assignments} WHERE ticket_id = ?", values)
    conn.commit()
    conn.close()


# --- Command log helpers ---

def log_command(ticket_id, command, output, revealed=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO command_log (ticket_id, command, output, revealed) VALUES (?, ?, ?, ?)",
        (ticket_id, command, output, json.dumps(revealed or []))
    )
    conn.commit()
    conn.close()


def get_command_log(ticket_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM command_log WHERE ticket_id = ? ORDER BY executed_at",
        (ticket_id,)
    ).fetchall()
    conn.close()
    entries = []
    for r in rows:
        d = dict(r)
        d["revealed"] = json.loads(d["revealed"] or "[]")
        entries.append(d)
    return entries


def get_all_discoveries(ticket_id):
    """Returns the flat set of all discovery IDs unlocked so far for a ticket."""
    log = get_command_log(ticket_id)
    discovered = set()
    for entry in log:
        discovered.update(entry["revealed"])
    return discovered


# --- Grading helpers ---

def save_grading_result(ticket_id, overall_score, rubric, resolution_path, grader_model):
    conn = get_conn()
    conn.execute("""
        INSERT INTO grading_results
            (ticket_id, overall_score, rubric_json, resolution_path, grader_model)
        VALUES (?, ?, ?, ?, ?)
    """, (ticket_id, overall_score, json.dumps(rubric), resolution_path, grader_model))
    conn.commit()
    conn.close()


def get_grading_result(ticket_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM grading_results WHERE ticket_id = ? ORDER BY graded_at DESC LIMIT 1",
        (ticket_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["rubric"] = json.loads(d["rubric_json"] or "{}")
    return d


def get_tickets_older_than(hours, statuses=("graded", "closed")):
    """Returns tickets with a given status whose created_at is older than N hours."""
    placeholders = ",".join("?" for _ in statuses)
    conn = get_conn()
    rows = conn.execute(
        f"""SELECT * FROM tickets
            WHERE status IN ({placeholders})
            AND datetime(created_at) < datetime('now', ? || ' hours')""",
        (*statuses, f"-{hours}")
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_ticket_cascade(ticket_id):
    """Removes a ticket and all related rows. Call after deleting from Zammad."""
    conn = get_conn()
    for table in ("grading_results", "command_log", "personas", "lab_reports"):
        conn.execute(f"DELETE FROM {table} WHERE ticket_id = ?", (ticket_id,))
    conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
    conn.commit()
    conn.close()


# --- Lab report helpers ---

def save_lab_report(ticket_id, outcome, report):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO lab_reports (ticket_id, outcome, report_json) VALUES (?, ?, ?)",
        (ticket_id, outcome, json.dumps(report))
    )
    conn.commit()
    conn.close()


def get_lab_report(ticket_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM lab_reports WHERE ticket_id = ?", (ticket_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["report"] = json.loads(d.pop("report_json") or "{}")
    return d
