"""Sport-type alias expansion."""

from strava_mcp_vault.sport_types import (
    RIDE_TYPES,
    RUN_TYPES,
    SWIM_TYPES,
    expand_sport_type,
)


def test_none_returns_none():
    assert expand_sport_type(None) is None


def test_empty_string_returns_none():
    assert expand_sport_type("") is None
    assert expand_sport_type("   ") is None


def test_single_strava_type_passes_through():
    assert expand_sport_type("Ride") == ["Ride"]
    assert expand_sport_type("GravelRide") == ["GravelRide"]


def test_camelcase_is_literal_not_alias():
    # "Ride".lower() == "ride", which is also an alias key. The literal
    # CamelCase form must NOT expand — backward compat with existing
    # callers passing Strava sport_types.
    assert expand_sport_type("Ride") == ["Ride"]
    assert expand_sport_type("Run") == ["Run"]


def test_lowercase_alias_expands_to_category():
    result = expand_sport_type("rides")
    assert set(result) == RIDE_TYPES
    result = expand_sport_type("running")
    assert set(result) == RUN_TYPES
    result = expand_sport_type("swims")
    assert set(result) == SWIM_TYPES


def test_alias_plural_and_singular_both_work():
    assert set(expand_sport_type("ride")) == RIDE_TYPES
    assert set(expand_sport_type("rides")) == RIDE_TYPES
    assert set(expand_sport_type("cycling")) == RIDE_TYPES


def test_comma_list_merges_types():
    result = expand_sport_type("Ride,Run")
    assert set(result) == {"Ride", "Run"}


def test_comma_list_can_mix_alias_and_literal():
    result = expand_sport_type("rides,Run")
    assert set(result) == RIDE_TYPES | {"Run"}


def test_duplicates_collapsed():
    result = expand_sport_type("Ride,Ride,GravelRide")
    assert sorted(result) == ["GravelRide", "Ride"]


def test_whitespace_around_tokens_tolerated():
    result = expand_sport_type(" Ride , Run ")
    assert set(result) == {"Ride", "Run"}


def test_unknown_alias_passes_through_as_literal():
    # If users invent a name like "FakeSport", we don't second-guess —
    # the DB query will just return zero rows. Better than silently
    # dropping the filter.
    assert expand_sport_type("FakeSport") == ["FakeSport"]
