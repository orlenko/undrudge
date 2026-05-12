"""Confidence-interval math."""

from __future__ import annotations

import math

import pytest

from undrudge import stats


def test_wilson_handles_zero_sample(tmp_path):
    assert stats.wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_handles_zero_successes():
    lo, hi = stats.wilson_interval(0, 10)
    assert lo == 0.0
    assert 0.2 < hi < 0.4  # Wilson upper for 0/10 at 95% is ~0.308


def test_wilson_handles_all_successes():
    lo, hi = stats.wilson_interval(10, 10)
    assert hi == 1.0
    assert 0.6 < lo < 0.8  # Wilson lower for 10/10 at 95% is ~0.692


def test_wilson_centres_near_p_hat_for_large_n():
    """At n=1000 the interval should be tight around p̂ = 0.6."""
    lo, hi = stats.wilson_interval(600, 1000)
    assert lo < 0.6 < hi
    assert hi - lo < 0.07  # ±~3% for 95% CI at this n


def test_wilson_matches_known_example():
    """Sanity-check against the textbook 12/40 example.

    Wilson 95% interval for 12 successes in 40 trials is approximately
    (0.182, 0.451) per most references.
    """
    lo, hi = stats.wilson_interval(12, 40)
    assert lo == pytest.approx(0.182, abs=0.005)
    assert hi == pytest.approx(0.451, abs=0.005)


def test_normal_interval_matches_textbook():
    """Normal-approx 95% CI for 60/100 is roughly (0.504, 0.696)."""
    lo, hi = stats.normal_interval(60, 100)
    assert lo == pytest.approx(0.504, abs=0.005)
    assert hi == pytest.approx(0.696, abs=0.005)


def test_normal_interval_handles_zero_n():
    assert stats.normal_interval(0, 0) == (0.0, 1.0)


def test_intervals_reject_invalid_inputs():
    with pytest.raises(ValueError):
        stats.wilson_interval(-1, 10)
    with pytest.raises(ValueError):
        stats.wilson_interval(5, 4)
    with pytest.raises(ValueError):
        stats.normal_interval(-1, 10)


def test_z_for_confidence_returns_known_z():
    """z(0.95) ≈ 1.95996 (two-tailed)."""
    z = stats._z_for_confidence(0.95)
    assert z == pytest.approx(1.95996, abs=0.001)


def test_z_for_confidence_rejects_out_of_range():
    for bad in (0.0, 1.0, -0.1, 1.1):
        with pytest.raises(ValueError):
            stats._z_for_confidence(bad)


def test_wilson_and_normal_agree_in_the_centre():
    """At p̂=0.5 with large n the two methods should be nearly identical."""
    wl, wh = stats.wilson_interval(500, 1000)
    nl, nh = stats.normal_interval(500, 1000)
    assert math.isclose(wl, nl, abs_tol=0.01)
    assert math.isclose(wh, nh, abs_tol=0.01)
