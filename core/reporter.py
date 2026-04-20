"""
Reporter Agent — generates the Zendesk internal note from execution results.
One Claude call per ticket. Generic across all skills.
Falls back to rule-based mock if the API call fails.
"""

import anthropic
client = anthropic.Anthropic()

SYSTEM_PROMPT = """
You are a support automation reporter. Given the result of an automated action,
write a concise internal note for the Zendesk ticket.

Rules:
- Be factual and concise (3-6 lines max)
- Clearly state what was done and the outcome
- If there were errors, state what the agent should check or do next
- Do not use jargon the support agent wouldn't understand
- Plain text only, no markdown
"""


def build_note(skill_name: str, inputs: dict, result: dict,
               elapsed_seconds: int = 0) -> str:
    """Returns the internal note string to post on the Zendesk ticket."""
    # TODO: Re-enable AI-generated notes for production:
    # context = (
    #     f"Skill executed: {skill_name}\n"
    #     f"Inputs used: {inputs}\n"
    #     f"Result: {result}"
    # )
    # try:
    #     response = client.messages.create(
    #         model="claude-haiku-4-5-20251001",
    #         max_tokens=200,
    #         system=SYSTEM_PROMPT,
    #         messages=[{"role": "user", "content": context}],
    #     )
    #     return response.content[0].text.strip()
    # except Exception as e:
    #     print(f"  [Reporter] WARNING - API call failed ({e}), using mock note.")
    return _build_note_mock(skill_name, result, elapsed_seconds)


def _build_note_mock(skill_name: str, result: dict, elapsed_seconds: int) -> str:
    """
    Rule-based reporter mock -- no AI API required.
    Shows skill name, status, and time taken only.
    """
    status = result.get("status", "unknown")

    if elapsed_seconds >= 60:
        mins = elapsed_seconds // 60
        secs = elapsed_seconds % 60
        time_str = f"{mins}m {secs}s"
    else:
        time_str = f"{elapsed_seconds}s"

    return (
        f"Skill : {skill_name}\n"
        f"Status : {status.capitalize()}\n"
        f"Time taken : {time_str}"
    )
