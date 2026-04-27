param(
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$tests = @(
    "tests/test_event_bus.py",
    "tests/test_runtime_config_and_notifiers.py",
    "tests/test_smc_helpers.py",
    "tests/test_telegram_queue.py",
    "tests/test_backtest_engine.py",
    "tests/test_backtest_metrics.py",
    "tests/test_training_pipeline.py",
    "tests/test_signal_classifier.py",
    "tests/test_ml_guardrails.py",
    "tests/test_application_delegation.py",
    "tests/test_remediation_indicators.py",
    "tests/test_remediation_intra_candle.py",
    "tests/test_remediation_regressions.py",
    "tests/test_strategies.py",
    "tests/test_filters.py",
    "tests/test_confluence_engine.py",
    "tests/test_market_context_updater.py",
    "tests/test_composite_regime.py"
)

$pytestArgs = @("-m", "pytest", "-q") + $tests
Write-Host "Running targeted remediation validation..."
& $PythonExe $pytestArgs
