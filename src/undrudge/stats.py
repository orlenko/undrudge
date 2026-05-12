"""Confidence intervals for binomial proportions.

Borrowed from autoharness (`src/autoharness/stats.py`). Used by the
probe-phase tooling to put error bars on the "what fraction of cited
evidence_refs resolved?" rate as we accumulate days of data: a 6/11
sample tells you very little about the true underlying rate; a
6000/11000 sample tells you a lot. The CI is the right way to say
that out loud.

Two intervals are exposed:

- :func:`wilson_interval` — the Wilson score interval. Recommended for
  small sample sizes (n < 200). Handles n=0, p=0, and p=1 gracefully
  without producing intervals outside [0, 1].
- :func:`normal_interval` — the textbook normal-approximation interval
  ``p ± z·sqrt(p(1-p)/n)``. Faster, simpler, accurate when ``np > 5``
  and ``n(1-p) > 5``. Provided for parity with autoharness's API; you
  should prefer ``wilson_interval`` in most cases.

Both return ``(lower, upper)``; both use a z critical value derived
from ``confidence``. Default is 0.95 (two-tailed z ≈ 1.96). No
external dependencies — the only numerics come from ``math``.
"""

from __future__ import annotations

import math


def _z_for_confidence(confidence: float) -> float:
    """Two-tailed z critical value via the inverse erf.

    Python's stdlib doesn't expose an inverse-erf, so we use the
    well-known relation ``z = sqrt(2) * erfinv(confidence)`` with an
    Acklam-style rational approximation. Accurate to ~1e-9 across the
    practical range of confidence levels we care about.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError(
            f"confidence must be in (0, 1); got {confidence!r}"
        )
    return math.sqrt(2.0) * _erfinv(confidence)


def _erfinv(x: float) -> float:
    """Inverse of math.erf via a rational approximation.

    Derived from the Beasley-Springer-Moro / Acklam tables. Good to
    about 1e-9 for |x| < 0.97; for tail values the error grows but
    stays under 1e-6, which is fine for CI work.
    """
    if not -1.0 < x < 1.0:
        raise ValueError(f"erfinv argument out of range: {x!r}")
    # Constants for Acklam's approximation
    a = [-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]
    # We want inverse-erf, but Acklam's table is for the inverse normal
    # CDF Φ⁻¹. Convert: erfinv(x) = Φ⁻¹((x+1)/2) / sqrt(2).
    p = (x + 1.0) / 2.0
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        z = (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
            ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
    elif p <= phigh:
        q = p - 0.5
        r = q * q
        z = (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
            (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        z = -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
            ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
    return z / math.sqrt(2.0)


def wilson_interval(
    successes: int, n: int, confidence: float = 0.95
) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Robust at small ``n`` and at the boundaries (``p=0`` or ``p=1``):
    the interval stays inside ``[0, 1]`` and remains usable when
    ``successes`` is 0 or equal to ``n``. ``n=0`` returns ``(0.0, 1.0)``
    — we know nothing, the interval is the whole space.
    """
    if successes < 0 or n < 0 or successes > n:
        raise ValueError(
            f"invalid (successes={successes}, n={n}): need 0 <= s <= n"
        )
    if n == 0:
        return (0.0, 1.0)
    z = _z_for_confidence(confidence)
    p_hat = successes / n
    denom = 1.0 + z * z / n
    centre = (p_hat + z * z / (2.0 * n)) / denom
    margin = z * math.sqrt(p_hat * (1.0 - p_hat) / n + z * z / (4.0 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def normal_interval(
    successes: int, n: int, confidence: float = 0.95
) -> tuple[float, float]:
    """Normal-approximation interval ``p ± z·sqrt(p(1-p)/n)``.

    Faster than Wilson but unreliable at small ``n`` or near the
    boundaries; prefer :func:`wilson_interval` unless you have a
    specific reason to use this form (parity with another tool that
    reports the normal CI, mainly).
    """
    if successes < 0 or n < 0 or successes > n:
        raise ValueError(
            f"invalid (successes={successes}, n={n}): need 0 <= s <= n"
        )
    if n == 0:
        return (0.0, 1.0)
    z = _z_for_confidence(confidence)
    p_hat = successes / n
    margin = z * math.sqrt(p_hat * (1.0 - p_hat) / n)
    return (max(0.0, p_hat - margin), min(1.0, p_hat + margin))
