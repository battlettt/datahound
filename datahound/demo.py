"""Built-in sample: an orders table with planted problems, plus a clean baseline.

The first --demo run against a store also records the clean baseline as an
earlier run, so drift flags and the trend view have something to show at once.
Everything is generated from fixed seeds, so the numbers are the same every time.
"""

from __future__ import annotations

import datetime as dt
import random

from .loader import Table

DATASET = "demo:orders"
COLUMNS = [
    "order_id",
    "customer_email",
    "customer_name",
    "country",
    "status",
    "amount",
    "quantity",
    "order_date",
    "coupon_code",
]
COUNTRIES = ["US", "CA", "GB", "DE", "FR", "MX"]
STATUSES = ["paid", "pending", "refunded", "shipped"]
NAMES = ["Ada", "Grace", "Alan", "Edsger", "Barbara", "Ken", "Margaret", "Linus", "Frances", "Dennis"]

DEMO_RULES = {
    "primary_key": "order_id",
    "defaults": {"max_null_rate": 0.05},
    "columns": {
        "order_id": {"type": "integer", "required": True},
        "customer_email": {"pattern": r"[^@\s]+@[^@\s]+\.[a-z]+"},
        "customer_name": {"required": True},
        "country": {"code": True, "case": "upper", "allowed": COUNTRIES},
        "status": {"allowed": STATUSES},
        "amount": {"type": "number", "min": 0, "max": 5000},
        "quantity": {"type": "integer", "min": 1, "max": 50},
        "order_date": {"type": "date", "format": "%Y-%m-%d", "min": "2026-01-01", "max": "2026-12-31"},
        "coupon_code": {"max_null_rate": 1.0},
    },
}

# What --demo plants, so the README and tests can point at exact counts.
PLANTED = {
    "customer_email_nulls": 30,
    "customer_name_blank": 10,
    "country_lowercase": 9,
    "country_mixedcase": 3,
    "status_unexpected": 9,
    "amount_out_of_range": 5,
    "amount_non_numeric": 2,
    "order_date_unparseable": 6,
    "order_id_reused": 4,
    "exact_duplicate_rows": 5,
}


def _row(rng: random.Random, number: int, amount_high: float) -> list[str | None]:
    day = dt.date(2026, 1, 1) + dt.timedelta(days=rng.randint(0, 300))
    return [
        str(1000 + number),
        f"user{rng.randint(1, 900)}@example.com",
        rng.choice(NAMES),
        rng.choice(COUNTRIES),
        rng.choice(STATUSES),
        f"{rng.uniform(5, amount_high):.2f}",
        str(rng.randint(1, 6)),
        day.isoformat(),
        "SAVE10" if rng.random() < 0.2 else None,
    ]


def clean_table() -> Table:
    rng = random.Random(4)
    rows = [_row(rng, i, 300) for i in range(200)]
    return Table("orders (demo baseline)", "built-in demo dataset, clean baseline", DATASET, "demo", list(COLUMNS), rows)


def messy_table() -> Table:
    rng = random.Random(27)
    rows = [_row(rng, i, 400) for i in range(240)]
    picks = iter(rng.sample(range(10, 240), 120))  # distinct rows to damage, never rows 0-9

    def plant(column: str, values: list[str | None]) -> None:
        index = COLUMNS.index(column)
        for value in values:
            rows[next(picks)][index] = value

    plant("customer_email", [None] * PLANTED["customer_email_nulls"])
    plant("customer_name", [""] * 8 + ["  "] * 2)
    plant("country", ["us"] * PLANTED["country_lowercase"] + ["Ca"] * PLANTED["country_mixedcase"])
    plant("status", ["PAID"] * 4 + ["shipped?"] * 3 + ["cancelled"] * 2)
    plant("amount", ["-25.00", "-3.10", "-80.00", "99999.00", "12500.00"])
    plant("amount", ["12,50", "abc"])
    plant("order_date", ["2026-02-30", "2026-13-01", "31/12/2026", "soon", "2026-06-31", "2026/05/04"])
    id_index = COLUMNS.index("order_id")
    for source in range(PLANTED["order_id_reused"]):
        rows[next(picks)][id_index] = rows[source][id_index]
    for source in rng.sample(range(240), PLANTED["exact_duplicate_rows"]):
        rows.append(list(rows[source]))
    return Table("orders (demo)", "built-in demo dataset", DATASET, "demo", list(COLUMNS), rows)
