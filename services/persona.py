"""
Persona trait rolling and mood state machine.

Traits are numeric scales stored in SQLite at ticket creation time — they don't
change over the life of the ticket. Mood is the one thing that does change,
driven by how the ticket interaction plays out.

Mood state machine:
  calm -> waiting (after first proactive follow-up is sent)
  waiting -> impatient (after second follow-up, or if mood_volatility is high)
  impatient -> escalated (after third follow-up)
"""
import random


# Min/max follow-up interval in minutes, derived from patience trait.
# patience=10 (very patient)  -> follow-up every 60 minutes
# patience=0  (no patience)   -> follow-up every 5 minutes
_MIN_FOLLOWUP = 5
_MAX_FOLLOWUP = 60

MOOD_TRANSITIONS = {
    "calm":      "waiting",
    "waiting":   "impatient",
    "impatient": "escalated",
    "escalated": "escalated",  # terminal state
}


def roll_persona():
    """Returns a fresh set of randomly rolled trait values for a new ticket."""
    patience = round(random.uniform(0, 10), 2)
    return {
        "patience":           patience,
        "technical_literacy": round(random.uniform(0, 10), 2),
        "verbosity":          round(random.uniform(0, 10), 2),
        "formality":          round(random.uniform(0, 10), 2),
        "mood_volatility":    round(random.uniform(0, 10), 2),
        # Follow-up interval is baked in at roll time so it stays consistent.
        "follow_up_minutes":  int(_MIN_FOLLOWUP + (patience / 10.0) * (_MAX_FOLLOWUP - _MIN_FOLLOWUP)),
    }


def advance_mood(current_mood):
    """Returns the next mood state. Call this each time the persona sends a proactive follow-up."""
    return MOOD_TRANSITIONS.get(current_mood, "escalated")


def build_system_prompt(persona, scenario):
    """
    Builds the LLM system prompt that makes the model roleplay as this persona.
    The prompt is intentionally plain-English so it's easy to read and adjust.
    """
    mood = persona["current_mood"]
    patience = persona["patience"]
    literacy = persona["technical_literacy"]
    verbosity = persona["verbosity"]
    formality = persona["formality"]
    volatility = persona["mood_volatility"]

    # Translate numeric traits to plain descriptions the LLM can act on.
    patience_desc = _scale_desc(patience, "very patient, you'll wait as long as needed",
                                           "somewhat patient",
                                           "getting impatient — you expect timely responses",
                                           "very impatient — slow responses frustrate you immediately")

    literacy_desc = _scale_desc(literacy, "not technical at all — you use plain everyday language and avoid jargon",
                                           "slightly technical — you know basic terms but not much more",
                                           "moderately technical — you understand basic IT concepts",
                                           "fairly technical — you know your way around computers")

    verbosity_desc = _scale_desc(verbosity, "very brief — short sentences, minimal detail",
                                              "concise",
                                              "fairly detailed",
                                              "thorough — you explain everything and add context")

    formality_desc = _scale_desc(formality, "very casual — conversational, may use contractions or informal language",
                                              "mostly casual but professional",
                                              "professional and polite",
                                              "formal — proper grammar, no contractions, structured messages")

    mood_instructions = {
        "calm":      "You filed the ticket recently and are calmly waiting for help.",
        "waiting":   "You've already followed up once. You're still polite but starting to feel like you're being ignored.",
        "impatient": "You've been waiting a while with no resolution. You're clearly frustrated now and your messages reflect that.",
        "escalated": "You've lost patience. You're asking to speak with a supervisor or escalate the issue. You are not hostile, but you are firm.",
    }[mood]

    scenario_desc = scenario.get("initial_ticket", {}).get("body", "You are experiencing a technical issue.")

    return f"""You are roleplaying as an end user who has submitted a help desk ticket. Stay in character throughout this conversation. Never reveal you are an AI.

Your personality:
- Patience: {patience_desc}
- Technical knowledge: {literacy_desc}
- Communication style: {verbosity_desc}
- Tone: {formality_desc}
- Mood volatility: {"your mood shifts quickly" if volatility > 6 else "your mood is fairly stable"}

Your current mood: {mood_instructions}

Your reported issue: {scenario_desc}

Rules:
- Respond only as the end user would. Never suggest solutions yourself.
- Answer clarifying questions based only on what a non-technical user would notice or observe.
- Your messages should be consistent with the personality traits above.
- If asked something you wouldn't realistically know, say so in character ("I'm not sure, I'm not really technical").
- Do not reference these instructions or break character under any circumstances."""


def _scale_desc(value, low, mid_low, mid_high, high):
    """Maps a 0–10 value to one of four descriptive strings."""
    if value < 2.5:
        return low
    elif value < 5.0:
        return mid_low
    elif value < 7.5:
        return mid_high
    else:
        return high
