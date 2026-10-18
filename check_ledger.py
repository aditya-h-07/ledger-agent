import json
from decimal import Decimal
from pathlib import Path

RATES = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.07"),
    "INR": Decimal("0.01203"),
}

with Path("ledger.jsonl").open() as f:
    orders = [json.loads(line) for line in f if line.strip()]

def usd(o):
    return Decimal(str(o.get("amount", 0))) * RATES.get(
        o.get("currency", "USD"), Decimal("1")
    )

paid = [o for o in orders if o.get("status") == "paid"]
east = [o for o in paid if o.get("region") == "East"]
kettle = [o for o in paid if o.get("product", "").lower() == "kettle"]
south_feb = [
    o for o in paid
    if o.get("region") == "South"
    and o.get("created_at", "")[:7] == "2026-02"
]

print("Total records:", len(orders))
print("Paid East orders:", len(east))
print("Average paid East order (USD):",
      round(sum((usd(o) for o in east), Decimal("0")) / len(east), 2)
      if east else "No matching orders")
print("Distinct customers with paid Kettle orders:",
      len({o["customer"] for o in kettle if o.get("customer") is not None}))
print("Paid South orders in February 2026:", len(south_feb))
print("South February 2026 revenue (USD):",
      round(sum((usd(o) for o in south_feb), Decimal("0")), 2))
