"""Validate feature lake schema alignment with registry and FeatureVector."""
from __future__ import annotations

from dataclasses import fields

from hunt_core.features.feature_engine import FeatureVector, load_feature_registry


def check_lake_schema() -> list[str]:
    issues: list[str] = []
    registry = load_feature_registry()
    reg_feats = registry.get("features") or {}
    if not isinstance(reg_feats, dict):
        issues.append("feature_registry: missing features dict")
        return issues

    fv_names = {f.name for f in fields(FeatureVector) if f.name not in {"symbol", "ts", "tf"}}
    reg_names = set(reg_feats.keys())

    missing_in_fv = sorted(reg_names - fv_names)
    missing_in_reg = sorted(fv_names - reg_names)
    if missing_in_fv:
        issues.append(f"registry fields not in FeatureVector: {missing_in_fv}")
    if missing_in_reg:
        issues.append(f"FeatureVector fields not in registry: {missing_in_reg}")

    required = [k for k, v in reg_feats.items() if isinstance(v, dict) and v.get("required")]
    gap_close = {"delta_ratio", "zscore30", "session_cvd", "rolling_cvd_24h"}
    tier1 = {
        "oi_acceleration",
        "funding_velocity",
        "poc_migration_1h",
        "poc_migration_4h",
        "va_contraction",
        "liquidity_void_path",
    }
    for name in sorted(gap_close | tier1):
        if name not in reg_names:
            issues.append(f"Phase 0.1 column missing from registry: {name}")
        elif name not in fv_names:
            issues.append(f"Phase 0.1 column missing from FeatureVector: {name}")

    schema_ver = registry.get("schema_version")
    if str(schema_ver) != "2":
        issues.append(f"expected schema_version=2, got {schema_ver!r}")

    if not required:
        issues.append("no required fields in registry")

    return issues


def main() -> int:
    issues = check_lake_schema()
    if issues:
        print("check_lake_schema FAIL")
        for line in issues:
            print(f"  {line}")
        return 1
    n = len((load_feature_registry().get("features") or {}))
    print(f"check_lake_schema ok | registry_cols={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
