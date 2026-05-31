"""vwap_trend alias."""
from .vwap import detect_vwap_reclaim

detect_vwap_trend = detect_vwap_reclaim

__all__ = ["detect_vwap_trend", "detect_vwap_reclaim"]
