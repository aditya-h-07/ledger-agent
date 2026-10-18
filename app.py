
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urljoin
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


def parse_time(value: str) -> datetime:
    """Parse an ISO timestamp and normalize it to India time."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    return dt.astimezone(TZ)


def deduplicate_orders(orders: list[dict]) -> list[dict]:
    """Keep the latest updated version of each order ID."""
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


def load_orders() -> list[dict]:
    """
    Load ledger.jsonl locally, or fetch orders from the API on Render.
    """
    local_path = Path(__file__).with_name("ledger.jsonl")

    if local_path.exists():
        orders = []

        with local_path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    orders.append(json.loads(line))

        return deduplicate_orders(orders)

    root_url = os.environ.get("LEDGER_ROOT_URL", "").strip()

    if not root_url:
        raise RuntimeError(
            "Set LEDGER_ROOT_URL when ledger.jsonl is not present."
        )

    session = requests.Session()

    response = session.get(root_url, timeout=20)
    response.raise_for_status()

    links = response.json().get("links", {})
    export_url = links.get("export")

    if not export_url:
        raise RuntimeError(
            "The ledger API did not provide an export link."
        )

    export_response = session.get(
        urljoin(root_url, export_url),
        timeout=60,
    )
    export_response.raise_for_status()

    orders = [
        json.loads(line)
        for line in export_response.text.splitlines()
        if line.strip()
    ]

    # Fall back to pagination if the export is unexpectedly incomplete.
    if len(orders) < 100:
        orders = []
        next_url = links.get("orders")

        if not next_url:
            raise RuntimeError(
                "The ledger API did not provide an orders link."
            )

        next_url = urljoin(root_url, next_url)

        while next_url:
            page_response = session.get(next_url, timeout=30)
            page_response.raise_for_status()

            page = page_response.json()
            orders.extend(page.get("orders", []))

            following = page.get("next")
            next_url = (
                urljoin(next_url, following)
                if following
                else None
            )

    if not orders:
        raise RuntimeError("The ledger API returned no orders.")

    return deduplicate_orders(orders)


# Load the ledger once when the application starts.
ORDERS = load_orders()


def amount_usd(order: dict) -> Decimal:
    """Convert an order's amount to USD using the configured rates."""
    currency = str(order.get("currency", "USD")).upper()

    if currency not in RATES:
        raise ValueError(f"Unknown currency: {currency}")

    amount = Decimal(str(order.get("amount", 0)))
    return amount * RATES[currency]


def money(value: Decimal) -> float:
    """Round a monetary value to two decimal places."""
    return float(
        value.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def extract_filters(question: str):
    q = question.lower()

    start = None
    end = None

    # Match a month and year, such as February 2026.
    for month_name, month_number in MONTHS.items():
        match = re.search(
            rf"\b{month_name}\s+((?:19|20)\d{{2}})\b",
            q,
        )

        if match:
            year = int(match.group(1))

            start = datetime(
                year, month_number, 1, tzinfo=TZ
            )

            if month_number == 12:
                end = datetime(year + 1, 1, 1, tzinfo=TZ)
            else:
                end = datetime(
                    year, month_number + 1, 1, tzinfo=TZ
                )

            break

    # Match a year alone if no month/year was found.
    if start is None:
        match = re.search(r"\b((?:19|20)\d{2})\b", q)

        if match:
            year = int(match.group(1))
            start = datetime(year, 1, 1, tzinfo=TZ)
            end = datetime(year + 1, 1, 1, tzinfo=TZ)

    customer_match = re.search(
        r"\bC\d{4}\b",
        question,
        re.IGNORECASE,
    )

    customer = (
        customer_match.group(0).upper()
        if customer_match
        else None
    )

    product = next(
        (
            item for item in PRODUCTS
            if re.search(rf"\b{re.escape(item.lower())}\b", q)
        ),
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


def answer_question(question: str):
    q = question.lower().strip()

    start, end, customer, product, region = extract_filters(question)
    rows = list(ORDERS)

    # Apply customer filter.
    if customer:
        rows = [
            order for order in rows
            if str(order.get("customer", "")).upper() == customer
        ]

    # Apply product filter.
    if product:
        rows = [
            order for order in rows
            if str(order.get("product", "")).lower()
            == product.lower()
        ]

    # Apply region filter.
    if region:
        rows = [
            order for order in rows
            if str(order.get("region", "")).lower()
            == region.lower()
        ]

    # Apply date filter using Asia/Kolkata time.
    if start or end:
        filtered_rows = []

        for order in rows:
            order_time = parse_time(order["created_at"])

            if start and order_time < start:
                continue

            if end and order_time >= end:
                continue

            filtered_rows.append(order)

        rows = filtered_rows

    # Identify the metric requested by the question.
    is_refund = any(
        word in q
        for word in ("refund", "refunded", "return", "returned")
    )

    asks_revenue = any(
        word in q
        for word in (
            "revenue",
            "sales",
            "sold",
            "earned",
            "earn",
            "income",
        )
    )

    # Explicit paid-order wording must also filter the records.
    asks_paid_orders = (
        "paid" in q
        and re.search(r"\border(s)?\b", q) is not None
    )

    # Apply status filtering BEFORE calculating any metric.
    if is_refund:
        rows = [
            order for order in rows
            if order.get("status") == "refunded"
        ]
    elif asks_revenue or asks_paid_orders:
        rows = [
            order for order in rows
            if order.get("status") == "paid"
        ]

    # Average must be handled BEFORE the "how many" count branch.
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

    # Count distinct customers only once each.
    if any(
        phrase in q
        for phrase in ("how many", "count", "number of")
    ):
        if (
            "customer" in q
            and any(word in q for word in ("unique", "distinct","different"))
        ):
            customers = {
                str(order["customer"])
                for order in rows
                if order.get("customer") is not None
            }

            return len(customers)

        return len(rows)

    # Calculate quantities/units separately from monetary totals.
    if (
        "quantity" in q
        or re.search(r"\bunits?\b", q)
    ):
        return sum(
            int(order.get("qty", 0))
            for order in rows
        )

    # Identify top product/customer questions.
    group_field = None

    if re.search(r"\b(which|what)\s+(product|item)s?\b", q):
        group_field = "product"

    elif re.search(r"\b(which|what)\s+customers?\b", q):
        group_field = "customer"

    elif re.search(
        r"\b(top|highest|most|best|largest|greatest)\b",
        q,
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
                eligible = [
                    item for item in items
                    if item.get("status") == "paid"
                ]

                return Decimal(
                    sum(int(item.get("qty", 0)) for item in eligible)
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

    # Default monetary questions return the USD total.
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
        return {
            "answer": answer_question(request.question)
        }
    except (ValueError, KeyError, ArithmeticError) as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )
