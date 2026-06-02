## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, invoke the `skill` tool with `skill: "graphify"` before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## Cursor project config

- Rules: `.cursor/rules/*.mdc` (guardrails, strategies, features, delivery)
- Skills: `.cursor/skills/` (`live-binance-verify`, `refactor-module`, `zero-hit-strategy-triage`, `validate-delivery-path`)
- Refactor plan: `docs/REFACTOR_PLAN.md`
- Canonical architecture: `docs/research/ARCHITECTURE_CANONICAL.md` (~180 `.py` in `bot/`, single `strategies/` tree)
- Target research spec: `docs/research/README.md` (38 strategies, architecture, Binance public matrix, Telegram spec)
- Structural gate: `python scripts/verify_refactor_gate.py`
- Live tests only: `tests/live/` with `PYTEST_LIVE=1`
- Local venv (**Python 3.14.5**): `py -3.14 -m venv .venv` then `.\.venv\Scripts\pip install -e ".[live,dev,test]"` — use `.venv` only (do not run `graphify`/pytest with system Python 3.13).

## Cursor Cloud specific instructions

### Python and dependencies

The repo requires **Python 3.14** (`requires-python = ">=3.14,<3.15"`). Cloud VMs often ship 3.12 only — install 3.14 with [uv](https://docs.astral.sh/uv/):

```bash
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.14
uv venv .venv --python 3.14
source .venv/bin/activate
uv pip install -e ".[live,dev,test]"
```

### Config (first run)

`config.toml` is gitignored. Copy once per workspace:

```bash
cp config.toml.example config.toml
python scripts/validate_config.py --config config.toml
python scripts/project_health_audit.py --stale-days 2 --full
```

For smoke runs without Telegram: `provider = "none"` in config (see `config.toml.example`) or `BOT_NOTIFIER_PROVIDER=none`.

### Services (single process)

| Port | Service |
|------|---------|
| — | `python main.py run` — main bot (WS + REST + strategies + SQLite) |
| 8080 | Embedded FastAPI dashboard (`/api/health`, `/api/status`, …) |
| 9090 | Prometheus metrics (`/metrics`) |

Disable dashboard/metrics for scripts: `BOT_DISABLE_HTTP_SERVERS=1`.

Standard commands: `Makefile` (`make check`, `make run`, `make live-smoke`), `AGENT_QUICK_START.md`.

### Binance geo-restriction (important)

Many cloud/datacenter IPs are **blocked by Binance public REST** (`Service unavailable from a restricted location`). In that case:

- WebSocket to `fstream.binance.com` may still connect.
- REST (`exchangeInfo`, klines, tickers) fails; live pytest and `live_check_*` scripts that fetch REST will fail.
- `python main.py run` still starts (doctor OK, 38 strategies, dashboard `/api/health` can show `ws_connected: true`) but enrichment uses **pinned_fallback** and skips kline preload.

Full live verification requires a network path Binance allows (local machine, allowed region, or proxy). Configure `[bot.network]` / `BINANCE_PROXY_URL` — see [docs/BINANCE_PROXY_RU.md](docs/BINANCE_PROXY_RU.md). This is an external constraint, not a broken venv.

Hot path uses **Polars only**; `pandas` is optional in `[ml]` extra (offline experiments), not installed by default.

### Verify in Cloud

```bash
source .venv/bin/activate
make check
python scripts/validate_config.py --config config.toml
PYTEST_LIVE=1 pytest tests/live/test_strategy_catalog_wiring.py -v   # no REST
# When Binance REST is reachable:
PYTEST_LIVE=1 pytest tests/live/ -v
python scripts/live_check_pipeline.py --symbols BTCUSDT --limit 1
```
