# Security Policy

## Supported versions

| Branch | Supported |
|--------|-----------|
| `main` | Yes |

## Reporting a vulnerability

This is a **solo-operated** signal bot (no user accounts, no auto-trading, public Binance data only).

If you find a security issue:

1. **Do not** open a public issue for exploitable findings.
2. Email or DM the repository owner via GitHub ([TrololoBird](https://github.com/TrololoBird)) with:
   - Description and impact
   - Steps to reproduce
   - Affected paths / commits
3. Expect an initial response within **7 days**.

Safe to report publicly (issues welcome):

- Misconfigured example `config.toml` defaults
- Documentation gaps
- Non-exploitable lint / CI failures

## Scope

In scope:

- Credential leakage (`.env`, Telegram tokens, proxy URLs with secrets)
- Bypass of delivery gates (`validate_signal_contract` → confluence → `deliver`)
- Unexpected private Binance API usage or order placement
- Dependency vulnerabilities on the **runtime hot path**

Out of scope:

- Social engineering, physical access, third-party Telegram/Binance outages
- Missing features or strategy tuning

## Known accepted risks

### aiohttp < 3.14 (Dependabot: moderate)

| Item | Detail |
|------|--------|
| Package | `aiohttp` pinned `>=3.13.5,<3.14` in `pyproject.toml` |
| Blocker | `aiogram>=3.28.2` requires `aiohttp<3.14` ([PyPI constraints](https://pypi.org/project/aiogram/)) |
| CVEs | CVE-2026-34993 (CookieJar deserialization), CVE-2026-47265 (cross-origin redirect cookies), CVE-2026-50269, CVE-2026-54273–54280 (3.13.5 advisories) |
| Mitigation | Bot does **not** load untrusted cookie jars; REST/WS targets Binance public endpoints only; no user cookie input |
| Remediation | Re-evaluate when aiogram supports `aiohttp>=3.14` — remove ignore in `.github/dependabot.yml` and bump cap |

Document updates tracked in repo; Dependabot alerts may remain open until upstream constraint is lifted.

## Automated security tooling (GitHub)

Enabled on this repository:

- Dependabot alerts + security updates
- Secret scanning + push protection
- CodeQL analysis (Python, Actions)
- CI: ruff, pytest, mypy critical, optional live Binance probes
- Dependency Review on pull requests

## Secure development

- Secrets live in `.env` / GitHub Actions secrets — never commit `config.toml`, `.env`, or `ourtg.json`
- Before live runs: `python scripts/clean_session_data.py --mode smoke --config config.toml`
- Delivery path must not be bypassed in production code (`bot/delivery/`)
