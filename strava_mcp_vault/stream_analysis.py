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


NP_WINDOW_SECONDS = 30  # standard Coggan 30-second rolling window
HR_ZONE_LABELS_5 = ["Recovery", "Endurance", "Tempo", "Threshold", "VO2 Max"]
POWER_ZONE_LABELS_7 = [
    "Active Recovery", "Endurance", "Tempo", "Threshold",
    "VO2 Max", "Anaerobic", "Neuromuscular",
]


def normalized_power(watts: list[float | int | None]) -> float:
    """Compute normalized power per Coggan's algorithm.

    Steps:
      1. 30-second rolling average of power.
      2. Raise each rolling-average value to the 4th power.
      3. Take the mean of those 4th powers.
      4. Take the 4th root.

    Returns 0.0 for empty input. Falls back to simple average if activity is
    shorter than the rolling window.
    """
    if not watts:
        return 0.0

    cleaned = [float(w) if w is not None else 0.0 for w in watts]
    n = len(cleaned)

    if n < NP_WINDOW_SECONDS:
        return sum(cleaned) / n

    rolling = []
    window_sum = sum(cleaned[:NP_WINDOW_SECONDS])
    rolling.append(window_sum / NP_WINDOW_SECONDS)
    for i in range(NP_WINDOW_SECONDS, n):
        window_sum += cleaned[i] - cleaned[i - NP_WINDOW_SECONDS]
        rolling.append(window_sum / NP_WINDOW_SECONDS)

    fourth_powers = [r**4 for r in rolling]
    mean_fourth = sum(fourth_powers) / len(fourth_powers)
    return mean_fourth**0.25


def _zone_labels(zones: list[dict]) -> list[str]:
    n = len(zones)
    if n == 5:
        return HR_ZONE_LABELS_5
    if n == 7:
        return POWER_ZONE_LABELS_7
    return [f"Z{i + 1}" for i in range(n)]


def _sample_deltas(streams: StreamDict, total_samples: int) -> list[float]:
    """Per-sample seconds. Uses time stream deltas if present, else 1s."""
    time_stream = streams.get("time")
    if not isinstance(time_stream, list) or len(time_stream) < 2:
        return [1.0] * total_samples
    deltas = [0.0]
    for i in range(1, len(time_stream)):
        deltas.append(float(time_stream[i] - time_stream[i - 1]))
    while len(deltas) < total_samples:
        deltas.append(1.0)
    return deltas[:total_samples]


def _bucket_into_zones(
    values: list[float | int | None],
    zones: list[dict],
    deltas: list[float],
) -> list[float]:
    """Return seconds spent in each zone, given per-sample values + deltas."""
    bucket = [0.0] * len(zones)
    for v, dt in zip(values, deltas):
        if v is None:
            continue
        for i, z in enumerate(zones):
            if v >= z["min"] and v < z["max"]:
                bucket[i] += dt
                break
        else:
            # value at or above the last zone's max — count in top zone
            bucket[-1] += dt
    return bucket


def compute_zone_distribution(
    streams: StreamDict,
    hr_zones: list[dict] | None,
    power_zones: list[dict] | None,
) -> dict[str, Any]:
    """Time spent in each HR and/or power zone.

    Returns {duration_s, hr: [...] | None, power: [...] | None}.
    """
    hr = streams.get("heartrate")
    watts = streams.get("watts")
    total = max(len(hr) if isinstance(hr, list) else 0,
                len(watts) if isinstance(watts, list) else 0)
    deltas = _sample_deltas(streams, total)
    duration_s = int(sum(deltas))

    out: dict[str, Any] = {"duration_s": duration_s, "hr": None, "power": None}

    if hr_zones and isinstance(hr, list) and hr:
        seconds = _bucket_into_zones(hr, hr_zones, deltas)
        total_hr_s = sum(seconds) or 1.0
        labels = _zone_labels(hr_zones)
        out["hr"] = [
            {
                "zone": i + 1,
                "name": labels[i],
                "min": z["min"],
                "max": z["max"],
                "time_s": int(round(seconds[i])),
                "pct": round(seconds[i] / total_hr_s * 100, 1),
            }
            for i, z in enumerate(hr_zones)
        ]

    if power_zones and isinstance(watts, list) and watts:
        seconds = _bucket_into_zones(watts, power_zones, deltas)
        total_p_s = sum(seconds) or 1.0
        labels = _zone_labels(power_zones)
        out["power"] = [
            {
                "zone": i + 1,
                "name": labels[i],
                "min": z["min"],
                "max": z["max"],
                "time_s": int(round(seconds[i])),
                "pct": round(seconds[i] / total_p_s * 100, 1),
            }
            for i, z in enumerate(power_zones)
        ]

    return out


def _rolling_mean_max(values: list[float], window: int) -> float | None:
    """Best mean of `window` consecutive samples. None if window > len."""
    n = len(values)
    if window > n or window <= 0:
        return None
    window_sum = sum(values[:window])
    best = window_sum
    for i in range(window, n):
        window_sum += values[i] - values[i - window]
        if window_sum > best:
            best = window_sum
    return best / window


def compute_power_curve(
    streams: StreamDict,
    durations: list[int],
) -> dict[str, Any]:
    """Best mean-max power at each requested duration (seconds)."""
    watts = streams.get("watts")
    if not isinstance(watts, list) or not watts or all(w in (None, 0) for w in watts):
        return {"error": "no_power_data"}

    cleaned = [float(w) if w is not None else 0.0 for w in watts]
    total = len(cleaned)
    avg = sum(cleaned) / total
    np = normalized_power(cleaned)

    points = []
    omitted = []
    for d in durations:
        best = _rolling_mean_max(cleaned, d)
        if best is None:
            omitted.append({"duration_s": d, "reason": "longer than activity"})
        else:
            points.append({"duration_s": d, "best_watts": round(best, 1)})

    return {
        "duration_s": total,
        "avg_power": round(avg, 1),
        "normalized_power": round(np, 1),
        "points": points,
        "omitted": omitted,
    }
