# Contributing Guide

## Local Development Setup

### Prerequisites
- Python 3.13+
- pip or uv
- Git

### Installation

```bash
# Clone the repository
git clone https://github.com/TrololoBird/Crypto-Analytic-Signal-Bot.git
cd Crypto-Analytic-Signal-Bot

# Install in development mode
pip install -e '.[dev,test]'

# Setup pre-commit hooks (recommended)
pre-commit install
pre-commit run --all-files  # Run on all files first time
```

## Development Workflow

### Before Committing

1. **Run linting & formatting locally**
   ```bash
   ruff check --fix bot/ tests/
   ruff format bot/ tests/
   ```

2. **Run type checking**
   ```bash
   mypy bot/ml/filter.py bot/confluence.py --follow-imports=skip
   ```

3. **Run tests**
   ```bash
   pytest tests/ -v --cov=bot
   ```

4. **Commit your changes**
   ```bash
   git add .
   git commit -m "type: description"
   git push origin your-branch
   ```

### CI/CD Pipeline

When you push to any branch, the CI pipeline automatically:

1. **Lint Job** - Runs ruff with auto-fix on bot/ and tests/
   - If you're on a non-main branch, auto-commits fixes
   - If on main, reports issues

2. **Type Check** - Runs mypy on critical files
   - Required to pass

3. **Type Check Full Report** - Extended mypy analysis
   - Informational only (can fail)

4. **Tests** - Runs pytest with coverage
   - Required to pass
   - Uploads coverage to Codecov

### Auto-Fix Workflow

When changes are pushed to `main` or `develop`:
- An automated workflow creates a PR with ruff formatting fixes
- Labeled with `automated` and `formatting`
- Requires review before merge

## Commit Message Format

Use conventional commits:

```
type(scope): description

[optional body]
[optional footer]
```

**Types:**
- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation
- `style:` Formatting (handled by ruff)
- `refactor:` Code restructuring
- `test:` Test changes
- `chore:` Build, deps, CI

**Examples:**
```
feat(bot): add new trading strategy
fix(market_data): handle binance api timeouts
docs: update README with setup instructions
test: add unit tests for filter module
chore: auto-format with ruff
```

## Code Style

- **Line length:** 100 characters
- **Python version:** 3.13+
- **Formatter:** ruff
- **Linter:** ruff
- **Type checker:** mypy

All formatting is automatic via pre-commit hooks and CI pipeline.

## Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test
pytest tests/test_bot.py::test_function -v

# Run with coverage
pytest tests/ --cov=bot --cov-report=html
```

## Troubleshooting

### "Multiple top-level packages discovered" Error

This is fixed by the updated `pyproject.toml`. If you still see it:

```bash
# Clean pip cache and reinstall
pip cache purge
pip install -e '.[dev,test]' --no-cache-dir
```

### Pre-commit hooks fail

```bash
# Skip hooks temporarily (not recommended)
git commit --no-verify

# Update and run hooks
pre-commit autoupdate
pre-commit run --all-files
```

## Questions?

Open an issue or discussion on GitHub!
