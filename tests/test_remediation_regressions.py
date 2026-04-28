"""Backward-compatible module path for remediation regression helpers/cases.

This file intentionally stays non-collectable by pytest to avoid duplicate test runs.
"""

__test__ = False

from tests import remediation_regression_cases as _cases

globals().update(
    {
        name: value
        for name, value in _cases.__dict__.items()
        if not (name.startswith("__") and name.endswith("__"))
    }
)
