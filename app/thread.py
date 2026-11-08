"""Thread 1.4 role terminology.

Thread 1.4 renamed Router to "Mesh Extender" and REED to "Standby Mesh Extender".
Wire values and the Matter spec keep the old names, so both are shown.
"""

from __future__ import annotations

_THREAD_14_NAMES = {
    "router": "Mesh Extender",
    "reed": "Standby Mesh Extender",
}


def thread_role_label(role: str | None, detail: str | None = None) -> str | None:
    """Return e.g. 'Mesh Extender (Router)'; roles without a 1.4 rename pass through.

    *detail* is appended inside the parentheses (for example an RLOC16).
    """
    if not role:
        return None
    new_name = _THREAD_14_NAMES.get(role.strip().lower())
    if new_name is None:
        return f"{role} ({detail})" if detail else role
    inner = f"{role}, {detail}" if detail else role
    return f"{new_name} ({inner})"
