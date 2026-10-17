
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Acme Ledger Agent")
TZ = ZoneInfo("Asia/Kolkata")

RATES = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.07"),
    "INR": Decimal("0.01203"),
}

PRODUCTS = [
    "Air Fryer",
    "Blender",
    "Grinder",
    "Juicer",
    "Kettle",
    "Mixer",
    "Rice Cooker",
    "Toaster",
]

REGIONS = ["Central", "East", "North", "South", "West"]

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        [
            "January", "February", "March", "April",
            "May", "June", "July", "August",
            "September", "October", "November", "December",
        ],
        start=1,
    )
}


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def parse_time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def deduplicate_orders(orders):
    latest = {}

    for order in orders:
        order_id = str(order["id"])
        current = latest.get(order_id)

        if (
            current is None
            or parse_time(order["updated_at"])
            > parse_time(current["updated_at"])
        ):
            latest[order_id] = order

    return list(latest.values())


def load_orders():
    local_path = Path(__file__).with_name("ledger.jsonl")

    # Use the local dataset during development.
    if local_path.exists():
        orders = []

        with local_path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    orders.append(json.loads(line))

        return deduplicate_orders(orders)

    # On Render, fetch data using an environment variable.
    root_url = os.environ.get("LEDGER_ROOT_URL")

    if not root_url:
        raise RuntimeError(
            "ledger.jsonl was not found. Set LEDGER_ROOT_URL."
        )

    session = requests.Session()

    response = session.get(root_url, timeout=20)
    response.raise_for_status()
    links = response.json().get("links", {})

    export_url = links.get("export")

    if not export_url:
        raise RuntimeError("The API did not provide an export link.")

    response = session.get(export_url, timeout=60)
    response.raise_for_status()

    orders = [
        json.loads(line)
        for line in response.text.splitlines()
        if line.strip()
    ]

    # Fall back to pagination if export data is unexpectedly incomplete.
    if len(orders) < 100:
        orders = []
        next_url = links.get("orders")

        if not next_url:
            raise RuntimeError("The API did not provide an orders link.")

        while next_url:
            response = session.get(next_url, timeout=30)
            response.raise_for_status()

            page = response.json()
            orders.extend(page.get("orders", []))
            next_url = page.get("next")

    if not orders:
        raise RuntimeError("The API returned no order records.")

    return deduplicate_orders(orders)


# Initialize the data before the endpoints use ORDERS.
ORDERS = load_orders()


def amount_usd(order):
    currency = str(order.get("currency", "USD")).upper()

    if currency not in RATES:
        raise ValueError(f"Unknown currency: {currency}")

    amount = Decimal(str(order.get("amount", 0)))
    return amount * RATES[currency]


def money(value):
    return float(
        value.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def extract_filters(question):
    q = question.lower()
    start = None
    end = None

    # Match month and year, for example March 2026.
    for month_name, month_number in MONTHS.items():
        match = re.search(
            rf"\b{month_name}\s+((?:19|20)\d{{2}})\b",
            q,
        )

        if match:
            year = int(match.group(1))
            start = datetime(year, month_number, 1, tzinfo=TZ)

            if month_number == 12:
                end = datetime(year + 1, 1, 1, tzinfo=TZ)
            else:
                end = datetime(
                    year, month_number + 1, 1, tzinfo=TZ
                )

            break

    # Match a year without a month.
    if start is None:
        match = re.search(r"\b((?:19|20)\d{2})\b", q)

        if match:
            year = int(match.group(1))
            start = datetime(year, 1, 1, tzinfo=TZ)
            end = datetime(year + 1, 1, 1, tzinfo=TZ)

    customer_match = re.search(
        r"\bC\d{4}\b", question, re.IGNORECASE
    )
    customer = (
        customer_match.group(0).upper()
        if customer_match else None
    )

    product = next(
        (item for item in PRODUCTS if item.lower() in q),
        None,
    )

    region = next(
        (
            item for item in REGIONS
            if re.search(rf"\b{re.escape(item.lower())}\b", q)
        ),
        None,
    )

    return start, end, customer, product, region


def answer_question(question):
    q = question.lower().strip()

    start, end, customer, product, region = extract_filters(question)
    rows = list(ORDERS)

    if customer:
        rows = [
            order for order in rows
            if str(order.get("customer", "")).upper() == customer
        ]

    if product:
        rows = [
            order for order in rows
            if str(order.get("product", "")).lower() == product.lower()
        ]

    if region:
        rows = [
            order for order in rows
            if str(order.get("region", "")).lower() == region.lower()
        ]

    if start or end:
        filtered = []

        for order in rows:
            order_time = parse_time(order["created_at"])

            if start and order_time < start:
                continue

            if end and order_time >= end:
                continue

            filtered.append(order)

        rows = filtered

    is_refund = any(
        word in q
        for word in ("refund", "refunded", "return", "returned")
    )

    asks_revenue = any(
        word in q
        for word in ("revenue", "sales", "sold", "earned", "income")
    )

    if is_refund:
        rows = [
            order for order in rows
            if order.get("status") == "refunded"
        ]
    elif asks_revenue:
        rows = [
            order for order in rows
            if order.get("status") == "paid"
        ]

    # Identify questions asking for a leading product or customer.
    group_field = None

    if re.search(r"\b(which|what)\s+(product|item)s?\b", q):
        group_field = "product"

    elif re.search(r"\b(which|what)\s+customers?\b", q):
        group_field = "customer"

    elif re.search(
        r"\b(top|highest|most|best|largest|greatest)\b", q
    ):
        if "product" in q or "item" in q:
            group_field = "product"
        elif "customer" in q or "client" in q:
            group_field = "customer"

    if group_field:
        groups = defaultdict(list)

        for order in rows:
            key = str(order.get(group_field, "Unknown"))
            groups[key].append(order)

        if not groups:
            return None

        def score(items):
            if (
                "count" in q
                or "number of orders" in q
                or "most orders" in q
            ):
                return Decimal(len(items))

            if "unit" in q or "quantity" in q:
                return Decimal(
                    sum(int(item.get("qty", 0)) for item in items)
                )

            return sum(
                (
                    amount_usd(item)
                    for item in items
                    if item.get("status") == "paid"
                ),
                Decimal("0"),
            )

        ranked = sorted(
            groups.items(),
            key=lambda pair: (
                score(pair[1]),
                pair[0].lower(),
            ),
            reverse=True,
        )

        return ranked[0][0]

    # Count orders or distinct customers.
    if any(
        phrase in q
        for phrase in ("how many", "count", "number of")
    ):
        if "customer" in q and (
            "unique" in q or "distinct" in q
        ):
            return len({order.get("customer") for order in rows})

        return len(rows)

    # Sum the number of units.
    if "quantity" in q or re.search(r"\bunits?\b", q):
        return sum(int(order.get("qty", 0)) for order in rows)

    # Average paid order value.
    if "average" in q or "mean" in q:
        eligible = [
            order for order in rows
            if order.get("status") == "paid"
        ]

        if not eligible:
            return 0

        total = sum(
            (amount_usd(order) for order in eligible),
            Decimal("0"),
        )

        return money(total / len(eligible))

    # Default: total amount in USD for the selected rows.
    total = sum(
        (amount_usd(order) for order in rows),
        Decimal("0"),
    )

    return money(total)


@app.get("/")
def home():
    return {
        "service": "Acme Ledger Agent",
        "orders_loaded": len(ORDERS),
        "message": "Send POST / with JSON containing a question.",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "orders_loaded": len(ORDERS),
    }


@app.post("/")
def ask(request: Question):
    try:
        answer = answer_question(request.question)
        return {"answer": answer}

    except (ValueError, KeyError, ArithmeticError) as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )
