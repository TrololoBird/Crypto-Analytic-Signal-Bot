"""Symbol analysis orchestration (v9 slim entry)."""

from __future__ import annotations

from bot.runtime.analyzer.context import AnalyzerContextMixin
from bot.runtime.analyzer.frames import AnalyzerFramesMixin
from bot.runtime.analyzer.pipeline import AnalyzerPipelineMixin


class SymbolAnalyzer(AnalyzerContextMixin, AnalyzerFramesMixin, AnalyzerPipelineMixin):
    """Per-symbol pipeline: frames → prepare → engine → filters."""
