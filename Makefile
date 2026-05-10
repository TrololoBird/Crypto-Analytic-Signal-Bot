.PHONY: check lint test test-smoke test-regression dry-run run

check:
	@echo "=== Compile check ==="
	@python -m compileall -q bot
	@echo "=== Import check ==="
	@python -c "from bot.application.bot import SignalBot; print('Imports OK')"
	@echo "=== Strategy export check ==="
	@python -c "from bot.strategies import STRATEGY_CLASSES; print(f'Strategies: {len(STRATEGY_CLASSES)}')"

lint:
	@ruff check bot/ tests/
	@mypy bot/ --ignore-missing-imports

test:
	@pytest -q tests/

test-smoke:
	@pytest -q tests/test_sanity.py tests/test_event_bus.py tests/test_config_intelligence.py tests/test_filters.py --maxfail=1

test-regression:
	@pytest tests/ -v --cov=bot --cov-report=xml --cov-fail-under=49

dry-run:
	@python main.py --mode dry-run --config config.toml

run:
	@python main.py --config config.toml
