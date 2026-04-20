"""
Base Skill class. All skills must inherit from this and implement all three methods.
"""


class Skill:
    name: str = "base"
    description: str = "Base skill — do not use directly"

    def input_schema(self) -> dict:
        """
        Returns the fields the orchestrator must extract from the ticket.
        Example: {"username": str, "account_id": str, "filename": str}
        """
        return {}

    def validate(self, inputs: dict) -> dict:
        """
        Validates inputs before execution.
        Returns {"valid": bool, "errors": [str]}
        """
        return {"valid": True, "errors": []}

    def execute(self, inputs: dict) -> dict:
        """
        Executes the skill. Returns {"status": "success"|"failure", ...}
        This is the ONLY place that calls Playwright, APIs, or external services.
        """
        raise NotImplementedError(f"Skill '{self.name}' must implement execute()")
