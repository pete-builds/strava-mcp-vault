"""Tests for stream_analysis.py — pure compute, no I/O."""

from strava_mcp_vault.stream_analysis import (
    compute_cardiac_drift,
    compute_decoupling,
    compute_power_curve,
    compute_zone_distribution,
    downsample,
    estimate_response_bytes,
    normalized_power,
    recommended_max_points,
)

MIN_DRIFT_DURATION_S = 1200  # 20 minutes


def test_cardiac_drift_rising_hr():
    hr = [130] * 600 + [150] * 600  # 20 min, drift +20bpm
    watts = [200] * 1200
    result = compute_cardiac_drift({"heartrate": hr, "watts": watts})
    assert result["first_half"]["avg_hr"] == 130.0
    assert result["second_half"]["avg_hr"] == 150.0
    # hr_drift_pct = (150-130)/130 * 100 ≈ 15.38
    assert abs(result["hr_drift_pct"] - 15.38) < 0.1


def test_cardiac_drift_too_short_returns_error():
    hr = [140] * 600  # 10 minutes
    result = compute_cardiac_drift({"heartrate": hr})
    assert result == {"error": "activity_too_short", "minimum_s": MIN_DRIFT_DURATION_S}


def test_cardiac_drift_no_power_returns_partial():
    """No watts stream → power-derived fields null + reason."""
    hr = [130] * 600 + [150] * 600
    result = compute_cardiac_drift({"heartrate": hr})
    assert result["hr_drift_pct"] > 0
    assert result["first_half"]["avg_power"] is None
    assert result["second_half"]["avg_power"] is None
    assert result["decoupling_pct"] is None


def test_cardiac_drift_no_hr_returns_error():
    result = compute_cardiac_drift({"watts": [200] * 1200})
    assert result == {"error": "missing_required_stream", "required": "heartrate"}


def test_downsample_no_cap_returns_full_data():
    streams = {"heartrate": [1, 2, 3, 4, 5], "watts": [10, 20, 30, 40, 50]}
    result, meta = downsample(streams, max_points=None)
    assert result == streams
    assert meta == {"original_points": 5, "returned_points": 5, "step": 1, "reason": "none"}


def test_downsample_cap_larger_than_data_returns_full():
    streams = {"heartrate": [1, 2, 3]}
    result, meta = downsample(streams, max_points=100)
    assert result == {"heartrate": [1, 2, 3]}
    assert meta == {"original_points": 3, "returned_points": 3, "step": 1, "reason": "none"}


def test_downsample_even_spacing():
    streams = {"heartrate": list(range(100))}
    result, meta = downsample(streams, max_points=10)
    # step = ceil(100/10) = 10, take indices 0, 10, 20, ..., 90
    assert result["heartrate"] == [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]
    assert meta == {"original_points": 100, "returned_points": 10, "step": 10, "reason": "user_requested"}


def test_downsample_uniform_step_across_streams():
    """Cross-stream alignment: HR[i] and watts[i] must be the same moment."""
    streams = {
        "heartrate": list(range(0, 100)),
        "watts": list(range(100, 200)),
        "time": list(range(0, 100)),
    }
    result, meta = downsample(streams, max_points=10)
    assert result["heartrate"] == [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]
    assert result["watts"] == [100, 110, 120, 130, 140, 150, 160, 170, 180, 190]
    assert result["time"] == [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]
    assert meta["step"] == 10


def test_downsample_empty_streams():
    result, meta = downsample({}, max_points=10)
    assert result == {}
    assert meta == {"original_points": 0, "returned_points": 0, "step": 1, "reason": "none"}


def test_downsample_single_point():
    streams = {"heartrate": [42]}
    result, meta = downsample(streams, max_points=10)
    assert result == {"heartrate": [42]}
    assert meta == {"original_points": 1, "returned_points": 1, "step": 1, "reason": "none"}


def test_downsample_non_evenly_divisible():
    streams = {"heartrate": list(range(97))}
    result, meta = downsample(streams, max_points=10)
    # step = ceil(97/10) = 10, returns ceil(97/10) = 10 points
    assert len(result["heartrate"]) == 10
    assert result["heartrate"][0] == 0
    assert meta["step"] == 10


def test_downsample_skips_non_list_values():
    """If a stream is None or non-list, leave it alone."""
    streams = {"heartrate": [1, 2, 3, 4, 5], "missing": None}
    result, meta = downsample(streams, max_points=2)
    assert result["heartrate"] == [1, 4]  # step=ceil(5/2)=3, indices 0,3 → values 1,4
    assert result["missing"] is None


def test_estimate_response_bytes_empty():
    assert estimate_response_bytes({}) == 0


def test_estimate_response_bytes_single_stream():
    # Rule of thumb: 10 bytes/number * count, plus 20% framing overhead
    streams = {"heartrate": list(range(1000))}
    est = estimate_response_bytes(streams)
    # 1000 * 10 * 1.2 = 12_000
    assert est == 12_000


def test_estimate_response_bytes_multi_stream():
    streams = {"heartrate": list(range(1000)), "watts": list(range(1000))}
    # 2 streams * 1000 * 10 * 1.2 = 24_000
    assert estimate_response_bytes(streams) == 24_000


def test_estimate_response_bytes_ignores_non_list():
    streams = {"heartrate": list(range(100)), "missing": None}
    assert estimate_response_bytes(streams) == int(100 * 10 * 1.2)


def test_recommended_max_points_basic():
    # target_bytes / (num_streams * 10 * 1.2)
    # 800_000 / (3 * 12) = 22_222
    streams = {"heartrate": [0] * 10_000, "watts": [0] * 10_000, "time": [0] * 10_000}
    rec = recommended_max_points(streams, target_bytes=800_000)
    assert rec == 22_222


def test_recommended_max_points_floors_at_minimum():
    """Never recommend less than 100 (too lossy to be useful)."""
    streams = {f"s{i}": [0] * 100_000 for i in range(50)}  # 50 streams
    rec = recommended_max_points(streams, target_bytes=800_000)
    assert rec >= 100


def test_recommended_max_points_empty_streams():
    assert recommended_max_points({}, target_bytes=800_000) == 0


def test_normalized_power_constant_equals_avg():
    """For a constant-power effort, NP == avg power."""
    watts = [200] * 600  # 10 minutes at 200W
    np = normalized_power(watts)
    assert abs(np - 200.0) < 0.01


def test_normalized_power_shorter_than_window_returns_avg():
    """Activities shorter than the 30s rolling window fall back to avg."""
    watts = [100, 200, 300]
    np = normalized_power(watts)
    assert abs(np - 200.0) < 0.01


def test_normalized_power_variable_higher_than_avg():
    """Variable power produces NP > avg (NP penalizes spikes)."""
    # 30s at 100W, then 30s at 300W, repeated 10 times
    pattern = [100] * 30 + [300] * 30
    watts = pattern * 10
    avg = sum(watts) / len(watts)  # 200
    np = normalized_power(watts)
    assert np > avg
    assert np < 300  # but less than the peak


def test_normalized_power_empty_returns_zero():
    assert normalized_power([]) == 0.0


def test_normalized_power_skips_none_values():
    """None values in watts (missing samples) are treated as zero."""
    watts = [200, None, 200, None, 200] * 100
    np = normalized_power(watts)
    # Average of present values = 200, of all (treating None as 0) = 120
    # NP is computed over all samples with None=0
    assert np > 0


# ---------------------------------------------------------------------------
# compute_zone_distribution
# ---------------------------------------------------------------------------

HR_ZONES_5 = [
    {"min": 0, "max": 115},
    {"min": 115, "max": 132},
    {"min": 132, "max": 152},
    {"min": 152, "max": 171},
    {"min": 171, "max": 220},
]


def test_zone_distribution_all_in_one_zone():
    """100 samples of HR=140 → 100% in zone 3 (Tempo)."""
    streams = {"heartrate": [140] * 100}
    result = compute_zone_distribution(streams, hr_zones=HR_ZONES_5, power_zones=None)
    assert result["hr"][2]["time_s"] == 100
    assert result["hr"][2]["pct"] == 100.0
    assert result["hr"][0]["time_s"] == 0
    assert sum(z["time_s"] for z in result["hr"]) == 100


def test_zone_distribution_uses_time_stream_for_deltas():
    """If time stream present, use actual seconds between samples."""
    # 5 samples over 10s, all HR=140 → zone 3
    streams = {"heartrate": [140] * 5, "time": [0, 2, 5, 7, 10]}
    result = compute_zone_distribution(streams, hr_zones=HR_ZONES_5, power_zones=None)
    assert result["hr"][2]["time_s"] == 10
    assert result["duration_s"] == 10


def test_zone_distribution_zone_labels_5_zone_hr():
    streams = {"heartrate": [100] * 50}
    result = compute_zone_distribution(streams, hr_zones=HR_ZONES_5, power_zones=None)
    assert result["hr"][0]["name"] == "Recovery"
    assert result["hr"][1]["name"] == "Endurance"
    assert result["hr"][2]["name"] == "Tempo"
    assert result["hr"][3]["name"] == "Threshold"
    assert result["hr"][4]["name"] == "VO2 Max"


def test_zone_distribution_falls_back_to_numeric_labels():
    """Non-5/7-zone counts get Z1, Z2, ..."""
    three_zones = [{"min": 0, "max": 100}, {"min": 100, "max": 150}, {"min": 150, "max": 220}]
    streams = {"heartrate": [120] * 10}
    result = compute_zone_distribution(streams, hr_zones=three_zones, power_zones=None)
    names = [z["name"] for z in result["hr"]]
    assert names == ["Z1", "Z2", "Z3"]


def test_zone_distribution_no_hr_zones_returns_null():
    streams = {"heartrate": [140] * 100}
    result = compute_zone_distribution(streams, hr_zones=None, power_zones=None)
    assert result["hr"] is None


def test_zone_distribution_no_hr_stream_returns_null():
    streams = {"watts": [200] * 100}
    result = compute_zone_distribution(streams, hr_zones=HR_ZONES_5, power_zones=None)
    assert result["hr"] is None


def test_zone_distribution_power_zones_coggan_labels():
    coggan = [
        {"min": 0, "max": 100}, {"min": 100, "max": 150}, {"min": 150, "max": 200},
        {"min": 200, "max": 250}, {"min": 250, "max": 300}, {"min": 300, "max": 400},
        {"min": 400, "max": 9999},
    ]
    streams = {"watts": [180] * 100}
    result = compute_zone_distribution(streams, hr_zones=None, power_zones=coggan)
    names = [z["name"] for z in result["power"]]
    assert names == ["Active Recovery", "Endurance", "Tempo", "Threshold",
                     "VO2 Max", "Anaerobic", "Neuromuscular"]
    assert result["power"][2]["time_s"] == 100  # 180 falls in Tempo (150-200)


# ---------------------------------------------------------------------------
# compute_power_curve
# ---------------------------------------------------------------------------


def test_power_curve_ascending_best_at_end():
    """Ascending power: best 5s is the last 5 samples."""
    watts = list(range(100))  # 0..99
    result = compute_power_curve({"watts": watts}, durations=[5, 10])
    # best 5s rolling avg in 0..99 = mean of last 5 = (95+96+97+98+99)/5 = 97
    p5 = next(p for p in result["points"] if p["duration_s"] == 5)
    assert p5["best_watts"] == 97
    p10 = next(p for p in result["points"] if p["duration_s"] == 10)
    assert p10["best_watts"] == 94.5  # mean(90..99) = 945/10


def test_power_curve_constant_power():
    watts = [200] * 600
    result = compute_power_curve({"watts": watts}, durations=[5, 60, 300])
    for p in result["points"]:
        assert p["best_watts"] == 200


def test_power_curve_duration_longer_than_activity_omitted():
    watts = [200] * 60
    result = compute_power_curve({"watts": watts}, durations=[30, 120, 3600])
    durations_in = [p["duration_s"] for p in result["points"]]
    assert 30 in durations_in
    assert 120 not in durations_in
    omitted = [o["duration_s"] for o in result["omitted"]]
    assert 120 in omitted
    assert 3600 in omitted


def test_power_curve_no_watts_returns_error_marker():
    result = compute_power_curve({"heartrate": [140] * 100}, durations=[5])
    assert result == {"error": "no_power_data"}


def test_power_curve_includes_avg_and_np():
    watts = [200] * 600
    result = compute_power_curve({"watts": watts}, durations=[5])
    assert abs(result["avg_power"] - 200.0) < 0.01
    assert abs(result["normalized_power"] - 200.0) < 0.01
    assert result["duration_s"] == 600


def test_power_curve_treats_none_as_zero():
    watts = [200, None, 200, None] * 100  # avg of real values = 200, with None=0 → 100
    result = compute_power_curve({"watts": watts}, durations=[10])
    assert result["avg_power"] == 100.0


# ---------------------------------------------------------------------------
# compute_decoupling
# ---------------------------------------------------------------------------


def test_decoupling_no_drift_returns_zero():
    """Constant HR and power → 0% decoupling."""
    hr = [140] * 600
    watts = [200] * 600
    result = compute_decoupling({"heartrate": hr, "watts": watts}, segment_minutes=None)
    assert abs(result["decoupling_pct"]) < 0.01
    assert result["threshold_5pct_exceeded"] is False
    assert result["first_segment"]["avg_hr"] == 140.0
    assert result["second_segment"]["avg_hr"] == 140.0


def test_decoupling_rising_hr_falling_efficiency():
    """HR rises in 2nd half, power stays → decoupling > 0."""
    hr = [130] * 300 + [150] * 300  # 130 avg, then 150 avg
    watts = [200] * 600  # constant
    result = compute_decoupling({"heartrate": hr, "watts": watts}, segment_minutes=None)
    # 1st: NP/HR = 200/130 = 1.538; 2nd: NP/HR = 200/150 = 1.333
    # decoupling = (1.333 - 1.538) / 1.538 * 100 ≈ -13.3%
    # Negative = aerobic decoupling (efficiency dropped)
    assert result["decoupling_pct"] < -10


def test_decoupling_missing_hr_returns_error():
    result = compute_decoupling({"watts": [200] * 600}, segment_minutes=None)
    assert result == {"error": "missing_required_stream", "required": "heartrate"}


def test_decoupling_missing_watts_returns_error():
    result = compute_decoupling({"heartrate": [140] * 600}, segment_minutes=None)
    assert result == {"error": "missing_required_stream", "required": "watts"}


def test_decoupling_custom_segment_minutes():
    """segment_minutes=1 compares first 60s vs last 60s."""
    hr = [130] * 60 + [140] * 600 + [150] * 60  # warmup, middle, last
    watts = [200] * 720
    result = compute_decoupling(
        {"heartrate": hr, "watts": watts}, segment_minutes=1
    )
    # first 60s avg HR = 130; last 60s avg HR = 150
    assert result["first_segment"]["avg_hr"] == 130.0
    assert result["second_segment"]["avg_hr"] == 150.0


def test_decoupling_threshold_5pct_marker():
    hr = [130] * 300 + [150] * 300
    watts = [200] * 600
    result = compute_decoupling({"heartrate": hr, "watts": watts}, segment_minutes=None)
    assert result["threshold_5pct_exceeded"] is True
