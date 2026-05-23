"""Tests for stream_analysis.py — pure compute, no I/O."""

from strava_mcp_vault.stream_analysis import downsample


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
