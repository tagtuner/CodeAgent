from __future__ import annotations
import re
from .loader import Skill, SkillLoader


class SkillManager:
    """Manages skill activation per session and builds context for prompt injection."""

    def __init__(self, skills_dir: str):
        self.all_skills = SkillLoader.load_dir(skills_dir)
        self.active_skills: list[Skill] = []

    def activate(self, skill_name: str) -> bool:
        for s in self.all_skills:
            if s.name == skill_name:
                if s not in self.active_skills:
                    self.active_skills.append(s)
                return True
        return False

    def deactivate(self, skill_name: str) -> bool:
        before = len(self.active_skills)
        self.active_skills = [s for s in self.active_skills if s.name != skill_name]
        return len(self.active_skills) < before

    def auto_activate(self, message: str):
        """Activate skills whose trigger keywords match the user message."""
        msg_lower = message.lower()
        for skill in self.all_skills:
            if skill in self.active_skills:
                continue
            for kw in skill.trigger_keywords:
                if kw.lower() in msg_lower:
                    self.active_skills.append(skill)
                    break

    def get_context(self, max_chars: int = 2000) -> str:
        """Build a compact skills context string for prompt injection."""
        if not self.active_skills:
            return ""
        parts = []
        total = 0
        for s in self.active_skills:
            text = f"## Skill: {s.name}\n{s.compact}\n"
            if total + len(text) > max_chars:
                break
            parts.append(text)
            total += len(text)
        return "\n".join(parts)

    def list_all(self) -> list[dict]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "tags": s.tags,
                "active": s in self.active_skills,
            }
            for s in self.all_skills
        ]

    def list_active(self) -> list[str]:
        return [s.name for s in self.active_skills]

    def clear(self):
        self.active_skills.clear()
