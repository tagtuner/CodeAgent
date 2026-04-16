from __future__ import annotations
import re
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Skill:
    name: str
    description: str
    content: str
    path: str
    tags: list[str]
    trigger_keywords: list[str]

    @property
    def compact(self) -> str:
        """Truncated version for prompt injection (max ~500 chars)."""
        if len(self.content) <= 500:
            return self.content
        return self.content[:500] + "\n... (truncated)"


class SkillLoader:
    """Load skill definitions from .md files in a directory."""

    @staticmethod
    def load_dir(skills_dir: str | Path) -> list[Skill]:
        d = Path(skills_dir)
        if not d.exists():
            return []
        skills = []
        for f in sorted(d.glob("**/*.md")):
            try:
                skill = SkillLoader._parse_skill(f)
                if skill:
                    skills.append(skill)
            except Exception:
                continue
        return skills

    @staticmethod
    def _parse_skill(path: Path) -> Skill | None:
        text = path.read_text(errors="replace")
        name = path.stem
        description = ""
        tags: list[str] = []
        trigger_keywords: list[str] = []
        content_lines: list[str] = []
        in_frontmatter = False
        frontmatter_done = False

        for line in text.splitlines():
            stripped = line.strip()
            if stripped == "---" and not frontmatter_done:
                if not in_frontmatter:
                    in_frontmatter = True
                    continue
                else:
                    in_frontmatter = False
                    frontmatter_done = True
                    continue
            if in_frontmatter:
                if stripped.startswith("name:"):
                    name = stripped[5:].strip().strip('"\'')
                elif stripped.startswith("description:"):
                    description = stripped[12:].strip().strip('"\'')
                elif stripped.startswith("tags:"):
                    tags_str = stripped[5:].strip().strip("[]")
                    tags = [t.strip().strip('"\'') for t in tags_str.split(",") if t.strip()]
                elif stripped.startswith("triggers:") or stripped.startswith("keywords:"):
                    kw_str = stripped.split(":", 1)[1].strip().strip("[]")
                    trigger_keywords = [t.strip().strip('"\'') for t in kw_str.split(",") if t.strip()]
                continue
            content_lines.append(line)

        content = "\n".join(content_lines).strip()
        if not content:
            return None

        if not description:
            first_line = content.split("\n")[0].strip("# ").strip()
            description = first_line[:100]

        return Skill(
            name=name,
            description=description,
            content=content,
            path=str(path),
            tags=tags,
            trigger_keywords=trigger_keywords,
        )
