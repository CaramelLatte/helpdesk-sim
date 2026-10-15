"""
LM Studio client. LM Studio exposes an OpenAI-compatible API, so we use the openai
package pointed at localhost instead of Anthropic's servers.
"""
from openai import OpenAI
from config import LM_STUDIO_URL, PERSONA_MODEL, GRADING_MODEL

# The api_key value doesn't matter for LM Studio — it just needs to be a non-empty string.
_client = OpenAI(base_url=LM_STUDIO_URL, api_key="lm-studio")


def _chat(model, messages, temperature=0.7, max_tokens=512):
    response = _client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content.strip()


def generate_persona_message(system_prompt, conversation_history):
    """
    Generates the persona's next message.
    conversation_history: list of {"role": "user"|"assistant", "content": "..."} dicts.
    'user' role = the operator's messages; 'assistant' role = the persona's prior messages.
    """
    messages = [{"role": "system", "content": system_prompt}] + conversation_history
    return _chat(PERSONA_MODEL, messages, temperature=0.75, max_tokens=1024)


def generate_grading(system_prompt, grading_prompt, max_tokens=1024):
    """
    Runs the grading model against the completed ticket.
    Returns the raw text response — the caller parses it.
    max_tokens: lab grading passes more, since the reasoning model reads a long evidence
    report and its reasoning counts against this limit.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": grading_prompt},
    ]
    # Grading needs more room to write a detailed rubric breakdown.
    return _chat(GRADING_MODEL, messages, temperature=0.2, max_tokens=max_tokens)
