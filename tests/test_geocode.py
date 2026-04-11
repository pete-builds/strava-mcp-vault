"""Tests for cache/geocode.py."""

import json
from unittest.mock import MagicMock, patch

from cache.geocode import forward_geocode, reverse_geocode_many


def _mock_urlopen(response_data):
    """Create a mock urlopen context manager returning JSON data."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


@patch("cache.geocode.time.sleep")
@patch("cache.geocode.time.monotonic", return_value=100.0)
@patch("cache.geocode.urllib.request.urlopen")
async def test_forward_geocode_success(mock_urlopen, mock_monotonic, mock_sleep):
    mock_urlopen.return_value = _mock_urlopen([{"lat": "42.4440", "lon": "-76.5019"}])
    result = await forward_geocode("Ithaca, NY")
    assert result is not None
    lat, lon = result
    assert abs(lat - 42.4440) < 0.001
    assert abs(lon - (-76.5019)) < 0.001


@patch("cache.geocode.time.sleep")
@patch("cache.geocode.time.monotonic", return_value=100.0)
@patch("cache.geocode.urllib.request.urlopen")
async def test_forward_geocode_not_found(mock_urlopen, mock_monotonic, mock_sleep):
    mock_urlopen.return_value = _mock_urlopen([])
    result = await forward_geocode("Nonexistent Place XYZ123")
    assert result is None


@patch("cache.geocode.time.sleep")
@patch("cache.geocode.time.monotonic", return_value=100.0)
@patch("cache.geocode.urllib.request.urlopen")
async def test_reverse_geocode_many_basic(mock_urlopen, mock_monotonic, mock_sleep):
    mock_urlopen.return_value = _mock_urlopen({"address": {"city": "Ithaca", "state": "New York"}})
    coords = [(42.4440, -76.5019)]
    result = await reverse_geocode_many(coords)
    assert (42.4440, -76.5019) in result
    assert "Ithaca" in result[(42.4440, -76.5019)]


@patch("cache.geocode.time.sleep")
@patch("cache.geocode.time.monotonic", return_value=100.0)
@patch("cache.geocode.urllib.request.urlopen")
async def test_reverse_geocode_many_deduplication(mock_urlopen, mock_monotonic, mock_sleep):
    """Two nearby coords that round to the same key should only trigger one HTTP call."""
    mock_urlopen.return_value = _mock_urlopen({"address": {"city": "Ithaca", "state": "New York"}})
    coords = [
        (42.4440, -76.5019),
        (42.4445, -76.5015),  # rounds to same (42.44, -76.50)
        (43.0481, -76.1474),  # different city
    ]
    result = await reverse_geocode_many(coords)
    assert len(result) == 3
    # urlopen should have been called only twice (2 unique rounded keys)
    assert mock_urlopen.call_count == 2


@patch("cache.geocode.time.sleep")
@patch("cache.geocode.time.monotonic", return_value=100.0)
@patch("cache.geocode.urllib.request.urlopen")
async def test_reverse_geocode_failure_returns_empty(mock_urlopen, mock_monotonic, mock_sleep):
    """If one geocode fails, it should return empty string for that coord."""
    mock_urlopen.side_effect = Exception("Network error")
    coords = [(42.4440, -76.5019)]
    result = await reverse_geocode_many(coords)
    assert result[(42.4440, -76.5019)] == ""
