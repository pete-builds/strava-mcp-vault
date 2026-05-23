"""Pure compute over Strava activity streams.

All functions take normalized streams (dict of {stream_type: list}) and
return computed results. No I/O, no caching, no side effects.
"""

from __future__ import annotations

import math
from typing import Any

StreamDict = dict[str, list[Any] | None]


def downsample(
    streams: StreamDict,
    max_points: int | None,
) -> tuple[StreamDict, dict[str, Any]]:
    """Evenly-spaced downsample of every list-valued stream.

    Uses a single shared step across all streams so that index i refers to
    the same moment across heartrate/watts/time/etc. Non-list values are
    passed through unchanged.

    Returns (downsampled_streams, downsample_meta).
    """
    list_lengths = [len(v) for v in streams.values() if isinstance(v, list)]
    original_points = max(list_lengths) if list_lengths else 0

    if max_points is None or original_points == 0 or original_points <= max_points:
        return streams, {
            "original_points": original_points,
            "returned_points": original_points,
            "step": 1,
            "reason": "none",
        }

    step = math.ceil(original_points / max_points)
    result: StreamDict = {}
    for key, value in streams.items():
        if isinstance(value, list):
            result[key] = value[::step]
        else:
            result[key] = value

    returned = math.ceil(original_points / step)
    return result, {
        "original_points": original_points,
        "returned_points": returned,
        "step": step,
        "reason": "user_requested",
    }


BYTES_PER_NUMBER = 10  # rough average for JSON-encoded floats/ints
FRAMING_OVERHEAD = 1.2  # 20% for keys, brackets, MCP wrapping
MIN_RECOMMENDED_POINTS = 100  # below this, the data is too lossy to be useful


def estimate_response_bytes(streams: StreamDict) -> int:
    """Estimate JSON-serialized byte size for a stream dict.

    Used by the pre-flight size guard to decide whether to error or proceed.
    Rough — based on number count × per-number byte estimate × framing factor.
    """
    total_numbers = sum(len(v) for v in streams.values() if isinstance(v, list))
    return int(total_numbers * BYTES_PER_NUMBER * FRAMING_OVERHEAD)


def recommended_max_points(
    streams: StreamDict,
    target_bytes: int = 800_000,
) -> int:
    """Compute a max_points value that would keep response under target_bytes.

    Floors at MIN_RECOMMENDED_POINTS so callers always get a usable number.
    """
    num_streams = sum(1 for v in streams.values() if isinstance(v, list))
    if num_streams == 0:
        return 0
    raw = int(target_bytes / (num_streams * BYTES_PER_NUMBER * FRAMING_OVERHEAD))
    return max(raw, MIN_RECOMMENDED_POINTS)
