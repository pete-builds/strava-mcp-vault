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
