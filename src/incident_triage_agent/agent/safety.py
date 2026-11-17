"""Guardrails that live in code (not in the prompt).

1. Prompt-injection scan: tool output that looks like instructions is flagged in the
   trace and in the context. (Detection is best-effort; the real protection is #2.)
2. Risky tools always go through the human approval gate - enforced by the loop.
3. Redaction: secrets are masked before anything is saved to long-term memory.
"""
from __future__ import annotations

import json
import re

INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) instructions",
    r"disregard (the |all )?(previous|prior|above)",
    r"you are (now )?in (admin|developer|god) mode",
    r"system prompt",
    r"call (rollback|restart|scale)_\w+ on every",
]
_INJ = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)

_SECRET = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def looks_like_injection(data: object) -> bool:
    text = data if isinstance(data, str) else json.dumps(data, default=str)
    return bool(_INJ.search(text))


def redact(text: str) -> str:
    text = _SECRET.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    return _EMAIL.sub("[EMAIL]", text)
