"""Full-resolution tracks must never reach further than Strava already did.

The one property that matters here: Strava privacy-zone trims the polyline it
publishes and does NOT trim the latlng stream underneath it. So a track built
from the raw stream republishes the exact start point Strava was hiding, on a
route that looks unchanged. There is no visible difference between a clipped
track and an unclipped one, which is why these assert on the geometry rather
than on the fact that a track came back at all.
"""

from __future__ import annotations

import math

import pytest

from strava_mcp_vault.tracks import (
    ClipError,
    build_track,
    clip_to_polyline,
    decode_polyline,
    haversine_m,
    normalize_streams,
    simplify,
)


def encode_polyline(points):
    """Encode [(lat, lon), ...] so a fixture can state its polyline directly."""

    def enc(value):
        v = int(round(value * 1e5)) << 1
        if v < 0:
            v = ~v
        out = ""
        while v >= 0x20:
            out += chr((0x20 | (v & 0x1F)) + 63)
            v >>= 5
        return out + chr(v + 63)

    out = ""
    prev_lat = prev_lon = 0
    for lat, lon in points:
        ilat, ilon = int(round(lat * 1e5)), int(round(lon * 1e5))
        out += enc((ilat - prev_lat) / 1e5) + enc((ilon - prev_lon) / 1e5)
        prev_lat, prev_lon = ilat, ilon
    return out


def line(start_lat, start_lon, n, step=0.0002):
    """A straight run of points roughly 22 m apart."""
    return [(start_lat + i * step, start_lon) for i in range(n)]


def streams_from(points, base_ele=300.0):
    return {
        "latlng": {"data": [[lat, lon] for lat, lon in points]},
        "altitude": {"data": [base_ele + i for i in range(len(points))]},
        "time": {"data": list(range(len(points)))},
    }


def test_polyline_round_trip():
    points = [(42.3451, -76.3505), (42.3461, -76.3495), (42.3471, -76.3485)]
    decoded = decode_polyline(encode_polyline(points))
    assert len(decoded) == len(points)
    for (a_lat, a_lon), (b_lat, b_lon) in zip(decoded, points):
        assert haversine_m(a_lat, a_lon, b_lat, b_lon) < 2


# --- The clipping guarantee ---


def test_a_stream_running_past_both_ends_is_clipped_back_to_the_polyline():
    """The real shape of the problem: Strava hid the first and last stretch."""
    full = line(42.3400, -76.3505, 60)
    # What Strava published: the middle, with both ends trimmed away.
    published = full[10:-10]
    result = build_track(streams_from(full), encode_polyline(published), tolerance_m=0)

    assert result["trimmed_head"] == 10
    assert result["trimmed_tail"] == 10
    first = result["points"][0]
    last = result["points"][-1]
    assert haversine_m(first[0], first[1], *published[0]) < 5
    assert haversine_m(last[0], last[1], *published[-1]) < 5

    # And the hidden start is genuinely gone, not merely unused.
    hidden_lat = full[0][0]
    assert all(abs(p[0] - hidden_lat) > 1e-6 for p in result["points"])


def test_clip_indices_match_the_polyline_extent():
    full = line(42.3400, -76.3505, 40)
    published = full[7:-5]
    start, end = clip_to_polyline(full, published)
    assert start == 7
    assert end == len(full) - 1 - 5


def test_an_untrimmed_activity_keeps_everything():
    """95 of 256 real activities are not trimmed at all. Those must not shrink."""
    full = line(42.3400, -76.3505, 30)
    result = build_track(streams_from(full), encode_polyline(full), tolerance_m=0)
    assert result["trimmed_head"] == 0
    assert result["trimmed_tail"] == 0
    assert result["clipped_points"] == 30


def test_a_stream_that_cannot_be_matched_is_refused_not_guessed():
    """Fail closed: an unboundable recording may expose a trimmed start."""
    full = line(42.3400, -76.3505, 30)
    elsewhere = line(44.0000, -73.0000, 10)
    with pytest.raises(ClipError):
        clip_to_polyline(full, elsewhere)


def test_build_track_propagates_the_refusal():
    full = line(42.3400, -76.3505, 30)
    with pytest.raises(ClipError):
        build_track(streams_from(full), encode_polyline(line(44.0, -73.0, 5)))


def test_no_polyline_is_a_refusal_rather_than_a_raw_stream():
    full = line(42.3400, -76.3505, 30)
    with pytest.raises(ClipError):
        build_track(streams_from(full), "")


def test_an_out_and_back_is_not_clipped_to_a_fragment():
    """A loop puts the finish near the start.

    A nearest-point search would match the tail against an early index and
    return a sliver of the route. Walking in from each end does not.
    """
    out = line(42.3400, -76.3505, 25)
    back = list(reversed(out))[1:]
    full = out + back
    published = full[4:-4]
    result = build_track(streams_from(full), encode_polyline(published), tolerance_m=0)
    # Nearly the whole ride survives, rather than collapsing to the first few.
    assert result["clipped_points"] >= len(full) - 10


# --- Simplification ---


def test_simplify_keeps_the_shape_and_both_ends():
    points = [(42.34 + i * 0.0001, -76.35, 300.0) for i in range(200)]
    out = simplify(points, 5.0)
    assert out[0] == points[0]
    assert out[-1] == points[-1]
    assert len(out) < len(points)


def test_simplify_is_a_no_op_at_zero_tolerance():
    points = [(42.34 + i * 0.0001, -76.35, 300.0) for i in range(20)]
    assert simplify(points, 0) == points


def test_simplify_survives_a_long_stream_without_recursion_limits():
    points = [(42.34 + i * 0.00001, -76.35 + math.sin(i / 50) * 0.001, 300.0) for i in range(20000)]
    out = simplify(points, 5.0)
    assert 2 <= len(out) < len(points)


def test_elevation_is_carried_and_rounded():
    full = line(42.3400, -76.3505, 20)
    result = build_track(streams_from(full, base_ele=312.345), encode_polyline(full), tolerance_m=0)
    assert result["has_elevation"] is True
    assert result["points"][0][2] == pytest.approx(312.3, abs=0.05)


def test_time_is_off_by_default_and_present_on_request():
    full = line(42.3400, -76.3505, 20)
    poly = encode_polyline(full)
    assert len(build_track(streams_from(full), poly, tolerance_m=0)["points"][0]) == 3
    withtime = build_track(streams_from(full), poly, tolerance_m=0, include_time=True)
    assert len(withtime["points"][0]) == 4


def test_precision_is_applied():
    full = line(42.3400, -76.3505, 10)
    result = build_track(streams_from(full), encode_polyline(full), tolerance_m=0, precision=3)
    lat = result["points"][0][0]
    assert lat == round(lat, 3)


# --- The shape the live API actually returns ---
#
# These exist because every test above passed against a dict fixture while the
# first real call failed with "'list' object has no attribute 'get'". Strava's
# streams endpoint answers with a LIST of stream objects for key_type=time. A
# fixture invented from the docs cannot catch that; only the real shape can.


def streams_as_list(points, base_ele=300.0):
    """The live shape: a list of {type, data} objects."""
    return [
        {"type": "latlng", "data": [[lat, lon] for lat, lon in points], "series_type": "distance"},
        {"type": "altitude", "data": [base_ele + i for i in range(len(points))]},
        {"type": "time", "data": list(range(len(points)))},
    ]


def test_build_track_accepts_the_list_shape_the_api_returns():
    full = line(42.3400, -76.3505, 40)
    published = full[6:-6]
    result = build_track(streams_as_list(full), encode_polyline(published), tolerance_m=0)
    assert result["trimmed_head"] == 6
    assert result["trimmed_tail"] == 6
    assert result["has_elevation"] is True


def test_both_shapes_produce_the_same_track():
    full = line(42.3400, -76.3505, 30)
    poly = encode_polyline(full[4:-4])
    as_dict = build_track(streams_from(full), poly, tolerance_m=0)
    as_list = build_track(streams_as_list(full), poly, tolerance_m=0)
    assert as_dict["points"] == as_list["points"]


@pytest.mark.parametrize(
    "raw",
    [
        [{"type": "latlng", "data": [[1.0, 2.0]]}],
        {"latlng": {"data": [[1.0, 2.0]]}},
        {"latlng": [[1.0, 2.0]]},
    ],
)
def test_normalize_handles_every_documented_shape(raw):
    assert normalize_streams(raw)["latlng"] == [[1.0, 2.0]]


def test_normalize_is_empty_on_nonsense_rather_than_raising():
    assert normalize_streams(None) == {}
    assert normalize_streams("nope") == {}
