"""Synthetic payments data generator.

Builds a reproducible DuckDB database with four related tables (transactions,
merchants, banks, settlements) plus a small data_freshness table. Several
problems are planted on purpose so the analyst has something real to find.
Every planted scenario is listed in PLANTED_SCENARIOS and documented in the README.

Usage:
    python -m data.generate --out data/payments.duckdb --seed 42
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #
START_DATE = date(2026, 9, 21)  # Monday, week 1
NUM_DAYS = 56  # 8 full weeks -> ends Sunday 2026-11-15
WEEK8_START = START_DATE + timedelta(days=49)  # 2026-11-09
LAST_DAY = START_DATE + timedelta(days=NUM_DAYS - 1)  # 2026-11-15
LOADED_THROUGH = datetime.combine(LAST_DAY, datetime.min.time()) + timedelta(hours=9)

BASE_DAILY_TXNS = 6000

# --------------------------------------------------------------------------- #
# Planted scenarios (the "answer key" - also used by tests and eval)
# --------------------------------------------------------------------------- #
OUTAGE_BANK_ID = "B03"  # Coastal Bank
OUTAGE_DAYS = [date(2026, 10, 21), date(2026, 10, 22)]
DUPLICATE_DAY = date(2026, 11, 4)
DUPLICATE_FRACTION = 0.08
NULL_BANK_DAYS = [date(2026, 10, 7), date(2026, 10, 8)]
NULL_BANK_FRACTION = 0.06
SETTLEMENT_MISMATCH_WEEK_START = date(2026, 10, 26)  # week 6
SETTLEMENT_MISMATCH_COUNT = 12

PLANTED_SCENARIOS = {
    "mix_shift": (
        f"From {WEEK8_START} (week 8) UPI share jumps from ~30% to ~55% of transactions. "
        "UPI has a lower success rate than cards, so overall success rate falls even though "
        "most per-method rates are unchanged."
    ),
    "upi_online_rate_dip": (
        "In week 8, UPI on the ONLINE channel also dips from ~87% to ~85% - a small genuine "
        "rate effect on top of the mix effect."
    ),
    "bank_outage": (
        f"Bank {OUTAGE_BANK_ID} (Coastal Bank) netbanking fails with ISSUER_DOWN on "
        f"{OUTAGE_DAYS[0]} and {OUTAGE_DAYS[1]} (success rate ~35%)."
    ),
    "duplicates": (
        f"~{int(DUPLICATE_FRACTION * 100)}% of transactions on {DUPLICATE_DAY} are exact "
        "duplicates (ingestion replay) - same txn_id appears twice."
    ),
    "missing_bank_id": (
        f"On {NULL_BANK_DAYS[0]} and {NULL_BANK_DAYS[1]}, ~{int(NULL_BANK_FRACTION * 100)}% of "
        "non-wallet transactions are missing bank_id. (Wallet rows have NULL bank_id by design.)"
    ),
    "partial_last_day": (
        f"{LAST_DAY} is only loaded until {LOADED_THROUGH:%H:%M} - see data_freshness."
    ),
    "settlement_mismatch": (
        f"{SETTLEMENT_MISMATCH_COUNT} merchant-days in the week of "
        f"{SETTLEMENT_MISMATCH_WEEK_START} have settlement txn_count/gross_amount "
        "that disagree with the transactions table."
    ),
}

# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
BANKS = [
    # bank_id, name, type, volume weight, success-rate offset
    ("B01", "Aravali Bank", "private", 0.22, 0.004),
    ("B02", "Ganga Bank", "public", 0.18, -0.006),
    ("B03", "Coastal Bank", "private", 0.12, 0.000),
    ("B04", "Deccan Bank", "private", 0.14, 0.002),
    ("B05", "Himalaya Bank", "public", 0.10, -0.008),
    ("B06", "Konkan Bank", "small_finance", 0.08, -0.004),
    ("B07", "Narmada Bank", "public", 0.09, -0.002),
    ("B08", "Sahyadri Bank", "small_finance", 0.07, 0.000),
]

CITIES = {
    "Delhi": "North", "Noida": "North", "Jaipur": "North", "Lucknow": "North",
    "Mumbai": "West", "Pune": "West", "Ahmedabad": "West",
    "Bengaluru": "South", "Chennai": "South", "Hyderabad": "South", "Kochi": "South",
    "Kolkata": "East", "Bhubaneswar": "East", "Patna": "East",
}

# category -> (POS share of the merchant's transactions, median ticket in INR)
CATEGORIES = {
    "grocery": (0.85, 650),
    "electronics": (0.55, 9000),
    "fashion": (0.60, 2200),
    "fuel": (0.95, 1500),
    "restaurants": (0.75, 900),
    "pharmacy": (0.80, 500),
    "travel": (0.10, 6500),
    "digital_services": (0.02, 450),
}

TIERS = [("enterprise", 0.10), ("mid_market", 0.30), ("small", 0.60)]

# Payment-method shares per channel: baseline (weeks 1-7) vs week 8.
METHOD_SHARES = {
    "POS": {
        "base": {"CARD": 0.62, "UPI": 0.30, "WALLET": 0.08},
        "week8": {"CARD": 0.37, "UPI": 0.56, "WALLET": 0.07},
    },
    "ONLINE": {
        "base": {"CARD": 0.36, "UPI": 0.30, "NETBANKING": 0.24, "WALLET": 0.10},
        "week8": {"CARD": 0.22, "UPI": 0.54, "NETBANKING": 0.16, "WALLET": 0.08},
    },
}

# Success rate among non-cancelled attempts, per (method, channel).
SUCCESS_RATES = {
    ("CARD", "POS"): 0.965,
    ("CARD", "ONLINE"): 0.935,
    ("UPI", "POS"): 0.900,
    ("UPI", "ONLINE"): 0.870,
    ("NETBANKING", "ONLINE"): 0.910,
    ("WALLET", "POS"): 0.940,
    ("WALLET", "ONLINE"): 0.925,
}
WEEK8_UPI_ONLINE_RATE = 0.850
OUTAGE_SUCCESS_RATE = 0.35
CANCEL_RATE = 0.03

FAIL_CODES = {
    "CARD": (["INSUFFICIENT_FUNDS", "AUTH_FAILED", "RISK_DECLINED", "TIMEOUT"],
             [0.40, 0.30, 0.15, 0.15]),
    "UPI": (["INSUFFICIENT_FUNDS", "AUTH_FAILED", "TIMEOUT", "ISSUER_DOWN"],
            [0.30, 0.25, 0.35, 0.10]),
    "NETBANKING": (["AUTH_FAILED", "TIMEOUT", "ISSUER_DOWN"], [0.45, 0.35, 0.20]),
    "WALLET": (["INSUFFICIENT_FUNDS", "TIMEOUT", "RISK_DECLINED"], [0.50, 0.30, 0.20]),
}

MDR = {"CARD": 0.018, "UPI": 0.0, "NETBANKING": 0.012, "WALLET": 0.015}
CARD_NETWORKS = (["VISA", "MASTERCARD", "RUPAY"], [0.45, 0.35, 0.20])
CARD_BINS = {"VISA": "4", "MASTERCARD": "5", "RUPAY": "6"}

# Hour-of-day weights (0-23): quiet nights, lunch and evening peaks.
HOUR_WEIGHTS = np.array(
    [1, 0.6, 0.4, 0.3, 0.3, 0.5, 1.2, 2.5, 4, 5, 6, 6.5,
     7.5, 7, 6, 5.5, 5.5, 6, 7, 8, 8, 6.5, 4, 2], dtype=float
)
HOUR_WEIGHTS /= HOUR_WEIGHTS.sum()


@dataclass
class GeneratedData:
    banks: pd.DataFrame
    merchants: pd.DataFrame
    transactions: pd.DataFrame
    settlements: pd.DataFrame
    data_freshness: pd.DataFrame


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def build_banks() -> pd.DataFrame:
    return pd.DataFrame(
        [(b[0], b[1], b[2]) for b in BANKS], columns=["bank_id", "bank_name", "bank_type"]
    )


def build_merchants(rng: np.random.Generator, n: int = 400) -> pd.DataFrame:
    cities = list(CITIES)
    categories = list(CATEGORIES)
    tier_names = [t[0] for t in TIERS]
    tier_p = [t[1] for t in TIERS]

    tier = rng.choice(tier_names, size=n, p=tier_p)
    tier_volume = {"enterprise": 12.0, "mid_market": 3.0, "small": 1.0}
    weight = np.array([tier_volume[t] for t in tier]) * rng.lognormal(0, 0.5, size=n)

    df = pd.DataFrame(
        {
            "merchant_id": [f"M{i:04d}" for i in range(1, n + 1)],
            "category": rng.choice(categories, size=n),
            "city": rng.choice(cities, size=n),
            "tier": tier,
            "onboarded_on": [
                START_DATE - timedelta(days=int(d)) for d in rng.integers(60, 1500, size=n)
            ],
        }
    )
    df["merchant_name"] = [
        f"{c.replace('_', ' ').title()} Store {mid[1:]}"
        for c, mid in zip(df["category"], df["merchant_id"], strict=True)
    ]
    df["region"] = df["city"].map(CITIES)
    df["_volume_weight"] = weight / weight.sum()
    return df[
        ["merchant_id", "merchant_name", "category", "city", "region", "tier",
         "onboarded_on", "_volume_weight"]
    ]


def _daily_volume(rng: np.random.Generator, day: date) -> int:
    vol = BASE_DAILY_TXNS
    if day.weekday() >= 5:
        vol *= 1.15
    if day >= WEEK8_START:
        vol *= 1.10  # campaign week brings extra traffic
    return int(rng.normal(vol, vol * 0.03))


def _choose_methods(rng, channels: np.ndarray, week8: bool) -> np.ndarray:
    methods = np.empty(len(channels), dtype=object)
    period = "week8" if week8 else "base"
    for ch in ("POS", "ONLINE"):
        mask = channels == ch
        shares = METHOD_SHARES[ch][period]
        methods[mask] = rng.choice(list(shares), size=mask.sum(), p=list(shares.values()))
    return methods


def build_day(rng: np.random.Generator, day: date, merchants: pd.DataFrame,
              start_seq: int) -> pd.DataFrame:
    n = _daily_volume(rng, day)
    week8 = day >= WEEK8_START

    m_idx = rng.choice(len(merchants), size=n, p=merchants["_volume_weight"].to_numpy())
    m = merchants.iloc[m_idx].reset_index(drop=True)
    pos_share = m["category"].map(lambda c: CATEGORIES[c][0]).to_numpy()
    channel = np.where(rng.random(n) < pos_share, "POS", "ONLINE")
    method = _choose_methods(rng, channel, week8)

    # bank: NULL for wallet by design
    bank_ids = np.array([b[0] for b in BANKS])
    bank_w = np.array([b[3] for b in BANKS])
    bank = rng.choice(bank_ids, size=n, p=bank_w / bank_w.sum()).astype(object)
    bank[method == "WALLET"] = None
    bank_offset = {b[0]: b[4] for b in BANKS}

    # card network + bin (restricted column)
    is_card = method == "CARD"
    network = np.full(n, None, dtype=object)
    network[is_card] = rng.choice(CARD_NETWORKS[0], size=is_card.sum(), p=CARD_NETWORKS[1])
    card_bin = np.full(n, None, dtype=object)
    for net, prefix in CARD_BINS.items():
        mask = network == net
        card_bin[mask] = [f"{prefix}{x:05d}" for x in rng.integers(0, 99999, size=mask.sum())]

    # success probability
    p_success = np.array([SUCCESS_RATES[(mt, ch)] for mt, ch in zip(method, channel, strict=True)])
    p_success += np.array([bank_offset.get(b, 0.0) if b else 0.0 for b in bank])
    if week8:
        p_success[(method == "UPI") & (channel == "ONLINE")] = WEEK8_UPI_ONLINE_RATE
    outage = np.zeros(n, dtype=bool)
    if day in OUTAGE_DAYS:
        outage = (bank == OUTAGE_BANK_ID) & (method == "NETBANKING")
        p_success[outage] = OUTAGE_SUCCESS_RATE

    r_cancel = rng.random(n)
    r_success = rng.random(n)
    status = np.where(
        r_cancel < CANCEL_RATE, "USER_CANCELLED",
        np.where(r_success < p_success, "SUCCESS", "FAILED"),
    )

    error_code = np.full(n, None, dtype=object)
    for mt, (codes, probs) in FAIL_CODES.items():
        mask = (status == "FAILED") & (method == mt)
        error_code[mask] = rng.choice(codes, size=mask.sum(), p=probs)
    error_code[(status == "FAILED") & outage] = "ISSUER_DOWN"
    error_code[status == "USER_CANCELLED"] = "USER_CANCELLED"

    # amounts
    median_ticket = m["category"].map(lambda c: CATEGORIES[c][1]).to_numpy()
    amount = np.round(median_ticket * rng.lognormal(0, 0.6, size=n), 2)

    # timestamps
    hours = rng.choice(24, size=n, p=HOUR_WEIGHTS)
    seconds = rng.integers(0, 3600, size=n)
    base = datetime.combine(day, datetime.min.time())
    created_at = [base + timedelta(hours=int(h), seconds=int(s))
                  for h, s in zip(hours, seconds, strict=True)]

    return pd.DataFrame(
        {
            "txn_id": [f"T{start_seq + i:09d}" for i in range(n)],
            "created_at": created_at,
            "merchant_id": m["merchant_id"].to_numpy(),
            "channel": channel,
            "payment_method": method,
            "bank_id": bank,
            "card_network": network,
            "card_bin": card_bin,
            "customer_id": [f"C{x:07d}" for x in rng.integers(0, 2_000_000, size=n)],
            "amount": amount,
            "status": status,
            "error_code": error_code,
        }
    )


def build_settlements(rng: np.random.Generator, txns: pd.DataFrame) -> pd.DataFrame:
    """Settlements are computed by a separate system from the *true* (clean) successful txns."""
    ok = txns[txns["status"] == "SUCCESS"].copy()
    ok["settlement_date"] = ok["created_at"].dt.date
    ok["fee"] = ok["amount"] * ok["payment_method"].map(MDR)
    s = (
        ok.groupby(["merchant_id", "settlement_date"])
        .agg(txn_count=("txn_id", "count"), gross_amount=("amount", "sum"), fees=("fee", "sum"))
        .reset_index()
    )
    s["gross_amount"] = s["gross_amount"].round(2)
    s["fees"] = s["fees"].round(2)

    # Plant mismatches in week 6 (settlement system missed some txns)
    week6 = s["settlement_date"].between(
        SETTLEMENT_MISMATCH_WEEK_START, SETTLEMENT_MISMATCH_WEEK_START + timedelta(days=6)
    )
    candidates = s[week6 & (s["txn_count"] >= 10)].index
    picked = rng.choice(candidates, size=SETTLEMENT_MISMATCH_COUNT, replace=False)
    factor = rng.uniform(0.70, 0.90, size=len(picked))
    s.loc[picked, "txn_count"] = (s.loc[picked, "txn_count"] * factor).round().astype(int)
    s.loc[picked, "gross_amount"] = (s.loc[picked, "gross_amount"] * factor).round(2)
    s.loc[picked, "fees"] = (s.loc[picked, "fees"] * factor).round(2)

    s["net_amount"] = (s["gross_amount"] - s["fees"]).round(2)
    s.insert(0, "settlement_id", [f"S{i:07d}" for i in range(1, len(s) + 1)])
    return s


def apply_data_quality_issues(rng: np.random.Generator, txns: pd.DataFrame) -> pd.DataFrame:
    df = txns.copy()
    day = df["created_at"].dt.date

    # Missing bank_id on non-wallet rows
    mask = day.isin(NULL_BANK_DAYS) & (df["payment_method"] != "WALLET")
    idx = df[mask].sample(frac=NULL_BANK_FRACTION, random_state=int(rng.integers(1e9))).index
    df.loc[idx, "bank_id"] = None

    # Duplicates on one day (ingestion replay)
    dup = df[day == DUPLICATE_DAY].sample(
        frac=DUPLICATE_FRACTION, random_state=int(rng.integers(1e9))
    )
    df = pd.concat([df, dup], ignore_index=True)

    # Partial last day
    df = df[df["created_at"] < LOADED_THROUGH]
    return df.sort_values(["created_at", "txn_id"]).reset_index(drop=True)


def generate(seed: int = 42) -> GeneratedData:
    rng = np.random.default_rng(seed)
    banks = build_banks()
    merchants = build_merchants(rng)

    frames, seq = [], 1
    for d in range(NUM_DAYS):
        day = START_DATE + timedelta(days=d)
        frame = build_day(rng, day, merchants, seq)
        seq += len(frame)
        frames.append(frame)
    clean = pd.concat(frames, ignore_index=True)

    settlements = build_settlements(rng, clean[clean["created_at"] < LOADED_THROUGH])
    transactions = apply_data_quality_issues(rng, clean)

    freshness = pd.DataFrame(
        [("transactions", LOADED_THROUGH), ("settlements", LOADED_THROUGH)],
        columns=["table_name", "loaded_through"],
    )
    return GeneratedData(
        banks=banks,
        merchants=merchants.drop(columns="_volume_weight"),
        transactions=transactions,
        settlements=settlements,
        data_freshness=freshness,
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
DDL = """
CREATE TABLE banks (
    bank_id     VARCHAR PRIMARY KEY,
    bank_name   VARCHAR NOT NULL,
    bank_type   VARCHAR NOT NULL
);
CREATE TABLE merchants (
    merchant_id   VARCHAR PRIMARY KEY,
    merchant_name VARCHAR NOT NULL,
    category      VARCHAR NOT NULL,
    city          VARCHAR NOT NULL,
    region        VARCHAR NOT NULL,
    tier          VARCHAR NOT NULL,
    onboarded_on  DATE NOT NULL
);
-- No PRIMARY KEY on txn_id on purpose: the raw feed can contain replayed duplicates.
CREATE TABLE transactions (
    txn_id         VARCHAR NOT NULL,
    created_at     TIMESTAMP NOT NULL,
    merchant_id    VARCHAR NOT NULL,
    channel        VARCHAR NOT NULL,
    payment_method VARCHAR NOT NULL,
    bank_id        VARCHAR,
    card_network   VARCHAR,
    card_bin       VARCHAR,
    customer_id    VARCHAR NOT NULL,
    amount         DECIMAL(12, 2) NOT NULL,
    status         VARCHAR NOT NULL,
    error_code     VARCHAR
);
CREATE TABLE settlements (
    settlement_id   VARCHAR PRIMARY KEY,
    merchant_id     VARCHAR NOT NULL,
    settlement_date DATE NOT NULL,
    txn_count       INTEGER NOT NULL,
    gross_amount    DECIMAL(14, 2) NOT NULL,
    fees            DECIMAL(14, 2) NOT NULL,
    net_amount      DECIMAL(14, 2) NOT NULL
);
CREATE TABLE data_freshness (
    table_name     VARCHAR PRIMARY KEY,
    loaded_through TIMESTAMP NOT NULL
);
"""


def write_duckdb(data: GeneratedData, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = duckdb.connect(str(path))
    try:
        con.execute(DDL)
        for name in ("banks", "merchants", "transactions", "settlements", "data_freshness"):
            df = getattr(data, name)  # noqa: F841 - referenced by DuckDB below
            con.execute(f"INSERT INTO {name} SELECT * FROM df")
        con.execute("CREATE INDEX idx_txn_created ON transactions(created_at)")
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/payments.duckdb")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    data = generate(args.seed)
    write_duckdb(data, Path(args.out))
    print(f"Wrote {args.out}")
    for name in ("banks", "merchants", "transactions", "settlements"):
        print(f"  {name:<13} {len(getattr(data, name)):>8,} rows")


if __name__ == "__main__":
    main()
