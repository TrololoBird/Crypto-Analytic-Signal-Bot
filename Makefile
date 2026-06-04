.PHONY: check lint validate-config live-smoke monitor-runtime run status stop clean-session graphify-update nightly-calibration reconcile-defaults shortlist-matrix calibration-pipeline

clean-session:
	@python scripts/clean_session_data.py --mode smoke --config config.toml

check:
	@echo "=== Compile check ==="
	@python -m compileall -q bot
	@echo "=== v9 refactor gate ==="
	@python scripts/verify_refactor_gate.py
	@echo "=== Import check ==="
	@python -c "from bot.runtime.bot import SignalBot; print('Imports OK')"
	@echo "=== Strategy export check ==="
	@python -c "from bot.strategies import STRATEGY_CLASSES; print(f'Strategies: {len(STRATEGY_CLASSES)}')"

lint:
	@ruff check bot/
	@mypy

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

monitor-runtime:
	@python -m scripts.live_runtime_monitor --duration 300 --poll-interval 5 --log-dir data/bot/logs

graphify-update:
	@if command -v graphify >/dev/null 2>&1; then graphify update .; else echo "graphify not installed — skipping"; fi

nightly-calibration:
	@python scripts/nightly_strategy_calibration.py --config config.toml

reconcile-defaults:
	@python scripts/reconcile_strategy_defaults.py

calibration-pipeline:
	@python scripts/calibration_pipeline.py --config config.toml

live-watch-report:
	@python scripts/live_watch_rollup_report.py --config config.toml

shortlist-matrix:
	@python scripts/strategy_shortlist_matrix.py --config config.toml --static --json
