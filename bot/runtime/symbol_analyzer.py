"""Symbol analysis orchestration (v9 slim entry)."""

from __future__ import annotations

from bot.runtime.analyzer.pipeline import AnalyzerPipelineMixin


class SymbolAnalyzer(AnalyzerPipelineMixin):
    """Per-symbol pipeline: frames → prepare → engine → filters."""
