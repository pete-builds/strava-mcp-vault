"""A published photo must not carry where it was taken.

Strava attaches a full-precision EXIF coordinate to every photo record. Probed
on 2026-09-13 against a real public gravel ride, one photo carried
`[42.51111111111111, -76.55777777777777]`. That is the shutter position, not the
trimmed route, and on a photo taken before setting off it is the front door.

Nothing downstream would catch it: it arrives as an ordinary field and a page
that only renders `urls` still commits it to a public JSON file.
"""

from __future__ import annotations

import json

from strava_mcp_vault.photos import FORBIDDEN_FIELDS, pick_for_spot, strip_photo

REAL_SHAPE = {
    "unique_id": "e03bb263-c7a8-4c80-bf7e-cf76e4719165",
    "athlete_id": 3404924,
    "activity_id": 17557028669,
    "activity_name": "Gravel Ride",
    "caption": "",
    "type": 1,
    "source": 1,
    "status": 3,
    "uploaded_at": "2026-02-28T19:59:21Z",
    "created_at_local": "2026-02-28T12:42:00Z",
    "urls": {"2048": "https://dgtzuqphqg23d.cloudfront.net/abc-1535x2048.jpg"},
    "sizes": {"2048": [1535, 2048]},
    "location": [42.51111111111111, -76.55777777777777],
}


def test_the_exif_coordinate_is_stripped():
    out = strip_photo(REAL_SHAPE, 2048)
    assert out is not None
    assert "location" not in out
    body = json.dumps(out)
    assert "42.51111" not in body
    assert "-76.55777" not in body


def test_no_forbidden_field_survives_whatever_it_is_called():
    out = strip_photo(REAL_SHAPE, 2048)
    for field in FORBIDDEN_FIELDS:
        assert field not in out


def test_the_useful_parts_do_survive():
    out = strip_photo(REAL_SHAPE, 2048)
    assert out["url"].endswith(".jpg")
    assert out["activity_id"] == 17557028669
    assert out["width"] == 1535
    assert out["height"] == 2048


def test_a_photo_without_the_requested_size_is_dropped():
    """A placeholder or a still-processing upload has no usable rendition."""
    assert strip_photo({**REAL_SHAPE, "urls": {}}, 2048) is None
    assert strip_photo({**REAL_SHAPE, "urls": {"1024": "x"}}, 2048) is None


def test_an_integer_size_key_is_accepted():
    photo = {**REAL_SHAPE, "urls": {2048: "https://example.test/a.jpg"}, "sizes": {2048: [10, 20]}}
    out = strip_photo(photo, 2048)
    assert out["url"] == "https://example.test/a.jpg"
    assert out["width"] == 10


def test_a_captioned_photo_is_preferred():
    plain = strip_photo(REAL_SHAPE, 2048)
    captioned = strip_photo({**REAL_SHAPE, "caption": "Peak fall"}, 2048)
    assert pick_for_spot([plain, captioned], 1)[0]["caption"] == "Peak fall"


def test_pick_respects_the_limit():
    photos = [strip_photo({**REAL_SHAPE, "unique_id": str(i)}, 2048) for i in range(5)]
    assert len(pick_for_spot(photos, 2)) == 2
    assert len(pick_for_spot(photos, 1)) == 1


def test_pick_on_an_empty_set_is_empty_not_an_error():
    assert pick_for_spot([], 1) == []
