"""Human and JSON renderers over one typed result."""

from __future__ import annotations

import json

from .models import CommandResult


def render_json(result: CommandResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def render_human(result: CommandResult) -> str:
    lines = [result.message, f"Status: {result.status}"]
    if result.error_kind is not None:
        lines.append(f"Error: {result.error_kind}")
    if result.repair is not None:
        lines.append(f"Repair: {result.repair}")
    if result.data:
        lines.append("Details:")
        lines.append(json.dumps(result.data, ensure_ascii=False, indent=2, sort_keys=True))
    if result.evidence:
        lines.append("Evidence:")
        lines.append(
            json.dumps(
                [item.to_dict() for item in result.evidence],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    return "\n".join(lines)
