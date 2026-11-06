from datetime import date

import pytest

from hermes_garmin.dates import ArgError, chunks, resolve_range

TODAY = date(2026, 11, 6)


@pytest.mark.parametrize("args, expected", [
    ({}, (TODAY, TODAY)),
    ({"date": "yesterday"}, (date(2026, 11, 5), date(2026, 11, 5))),
    ({"days": 7}, (date(2026, 10, 31), TODAY)),
    ({"start_date": "2026-10-01"}, (date(2026, 10, 1), TODAY)),
    ({"start_date": "2026-10-01", "end_date": "2026-10-03"}, (date(2026, 10, 1), date(2026, 10, 3))),
    ({"end_date": "2026-12-31", "start_date": "2026-11-01"}, (date(2026, 11, 1), TODAY)),  # clamped
])
def test_resolve_range(args, expected):
    assert resolve_range(args, TODAY) == expected


def test_default_days():
    assert resolve_range({}, TODAY, default_days=30) == (date(2026, 10, 8), TODAY)


@pytest.mark.parametrize("args, message", [
    ({"date": "06/11/2026"}, "YYYY-MM-DD"),
    ({"days": "many"}, "whole number"),
    ({"days": 0}, "at least 1"),
    ({"start_date": "2026-11-05", "end_date": "2026-11-01"}, "after"),
    ({"days": 200}, "at most 93"),
])
def test_bad_ranges(args, message):
    with pytest.raises(ArgError, match=message):
        resolve_range(args, TODAY)


def test_chunks_cover_the_range_without_overlap():
    windows = list(chunks(date(2026, 1, 1), date(2026, 3, 1), 28))
    assert windows[0] == (date(2026, 1, 1), date(2026, 1, 28))
    assert windows[-1][1] == date(2026, 3, 1)
    assert all((b - a).days <= 27 for a, b in windows)
    assert all(nxt[0].toordinal() == prev[1].toordinal() + 1 for prev, nxt in zip(windows, windows[1:]))
    assert list(chunks(TODAY, TODAY, 31)) == [(TODAY, TODAY)]
