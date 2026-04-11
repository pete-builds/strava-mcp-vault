from server import _validate_radius_miles


def test_radius_validation_bounds():
    assert _validate_radius_miles(20.0) is None
    assert "greater than 0" in _validate_radius_miles(0)
    assert "greater than 0" in _validate_radius_miles(-5)
    assert "250 miles" in _validate_radius_miles(300)
