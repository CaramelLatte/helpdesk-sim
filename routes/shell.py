from flask import Blueprint, request, jsonify
import db
import services.scenario as scenario_svc

shell_bp = Blueprint("shell", __name__)


@shell_bp.route("/api/shell/<int:ticket_id>/command", methods=["POST"])
def run_command(ticket_id):
    """
    Simulates running a diagnostic command on the scenario's virtual machine.
    Body (JSON): { "command": "ping 8.8.8.8" }
    Returns the simulated terminal output and logs the command to the ticket.
    """
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    if ticket["status"] != "open":
        return jsonify({"error": "This ticket is closed — no further commands can be run"}), 400

    data = request.get_json()
    command = (data.get("command") or "").strip()
    if not command:
        return jsonify({"error": "command is required"}), 400

    # Discoveries made so far feed conditional outputs — e.g. a verification
    # command shows the fixed state only after the fix command has been run.
    discoveries = db.get_all_discoveries(ticket_id)
    result = scenario_svc.run_command(ticket["scenario_id"], command, discoveries)

    # Log to SQLite only. The full command log is posted to Zammad as a single
    # summary note when the ticket closes, keeping the live ticket thread clean.
    revealed = [result["reveals"]] if result["reveals"] else []
    db.log_command(ticket_id, command, result["output"], revealed)

    return jsonify({
        "command": command,
        "output": result["output"],
        "matched": result["matched"],
        "reveals": result["reveals"],
    })


@shell_bp.route("/api/shell/<int:ticket_id>/commands", methods=["GET"])
def get_available_commands(ticket_id):
    """Returns the list of commands available in the ticket's scenario."""
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404

    try:
        scenario = scenario_svc.load_scenario(ticket["scenario_id"])
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404

    return jsonify({
        "scenario_id": ticket["scenario_id"],
        "shell": scenario.get("shell", "bash"),
        "available_commands": scenario.get("available_commands", []),
    })


@shell_bp.route("/api/shell/<int:ticket_id>/log", methods=["GET"])
def get_command_log(ticket_id):
    """Returns the full command history for a ticket."""
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404

    log = db.get_command_log(ticket_id)
    discoveries = list(db.get_all_discoveries(ticket_id))

    return jsonify({
        "ticket_id": ticket_id,
        "command_log": log,
        "discoveries": discoveries,
    })
