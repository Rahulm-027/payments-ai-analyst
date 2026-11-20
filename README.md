# Payments AI Business Analyst

> Ask questions about payment performance in plain English and get answers backed by evidence:
> the executed SQL, a segment-level breakdown, charts, and explicit caveats.

🚧 **Work in progress.** Day 1 is done: synthetic data, semantic layer and access-control config.

## Quickstart (current state)

```bash
pip install -e ".[dev]"
make data     # builds data/payments.duckdb (~350K transactions, 8 weeks)
make test
```

## Data model

| Table | Grain | Notes |
|---|---|---|
| `transactions` | one payment attempt | raw feed - may contain replayed duplicates |
| `merchants` | one merchant | category, city, region, tier |
| `banks` | one issuing bank | fictional names |
| `settlements` | merchant x day | produced by a separate settlement system |
| `data_freshness` | one table | how far each table is loaded |

## Planted scenarios (answer key)

The generator plants these problems on purpose. The analyst is expected to find and explain them.

| # | Scenario | What a good answer says |
|---|---|---|
| 1 | **UPI mix shift** in week 8 (2026-11-09) | Success rate fell ~2pp, mostly because volume moved to UPI, which succeeds less often. Most per-method rates are flat. |
| 2 | **UPI online dip** in week 8 | A smaller, real rate drop in UPI on the online channel. |
| 3 | **Coastal Bank netbanking outage** (2026-10-21/22) | Success rate ~35%, `ISSUER_DOWN` errors. |
| 4 | **Duplicate rows** on 2026-11-04 | ~8% replayed rows; metrics must deduplicate. |
| 5 | **Missing bank_id** on 2026-10-07/08 | ~6% of non-wallet rows; wallet NULLs are expected. |
| 6 | **Partial last day** (2026-11-15 loaded to 09:00) | Week 8 volume looks lower - that's a loading artefact, not a real drop. |
| 7 | **Settlement mismatch** (week of 2026-10-26) | 12 merchant-days where settlements disagree with transactions. |

## Metric definitions

All metrics live in [`config/metrics.yaml`](config/metrics.yaml). Example: **success rate** = successful / attempts excluding `USER_CANCELLED`, computed on deduplicated transactions.
