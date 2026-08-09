"""Small unit-conversion/derived-value helpers. Temperature is stored
canonically in Fahrenheit throughout (per the resolved project decision --
every control constant in every design doc is in F); this is the one place
a C-preferring display converts, so there's exactly one conversion to get
right rather than one per call site.
"""
from __future__ import annotations


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def abv_pct(og: float, fg: float) -> float:
    """Standard homebrew approximation. Not exact (real attenuation is
    nonlinear at high gravity), but it's the formula every yeast-preset
    label and hydrometer app uses, so matching it is more useful than a
    more "correct" formula that disagrees with what users expect to see."""
    return round((og - fg) * 131.25, 2)
