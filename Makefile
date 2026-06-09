.PHONY: check check-imports check-cycles lint typecheck smoke validate-config live-smoke research-harvest monitor-runtime run status stop clean-session graphify-install graphify-update nightly-calibration reconcile-defaults shortlist-matrix calibration-pipeline

clean-session:
	@python scripts/clean_session_data.py --mode smoke --config config.toml

check:
	@echo "=== Ruff lint ==="
	@.venv/bin/ruff check bot/ tests/ scripts/ main.py
	@echo "=== Compile check ==="
	@.venv/bin/python -m compileall -q bot
	@echo "=== v9 refactor gate ==="
	@.venv/bin/python scripts/verify_refactor_gate.py
	@$(MAKE) check-imports
	@$(MAKE) check-cycles
	@echo "=== Strategy export check ==="
	@.venv/bin/python -c "from bot.strategies import STRATEGY_CLASSES; print(f'Strategies: {len(STRATEGY_CLASSES)}')"

check-imports:
	@.venv/bin/python -c "from bot.runtime.bot import SignalBot; from bot.strategies import STRATEGY_CLASSES; print(f'Imports OK ({len(STRATEGY_CLASSES)} strategies)')"

check-cycles:
	@.venv/bin/python scripts/check_circular_imports.py

lint:
	@ruff check bot/ tests/ scripts/ --fix

typecheck:
	@.venv/bin/python scripts/run_mypy_critical.py

smoke:
	@.venv/bin/pytest -x -q --ignore=tests/live -m "not slow"

validate-config:
	@python scripts/validate_config.py --config config.toml

run:
	@python main.py run

status:
	@python main.py status

stop:
	@python main.py stop

live-smoke:
	@python scripts/clean_session_data.py --mode smoke --config config.toml
	@python scripts/live_smoke_bot.py --warmup-seconds 30 --keep-session-data

research-harvest:
	@chmod +x scripts/run_research_harvest.sh
	@HARVEST_MINUTES=60 bash scripts/run_research_harvest.sh

research-harvest-2h:
	@chmod +x scripts/run_research_harvest.sh
	@HARVEST_MINUTES=120 bash scripts/run_research_harvest.sh

bot-supervisor:
	@chmod +x scripts/bot_supervisor.sh
	@bash scripts/bot_supervisor.sh

monitor-runtime:
	@python -m scripts.live_runtime_monitor --duration 300 --poll-interval 5 --log-dir data/bot/logs

graphify-install:
	@bash scripts/setup_graphify.sh

graphify-update:
	@if command -v graphify >/dev/null 2>&1; then graphify update .; else echo "graphify not installed — run: make graphify-install"; fi

nightly-calibration:
	@chmod +x scripts/run_nightly_calibration.sh
	@bash scripts/run_nightly_calibration.sh

reconcile-defaults:
	@python scripts/reconcile_strategy_defaults.py

calibration-pipeline:
	@python scripts/calibration_pipeline.py --config config.toml

calibration-wave:
	@test -n "$(RUN_ID)" || (echo "Usage: make calibration-wave RUN_ID=20260604T155544Z" && exit 1)
	@python scripts/post_session_calibration.py --config config.toml --run-id $(RUN_ID) --rollup

live-watch-report:
	@python scripts/live_watch_rollup_report.py --config config.toml

live-detached-6h:
	@python scripts/clean_session_data.py --mode smoke --config config.toml
	@mkdir -p logs data/live_watch
	@python scripts/launch_detached.py --log logs/live_supervised_6h.log --pid-file data/live_watch/supervisor.pid --cwd . -- \
		caffeinate -i .venv/bin/python -m scripts.live_supervised_session --hours 6 --minutes 360 --snapshot-interval 60 --config config.toml --takeover
	@echo "Supervisor PID: $$(cat data/live_watch/supervisor.pid 2>/dev/null || echo unknown)"

shortlist-matrix:
	@python scripts/strategy_shortlist_matrix.py --config config.toml --static --json
