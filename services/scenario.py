"""
Loads scenario YAML files and handles command matching.

Command matching works like this:
  1. The operator types a command (e.g. "ping 8.8.8.8")
  2. We test it against each command's match_pattern (a Python regex)
  3. The first match wins — we format the output template and return it
  4. If nothing matches, we return an OS-appropriate "command not found" response
     (the scenario's optional `shell:` field picks the flavor — bash, zsh, cmd, powershell)

A matched command may also carry `conditional_outputs`: a list of alternate outputs
that take over once certain discoveries have been made in the session. This is how
scenarios stay stateful — e.g. after the operator runs `faillock --reset`, re-running
`faillock --user X` shows an empty table instead of the original lockout entries.
Each entry has `requires` (a discovery ID or list of IDs that must all be present),
its own `output`, and optionally its own `reveals`. First satisfied entry wins;
if none are satisfied, the command's base output is used.

Output templates use {variable} placeholders filled from two sources:
  - scenario system_state (e.g. {hostname}, {bad_dns})
  - the command itself: {target} is always set to whatever word follows the command verb
"""
import os
import re
import yaml
from config import SCENARIOS_DIR


_cache = {}  # scenario_id -> loaded dict, so we don't re-read the file every call

# "Command not found" text per shell flavor. A scenario opts in with `shell: cmd` etc.
# Defaults to bash so existing Linux scenarios don't need to change.
NOT_FOUND_TEMPLATES = {
    "bash":       "bash: {verb}: command not found",
    "zsh":        "zsh: command not found: {verb}",
    "cmd":        "'{verb}' is not recognized as an internal or external command,\noperable program or batch file.",
    "powershell": ("{verb} : The term '{verb}' is not recognized as the name of a cmdlet, "
                   "function, script file, or operable program."),
}


def load_scenario(scenario_id):
    if scenario_id not in _cache:
        path = os.path.join(SCENARIOS_DIR, f"{scenario_id}.yaml")
        if not os.path.exists(path):
            raise FileNotFoundError(f"No scenario file found: {path}")
        with open(path) as f:
            _cache[scenario_id] = yaml.safe_load(f)
    return _cache[scenario_id]


def list_scenarios():
    """Returns a list of available scenario IDs (filenames without .yaml)."""
    if not os.path.isdir(SCENARIOS_DIR):
        return []
    return [f[:-5] for f in os.listdir(SCENARIOS_DIR) if f.endswith(".yaml")]


def run_command(scenario_id, command_text, discoveries=None):
    """
    Matches command_text against the scenario's command list.
    `discoveries` is the set of discovery IDs already unlocked this session —
    used to select conditional outputs (e.g. post-fix verification output).
    Returns a dict: {output: str, reveals: str|None, matched: bool}
    """
    scenario = load_scenario(scenario_id)
    command_text = command_text.strip()
    discoveries = set(discoveries or [])

    for cmd in scenario.get("commands", []):
        pattern = cmd.get("match_pattern", "")
        if re.match(pattern, command_text, re.IGNORECASE):
            # Default to the base output, then check whether session state
            # (accumulated discoveries) switches us to a conditional one.
            spec = cmd
            for cond in cmd.get("conditional_outputs", []):
                required = cond.get("requires", [])
                if isinstance(required, str):
                    required = [required]
                if set(required).issubset(discoveries):
                    spec = cond
                    break
            output = _format_output(spec["output"], scenario, command_text)
            return {
                "output": output,
                "reveals": spec.get("reveals"),  # may be null/None for red herrings
                "matched": True,
                "command_id": cmd["id"],
            }

    # Nothing matched — return a realistic shell error for this scenario's OS.
    verb = command_text.split()[0] if command_text else "command"
    template = NOT_FOUND_TEMPLATES.get(scenario.get("shell", "bash"), NOT_FOUND_TEMPLATES["bash"])
    return {
        "output": template.format(verb=verb),
        "reveals": None,
        "matched": False,
        "command_id": None,
    }


def get_resolution_path(scenario_id, discoveries):
    """
    Given a set of discovery IDs accumulated during the session, finds the best
    matching resolution path and returns its id, label, and score_modifier.

    'Best' means the path whose required_discoveries are all present in `discoveries`
    AND which has the highest score_modifier (i.e. most complete diagnosis).
    """
    scenario = load_scenario(scenario_id)
    discoveries = set(discoveries)

    best = None
    for path in scenario.get("resolution_paths", []):
        required = set(path.get("required_discoveries", []))
        if required.issubset(discoveries):
            if best is None or path["score_modifier"] > best["score_modifier"]:
                best = path

    # If somehow nothing matched (shouldn't happen given blind_escalation has no requirements),
    # fall back to a zero-score default.
    return best or {"id": "unknown", "label": "No resolution path matched", "score_modifier": 0.0}


def _format_output(template, scenario, command_text):
    """Fills {variable} placeholders in the output template."""
    variables = dict(scenario.get("system_state", {}))

    # {target} = the first argument after the command verb (e.g. the domain or IP being pinged)
    parts = command_text.split()
    variables["target"] = parts[1] if len(parts) > 1 else ""

    # Fill in placeholders. Leave unknown ones as-is rather than crashing.
    try:
        return template.format_map(_SafeFormat(variables))
    except Exception:
        return template


class _SafeFormat(dict):
    """A dict subclass that returns {key} unchanged for missing keys instead of raising KeyError."""
    def __missing__(self, key):
        return f"{{{key}}}"
