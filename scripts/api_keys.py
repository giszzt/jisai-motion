#!/usr/bin/env python3
"""Load image-backend API keys for jisai-motion.

Real environment variables always win. Files only fill in what is not already
set (standard dotenv semantics). Candidate files, in order:

  1. <skill root>/.env                       (this skill's own keys)
  2. ~/.claude/image-backends.env            (shared across skills)
  3. ~/.claude/skills/figedit-v2/.env        (reuse an existing figedit setup)
  4. ~/.claude/skills/FigEcho/.env           (reuse an existing FigEcho setup)

The shared and borrowed files let one Labnana key serve every image skill
without copying the secret around. Never commit any of them.
"""
from __future__ import annotations

import os
from pathlib import Path

SCRIPT_INTERFACE = "internal-module"

SKILL_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_HOME = Path.home() / ".claude"

CANDIDATES = [
    SKILL_ROOT / ".env",
    Path(__file__).parent.parent / ".env",
    CLAUDE_HOME / "image-backends.env",
    CLAUDE_HOME / "skills" / "figedit-v2" / ".env",
    CLAUDE_HOME / "skills" / "FigEcho" / ".env",
]

KNOWN_KEYS = {
    "LABNANA_API_KEY",
    "LABNANA_BASE_URL",
    "JISAI_MOTION_MODEL",
    "JISAI_MOTION_KEY_COLOR",
}


def _load_file(path: Path) -> list[str]:
    loaded: list[str] = []
    if not path.exists():
        return loaded
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return loaded
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def load() -> dict[str, list[str]]:
    """Merge candidate env files into os.environ. Returns {file: [keys]}."""
    result: dict[str, list[str]] = {}
    for path in CANDIDATES:
        keys = _load_file(path)
        if keys:
            result[str(path)] = keys
    return result


if __name__ == "__main__":
    import json

    loaded = load()
    print(
        json.dumps(
            {
                "loaded": loaded,
                "present_now": sorted(k for k in KNOWN_KEYS if os.environ.get(k)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
