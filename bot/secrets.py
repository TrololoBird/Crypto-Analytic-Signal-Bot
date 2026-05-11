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


def _first_configured_env(*names: str) -> str:
    for name in names:
        if name in os.environ:
            return os.environ[name].strip()
    return ""


def load_secrets() -> Secrets:
    load_dotenv()
    tg_token = _first_configured_env("TG_TOKEN", "TELEGRAM_BOT_TOKEN")
    target_chat_id = _first_configured_env("TARGET_CHAT_ID", "TELEGRAM_CHAT_ID")
    return Secrets(
        tg_token=tg_token,
        target_chat_id=target_chat_id,
    )
