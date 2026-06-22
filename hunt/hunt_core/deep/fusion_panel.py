"""Human-readable fusion panel for deep analysis."""
from __future__ import annotations

from typing import Any


def format_fusion_panel(fusion: dict[str, Any]) -> str:
    if not fusion:
        return ""
    lines = [
        "🧬 <b>Manipulation fusion</b>",
        f"Archetype: <code>{fusion.get('archetype', 'none')}</code>",
        f"Scores — predump <code>{fusion.get('score_predump', 0):.0f}</code> "
        f"coil <code>{fusion.get('score_coil', 0):.0f}</code> "
        f"ignition <code>{fusion.get('score_ignition', 0):.0f}</code>",
        f"OI regime: <code>{fusion.get('oi_regime', 'unknown')}</code>",
    ]
    pc = fusion.get("pass_count")
    req = fusion.get("required_n")
    if pc is not None and req:
        lines.append(f"Playbook: <code>{pc}/{req}</code> checks")
    checks = fusion.get("checks") if isinstance(fusion.get("checks"), dict) else {}
    sources = fusion.get("check_sources") if isinstance(fusion.get("check_sources"), dict) else {}
    if checks:
        hit = [k for k, v in checks.items() if v][:6]
        if hit:
            lines.append("Checks: " + ", ".join(hit))
        src_parts = [f"{k}({sources.get(k, '?')})" for k in hit[:4]]
        if src_parts:
            lines.append("Sources: " + ", ".join(src_parts))
    factors = fusion.get("factors") or []
    if factors:
        top = factors[:5]
        parts = [f"{f.get('domain', '?')}:{f.get('name', '?')}" for f in top if isinstance(f, dict)]
        if parts:
            lines.append("Top factors: " + ", ".join(parts))
    return "\n".join(lines)


__all__ = ["format_fusion_panel"]
