"""Optional autonomous analyst provider (Gemini free tier). Skipped when unset."""

from __future__ import annotations

import json
import os
from typing import Any

from intel.schema import empty_report, validate_intel_report


def gemini_available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


async def analyze_with_gemini(dossier_md: str, *, n_signals: int = 0) -> dict[str, Any] | None:
    """Call Gemini if API key set; otherwise return None."""
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return None

    try:
        import aiohttp
    except ImportError:
        return None

    prompt = (
        "You are an offline crypto signal research analyst. Read the dossier and return ONLY "
        "valid JSON with keys: hypotheses, threshold_suggestions, strategy_gaps, risk_flags, meta. "
        "Propose only — never claim changes were applied. Respect small-n guardrails.\n\n"
        + dossier_md
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, params={"key": api_key}, json=payload, timeout=60) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        report = json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError, TypeError):
        return None

    ok, _ = validate_intel_report(report)
    if not ok:
        return empty_report(n_signals=n_signals)
    report.setdefault("meta", {})["source"] = "gemini"
    report["meta"]["n_signals"] = n_signals
    return report
