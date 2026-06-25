"""Syrupy snapshot extension with scrubber for stable Telegram output tests."""

from __future__ import annotations

import pytest
from syrupy.extensions.amber import AmberSnapshotExtension

from tests.fixtures.scrubber import scrub


class ScrubTelegramSnapshotExtension(AmberSnapshotExtension):
    """Amber extension that scrubs timestamps and refs before comparison."""

    def serialize(self, data: object, **kwargs: object) -> str:
        if isinstance(data, str):
            data = scrub(data)
        return super().serialize(data, **kwargs)


@pytest.fixture
def snapshot_scrubbed(snapshot):
    """Snapshot fixture with Telegram-aware scrubbing."""
    return snapshot.use_extension(ScrubTelegramSnapshotExtension)
