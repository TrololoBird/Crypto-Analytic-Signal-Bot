.PHONY: check lint validate-config live-smoke monitor-runtime run status stop

check:
	@echo "=== Compile check ==="
	@python -m compileall -q bot
	@echo "=== Import check ==="
	@python -c "from bot.application.bot import SignalBot; print('Imports OK')"
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
	@python scripts/live_smoke_bot.py --warmup-seconds 30

monitor-runtime:
	@python -m scripts.live_runtime_monitor --duration 300 --poll-interval 5 --log-dir data/bot/logs
