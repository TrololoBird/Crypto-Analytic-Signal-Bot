"""Environment-backed runtime secrets.

Centralizes secret loading so config/model code doesn't duplicate env parsing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Secrets:
    tg_token: str
    target_chat_id: str
    operator_user_ids: tuple[int, ...] = ()


def parse_operator_user_ids(raw: str) -> tuple[int, ...]:
    """Parse comma/space-separated Telegram user ids for operator console."""
    ids: list[int] = []
    for chunk in str(raw or "").replace(";", ",").split(","):
        piece = chunk.strip()
        if not piece:
            continue
        try:
            ids.append(int(piece))
        except ValueError:
            continue
    return tuple(sorted(set(ids)))


def _first_configured_env(*names: str) -> str:
    for name in names:
        if name in os.environ:
            return os.environ[name].strip()
    return ""


def load_secrets() -> Secrets:
    # Canonical env keys: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    # Legacy aliases TG_TOKEN / TARGET_CHAT_ID are deprecated — do not introduce new references.
    load_dotenv()
    tg_token = _first_configured_env("TELEGRAM_BOT_TOKEN", "TG_TOKEN")
    target_chat_id = _first_configured_env("TELEGRAM_CHAT_ID", "TARGET_CHAT_ID")
    operator_raw = _first_configured_env("TELEGRAM_OPERATOR_USER_IDS", "OPERATOR_USER_IDS")
    return Secrets(
        tg_token=tg_token,
        target_chat_id=target_chat_id,
        operator_user_ids=parse_operator_user_ids(operator_raw),
    )
