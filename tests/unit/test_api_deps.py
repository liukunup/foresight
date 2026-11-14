"""Tests for the dependencies of the API.

The time bounds are normalized in one place, and the command line uses the
same normalization, so a bound means the same thing in the API and in the
terminal. The bounds are compared as strings against the stored
timestamps, so the spelling of a bound decides what it covers.
"""

from __future__ import annotations

import pytest

from foresight.api.deps import normalize_time_bounds


def test_a_bound_with_a_space_is_normalized_to_the_stored_form():
    """The stored timestamps use a `T` as the separator.

    A space sorts before a `T`, so a bound that keeps the space sorts
    before every timestamp of the day it names and excludes that day
    without saying so.
    """
    bounds = normalize_time_bounds(since="2026-11-11 13:45")
    assert bounds.since == "2026-11-11T13:45"


def test_an_upper_bound_with_a_space_is_normalized_too():
    bounds = normalize_time_bounds(until="2026-11-11 13:45")
    assert bounds.until == "2026-11-11T13:45"


def test_a_bound_with_a_space_keeps_its_fraction_and_its_offset():
    """Only the separator changes, the rest of the bound stays as it is."""
    bounds = normalize_time_bounds(since="2026-11-11 13:45:30.123456+00:00")
    assert bounds.since == "2026-11-11T13:45:30.123456+00:00"


def test_a_bound_with_a_t_is_left_alone():
    bounds = normalize_time_bounds(since="2026-11-11T13:45:30+00:00")
    assert bounds.since == "2026-11-11T13:45:30+00:00"


def test_a_bare_date_still_names_the_whole_day():
    """The day is not touched by the normalization of the separator."""
    bounds = normalize_time_bounds(since="2026-11-11", until="2026-11-11")
    assert bounds.since == "2026-11-11T00:00:00+00:00"
    assert bounds.until == "2026-11-11T23:59:59.999999+00:00"


def test_an_unreadable_bound_is_refused():
    with pytest.raises(ValueError):
        normalize_time_bounds(since="2026-11-11 13:45 yesterday")
