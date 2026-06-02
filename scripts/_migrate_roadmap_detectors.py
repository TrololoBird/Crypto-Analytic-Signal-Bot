"""Migrate roadmap strategy detect() bodies into bot/setups/detectors/."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRAT = ROOT / "bot" / "strategies"
DET = ROOT / "bot" / "setups" / "detectors"

ROADMAP_MAP = {
    "whale_walls.py": ("whale_walls", "WhaleWallsSetup"),
    "spread_strategy.py": ("spread_strategy", "SpreadStrategySetup"),
    "depth_imbalance.py": ("depth_imbalance", "DepthImbalanceSetup"),
    "liquidation_heatmap.py": ("liquidation_heatmap", "LiquidationHeatmapSetup"),
    "multi_tf_trend.py": ("multi_tf_trend", "MultiTFTrendSetup"),
    "oi_divergence.py": ("oi_divergence", "OIDivergenceSetup"),
    "ls_ratio_extreme.py": ("ls_ratio_extreme", "LSRatioExtremeSetup"),
    "btc_correlation.py": ("btc_correlation", "BTCCorrelationSetup"),
    "altcoin_season_index.py": ("altcoin_season_index", "AltcoinSeasonIndexSetup"),
}


def _extract_detect_method(source: str, class_name: str) -> str:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "detect":
                    return ast.get_source_segment(source, item) or ""
    raise ValueError(f"detect not found in {class_name}")


def _body_to_function(detect_src: str, func_name: str) -> str:
    lines = detect_src.splitlines()
    # drop def detect line and dedent one level
    body_lines = lines[1:]
    dedented = []
    for line in body_lines:
        if line.startswith("        "):
            dedented.append(line[4:])
        elif line.strip() == "":
            dedented.append("")
        else:
            dedented.append(line)
    body = "\n".join(dedented)
    body = body.replace("self.setup_id", "setup_id")
    body = body.replace("self.family", "family")
    body = body.replace("params = self._params(prepared, settings)", "params = effective_params")
    body = body.replace("self._params(prepared, settings)", "effective_params")
    return (
        f"def {func_name}(\n"
        "    prepared: PreparedSymbol,\n"
        "    settings: BotSettings,\n"
        "    effective_params: dict[str, float],\n"
        "    *,\n"
        "    setup_id: str,\n"
        "    family: str,\n"
        ") -> Signal | None:\n" + body
    )


def main() -> None:
    # _roadmap.py from roadmap_base without RoadmapSetup class
    rb = (STRAT / "roadmap_base.py").read_text(encoding="utf-8")
    cut = rb.index("class RoadmapSetup")
    roadmap_helpers = rb[:cut]
    roadmap_helpers = roadmap_helpers.replace(
        '"""Roadmap strategy detectors',
        '"""Roadmap detector helpers',
    )
    roadmap_helpers = roadmap_helpers.replace(
        "from .common import", "from ...strategies.common import"
    )
    roadmap_helpers = roadmap_helpers.replace("from ..setups.base import BaseSetup\n", "")
    (DET / "_roadmap.py").write_text(roadmap_helpers, encoding="utf-8")

    imports = """\"\"\"{title}\"\"\"
from __future__ import annotations

from ...domain.config import BotSettings
from ...domain.schemas import PreparedSymbol, Signal
from ._roadmap import (
    _as_float,
    _build_atr_signal,
    _confirmed_context_conflict,
    _finite_or_none,
    _first_finite,
    _has_l2_depth,
    _last,
    _orderbook_source,
    _price_change_pct,
    _reject,
)

"""

    for strat_file, (mod, cls) in ROADMAP_MAP.items():
        src = (STRAT / strat_file).read_text(encoding="utf-8")
        detect_src = _extract_detect_method(src, cls)
        func = _body_to_function(detect_src, f"detect_{mod}")
        # pick only imports used - use full roadmap import block subset from original imports
        used = set(re.findall(r"_\w+", func))
        imp_lines = []
        for name in sorted(used):
            if name.startswith("_") and name in {
                "_build_atr_signal",
                "_confirmed_context_conflict",
                "_finite_or_none",
                "_first_finite",
                "_has_l2_depth",
                "_last",
                "_orderbook_source",
                "_price_change_pct",
                "_reject",
                "_as_float",
            }:
                pass
        header = imports.format(title=mod.replace("_", " "))
        # trim imports based on strat file imports from roadmap_base
        imp_block = re.search(
            r"from \.roadmap_base import \((.*?)\)",
            src,
            re.S,
        )
        names = []
        if imp_block:
            names = [n.strip() for n in imp_block.group(1).split(",") if n.strip()]
        imp_names = ",\n    ".join(names)
        header = (
            f'"""{mod} detector."""\nfrom __future__ import annotations\n\n'
            f"from ...domain.config import BotSettings\n"
            f"from ...domain.schemas import PreparedSymbol, Signal\n"
            f"from ._roadmap import (\n    {imp_names},\n)\n\n"
        )
        out = header + f'__all__ = ["detect_{mod}"]\n\n\n' + func + "\n"
        (DET / f"{mod}.py").write_text(out, encoding="utf-8")
        thin = f'''from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.{mod} import detect_{mod}


class {cls}(RoadmapSetup):
    setup_id = "{mod}"
    family = {cls}.family if hasattr({cls}, "family") else RoadmapSetup.family
    confirmation_profile = getattr({cls}, "confirmation_profile", RoadmapSetup.confirmation_profile)
    required_context = getattr({cls}, "required_context", RoadmapSetup.required_context)

'''
        # preserve class attrs from original
        class_match = re.search(
            rf"class {cls}\(RoadmapSetup\):(.*?)(?=\n    def detect)",
            src,
            re.S,
        )
        if class_match:
            attrs = class_match.group(1)
            thin = (
                "from __future__ import annotations\n\n"
                "from ..domain.config import BotSettings\n"
                "from ..domain.schemas import PreparedSymbol, Signal\n"
                "from .roadmap_base import RoadmapSetup\n"
                f"from ..setups.detectors.{mod} import detect_{mod}\n\n\n"
                f"class {cls}(RoadmapSetup):"
                + attrs
                + f"""
    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_{mod}(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["{cls}"]
"""
            )
        (STRAT / strat_file).write_text(thin, encoding="utf-8")
        print("migrated", mod)

    # roadmap_base re-export
    rb_new = '''"""Roadmap strategy base — params wiring; detect logic in setups/detectors/."""
from __future__ import annotations

from typing import ClassVar

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol
from ..setups.base import BaseSetup
from ..setups.utils import get_dynamic_params
from ._roadmap import _configured_params

from ..setups.detectors._roadmap import *  # noqa: F403


class RoadmapSetup(BaseSetup):
    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.52,
        "sl_buffer_atr": 0.65,
        "min_rr": 1.9,
    }

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        return _configured_params(settings, self.setup_id, self.DEFAULTS)

    def _params(self, prepared: PreparedSymbol, settings: BotSettings) -> dict[str, float]:
        return {
            **self.get_optimizable_params(settings),
            **get_dynamic_params(prepared, self.setup_id),
        }


__all__ = ["RoadmapSetup"]
'''
    # Fix roadmap_base path - it's in strategies not detectors
    rb_new = rb_new.replace("from ._roadmap", "from ..setups.detectors._roadmap")
    rb_new = rb_new.replace("from ..setups.detectors._roadmap import *", "")
    rb_new = '''"""Roadmap strategy base — params wiring; detect logic in setups/detectors/."""
from __future__ import annotations

from typing import ClassVar

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol
from ..setups.base import BaseSetup
from ..setups.utils import get_dynamic_params
from ..setups.detectors._roadmap import (
    _as_float,
    _build_atr_signal,
    _configured_params,
    _confirmed_context_conflict,
    _finite_or_none,
    _first_finite,
    _flow_delta,
    _flow_delta_with_source,
    _has_l2_depth,
    _last,
    _missing_columns,
    _orderbook_source,
    _prev,
    _price_change_pct,
    _reject,
    _series_max_tail,
    _series_mean_tail,
    _series_min_tail,
)


class RoadmapSetup(BaseSetup):
    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.52,
        "sl_buffer_atr": 0.65,
        "min_rr": 1.9,
    }

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        return _configured_params(settings, self.setup_id, self.DEFAULTS)

    def _params(self, prepared: PreparedSymbol, settings: BotSettings) -> dict[str, float]:
        return {
            **self.get_optimizable_params(settings),
            **get_dynamic_params(prepared, self.setup_id),
        }


__all__ = [
    "RoadmapSetup",
    "_as_float",
    "_build_atr_signal",
    "_configured_params",
    "_confirmed_context_conflict",
    "_finite_or_none",
    "_first_finite",
    "_flow_delta",
    "_flow_delta_with_source",
    "_has_l2_depth",
    "_last",
    "_missing_columns",
    "_orderbook_source",
    "_prev",
    "_price_change_pct",
    "_reject",
    "_series_max_tail",
    "_series_mean_tail",
    "_series_min_tail",
]
'''
    (STRAT / "roadmap_base.py").write_text(rb_new, encoding="utf-8")
    print("updated roadmap_base.py")


if __name__ == "__main__":
    main()
