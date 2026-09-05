"""
Standalone diagnostic shell client.

Usage:
  python shell_client.py --ticket <local_ticket_id>

This presents a fake terminal prompt. Commands you type are sent to the Flask
server, which matches them against the scenario and returns simulated output.
Commands are logged locally and attached to the Zammad ticket as a single
internal note when you close the session.

Type 'log' to see commands run so far.
Type 'close' to submit the ticket and receive your performance grade.
Type 'exit' or press Ctrl-C to quit without closing.
"""
import argparse
import requests
import sys
import json

DEFAULT_SERVER = "http://localhost:5001"

# Terminal prompt per scenario shell flavor — matches what the simulated OS would show.
PROMPTS = {
    "bash":       "$ ",
    "zsh":        "% ",
    "cmd":        "C:\\> ",
    "powershell": "PS C:\\> ",
}


def main():
    parser = argparse.ArgumentParser(description="AI Ticket diagnostic shell simulator")
    parser.add_argument("--ticket", required=True, type=int, help="Local ticket ID to work against")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="Flask server URL")
    args = parser.parse_args()

    ticket_id = args.ticket
    server = args.server.rstrip("/")

    # Verify the ticket exists before dropping into the prompt.
    try:
        r = requests.get(f"{server}/api/tickets/{ticket_id}")
        r.raise_for_status()
        info = r.json()
    except requests.RequestException as e:
        print(f"Error: could not reach server at {server} — {e}")
        sys.exit(1)

    ticket = info["ticket"]
    if ticket["status"] != "open":
        print(f"Ticket #{ticket_id} is {ticket['status']}. Only open tickets can be worked.")
        sys.exit(1)

    # Ask the server which shell flavor this scenario simulates so the prompt matches.
    prompt = PROMPTS["bash"]
    try:
        r = requests.get(f"{server}/api/shell/{ticket_id}/commands")
        if r.ok:
            prompt = PROMPTS.get(r.json().get("shell", "bash"), PROMPTS["bash"])
    except requests.RequestException:
        pass  # cosmetic only — fall back to the bash prompt

    print(f"\n=== AI Ticket Shell — Ticket #{ticket_id} ({ticket['scenario_id']}) ===")
    print("Type diagnostic commands as you would in a real terminal.")
    print("Type 'log' to review commands run so far.")
    print("Type 'close' when done to submit the ticket and receive your grade.")
    print("Type 'exit' to quit without closing.\n")

    while True:
        try:
            command = input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting shell.")
            break

        if not command:
            continue

        if command.lower() == "exit":
            print("Exiting shell.")
            break

        if command.lower() == "log":
            _show_log(server, ticket_id)
            continue

        if command.lower() == "close":
            _close_ticket(server, ticket_id)
            break

        # Send command to the Flask server.
        try:
            r = requests.post(
                f"{server}/api/shell/{ticket_id}/command",
                json={"command": command}
            )
            if r.status_code == 400:
                print(r.json().get("error", "Bad request"))
                continue
            r.raise_for_status()
            result = r.json()
        except requests.RequestException as e:
            print(f"Server error: {e}")
            continue

        # Print the output exactly as a real terminal would.
        print(result["output"])

        if not result["matched"]:
            print("(Command not recognized by this scenario's simulated environment)")

        print()


def _close_ticket(server, ticket_id):
    print("\nBefore continuing, make sure you have already taken your final action in Zammad:")
    print("  - Resolved by T1: mark the ticket CLOSED in the Zammad UI")
    print("  - Escalated: reassign the ticket to the appropriate group in the Zammad UI")
    print()
    notes = input("Resolution notes (what did you find / what action did you take?): ").strip()
    print("\nRunning grading model — this may take a moment...")
    try:
        r = requests.post(
            f"{server}/api/tickets/{ticket_id}/close",
            json={"resolution_notes": notes}
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        print(f"Error: {e}")
        return

    resolution = data.get("resolution_path", {})
    grading = data.get("grading", {})

    print("\n" + "=" * 60)
    print("PERFORMANCE REVIEW")
    print("=" * 60)
    print(f"\nZammad state at close:  {data.get('zammad_state', '—')}")
    print(f"Assigned group:         {data.get('zammad_group', '—')}")
    print(f"Resolution path:        {resolution.get('label', 'Unknown')}")
    print(f"Path score modifier:    {resolution.get('score_modifier', 0) * 100:.0f}/100")
    print()
    if "diagnostic_accuracy" in grading:
        print(f"Diagnostic accuracy:    {grading['diagnostic_accuracy']}")
        print(f"Diagnostic efficiency:  {grading['diagnostic_efficiency']}")
        print(f"Escalation judgment:    {grading['escalation_judgment']}")
        print(f"Response timeliness:    {grading.get('response_timeliness', '—')}")
        print(f"Customer service:       {grading.get('customer_service', '—')}")
        print(f"Overall score:          {grading.get('overall_score', '—')}")
    if "feedback" in grading:
        print(f"\nFeedback:\n  {grading['feedback']}")
    print("=" * 60 + "\n")


def _show_log(server, ticket_id):
    try:
        r = requests.get(f"{server}/api/shell/{ticket_id}/log")
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        print(f"Could not fetch log: {e}")
        return

    log = data.get("command_log", [])
    if not log:
        print("(No commands run yet)\n")
        return

    print(f"\n--- Command Log ({len(log)} commands) ---")
    for entry in log:
        marker = " [!]" if entry["revealed"] else ""
        print(f"  {entry['executed_at'][:19]}  $ {entry['command']}{marker}")
    discoveries = data.get("discoveries", [])
    if discoveries:
        print(f"\nDiscoveries so far: {', '.join(discoveries)}")
    print()


if __name__ == "__main__":
    main()
