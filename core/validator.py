"""
Validation Layer — runs before any skill execution.
Checks inputs, confidence threshold, and skill-level validation.
"""

CONFIDENCE_THRESHOLD = 0.8


def validate(decision: dict, skill) -> dict:
    """
    Returns {"valid": bool, "errors": [str], "action": "proceed"|"review"|"reject"}
    """
    errors = []

    # Confidence check
    confidence = decision.get("confidence", 0)
    if confidence < CONFIDENCE_THRESHOLD:
        errors.append(
            f"Confidence {confidence:.2f} below threshold {CONFIDENCE_THRESHOLD}"
        )

    # Missing fields check
    missing = decision.get("missing_fields", [])
    if missing:
        errors.append(f"Missing required fields: {', '.join(missing)}")

    # Skill-level input validation
    if not errors:
        skill_validation = skill.validate(decision.get("inputs", {}))
        if not skill_validation.get("valid"):
            errors.extend(skill_validation.get("errors", []))

    if not errors:
        return {"valid": True, "errors": [], "action": "proceed"}

    # Confidence too low → human review; missing data → review; other → reject
    action = "review" if confidence < CONFIDENCE_THRESHOLD or missing else "reject"
    return {"valid": False, "errors": errors, "action": action}
