import pandas as pd
import pytest

from analyst.stages import analyzer as A


def frame(rows):
    """rows: (segment, period, num, den)"""
    return pd.DataFrame(rows, columns=["segment", "period", "num", "den"])


def test_decomposition_identity_holds():
    df = frame([
        ("CARD", "baseline", 950, 1000), ("CARD", "current", 470, 500),
        ("UPI", "baseline", 440, 500), ("UPI", "current", 860, 1000),
    ])
    d = A.decompose(df, is_ratio=True)
    assert d.mix_total + d.rate_total == pytest.approx(d.delta, abs=1e-12)


def test_pure_mix_shift_has_zero_rate_effect():
    # Rates unchanged (95% and 88%); only the mix moves.
    df = frame([
        ("CARD", "baseline", 950, 1000), ("CARD", "current", 475, 500),
        ("UPI", "baseline", 440, 500), ("UPI", "current", 880, 1000),
    ])
    d = A.decompose(df, is_ratio=True)
    assert d.delta < 0
    assert d.rate_total == pytest.approx(0, abs=1e-12)
    assert d.mix_total == pytest.approx(d.delta)


def test_pure_rate_change_has_zero_mix_effect():
    df = frame([
        ("CARD", "baseline", 950, 1000), ("CARD", "current", 900, 1000),
        ("UPI", "baseline", 880, 1000), ("UPI", "current", 880, 1000),
    ])
    d = A.decompose(df, is_ratio=True)
    assert d.mix_total == pytest.approx(0, abs=1e-12)
    assert d.rate_total == pytest.approx(d.delta)


def test_new_segment_keeps_identity():
    df = frame([
        ("CARD", "baseline", 950, 1000), ("CARD", "current", 950, 1000),
        ("BNPL", "current", 50, 100),
    ])
    d = A.decompose(df, is_ratio=True)
    assert d.mix_total + d.rate_total == pytest.approx(d.delta)


def test_simpsons_paradox_detected_as_mix():
    """Both segments improve, yet the total falls - the decomposition must show why."""
    df = frame([
        ("CARD", "baseline", 900, 1000), ("CARD", "current", 182, 200),
        ("UPI", "baseline", 70, 100), ("UPI", "current", 720, 1000),
    ])
    totals = A.decompose(df, is_ratio=True)
    by = {"payment_method": A.decompose(df, is_ratio=True, dimension="payment_method")}
    assert totals.delta < 0
    assert all(s.value1 > s.value0 for s in by["payment_method"].segments)
    dim, driver = A.find_mix_dimension(totals, by)
    assert dim == "payment_method" and driver.share_of_delta > 1  # mix > total, rate offsets
    assert A.classify(totals, driver, 0.001) == "mix_driven"


def test_additive_metric_normalises_by_days():
    df = frame([("all", "baseline", 7000, 7000), ("all", "current", 6000, 6000)])
    d = A.decompose(df, is_ratio=False, days0=7, days1=6)
    assert d.value0 == pytest.approx(1000) and d.value1 == pytest.approx(1000)
    assert d.delta == pytest.approx(0)


def test_rate_driver_requires_significance():
    # 20 vs 18 successes out of 200: not significant.
    df = frame([("X", "baseline", 180, 200), ("X", "current", 178, 200),
                ("Y", "baseline", 9000, 10000), ("Y", "current", 9000, 10000)])
    d = A.decompose(df, is_ratio=True, dimension="seg")
    assert A.find_rate_drivers(d.delta, [d]) == []


def test_specific_cell_preferred_over_parent():
    parent = A.decompose(frame([
        ("NB", "baseline", 900, 1000), ("NB", "current", 800, 1000),
        ("CARD", "baseline", 9500, 10000), ("CARD", "current", 9500, 10000),
    ]), is_ratio=True, dimension="payment_method")
    child = A.decompose(frame([
        ("NB | BankC", "baseline", 180, 200), ("NB | BankC", "current", 85, 200),
        ("NB | Other", "baseline", 720, 800), ("NB | Other", "current", 715, 800),
        ("CARD | Other", "baseline", 9500, 10000), ("CARD | Other", "current", 9500, 10000),
    ]), is_ratio=True, dimension="payment_method x bank")
    drivers = A.find_rate_drivers(parent.delta, [parent, child])
    assert [d.segment for d in drivers] == ["NB | BankC"]


def test_p_value_basic():
    assert A.two_proportion_p_value(500, 1000, 500, 1000) == pytest.approx(1.0)
    assert A.two_proportion_p_value(900, 1000, 800, 1000) < 1e-6
    assert A.two_proportion_p_value(0, 0, 1, 1) is None


def test_trend_flags_anomaly():
    rows = [{"period_start": f"2026-10-{d:02d}", "segment": "all",
             "num": 920 if d != 15 else 600, "den": 1000} for d in range(1, 29)]
    pts = A.compute_trend(pd.DataFrame(rows), is_ratio=True)
    flagged = [p.period_start for p in pts if p.anomaly]
    assert flagged == ["2026-10-15"]
