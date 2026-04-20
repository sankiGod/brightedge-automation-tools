"""
Skill Registry — auto-discovers all Skill subclasses in skills/.
No hardcoding. Adding a new skill = dropping a file in skills/.
"""

import importlib
import pkgutil
import skills
from skills.base import Skill


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, Skill] = {}
        self._discover()

    def _discover(self):
        for _, module_name, _ in pkgutil.iter_modules(skills.__path__):
            if module_name == "base":
                continue
            module = importlib.import_module(f"skills.{module_name}")
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Skill)
                    and attr is not Skill
                ):
                    instance = attr()
                    self._skills[instance.name] = instance

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all(self) -> dict[str, Skill]:
        return self._skills

    def descriptions(self) -> list[dict]:
        """Returns skill name, description, and input_schema for the orchestrator prompt."""
        return [
            {"name": s.name, "description": s.description, "input_schema": s.input_schema()}
            for s in self._skills.values()
        ]
