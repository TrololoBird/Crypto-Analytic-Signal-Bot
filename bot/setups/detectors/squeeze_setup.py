"""squeeze_setup — re-exports bb squeeze release detector."""
from .bb_squeeze import detect_bb_squeeze_release

detect_squeeze_setup = detect_bb_squeeze_release

__all__ = ["detect_squeeze_setup", "detect_bb_squeeze_release"]
