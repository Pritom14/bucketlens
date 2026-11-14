"""Make a large sales workbook (synthetic data) laid out the way Excel saves files:
shared strings, and a <dimension> tag at the top of every sheet."""

import datetime as dt
import random
import sys

import xlsxwriter

out = sys.argv[1]
rows = int(sys.argv[2]) if len(sys.argv) > 2 else 300_000
random.seed(7)
wb = xlsxwriter.Workbook(out, {"strings_to_numbers": False, "constant_memory": True})
datefmt = wb.add_format({"num_format": "yyyy-mm-dd"})
regions = ["North", "South", "East", "West", "Central"]
products = [f"SKU-{i:04d}" for i in range(1, 801)]
customers = [f"Customer {i:05d}" for i in range(1, 20001)]
ws = wb.add_worksheet("Orders")
ws.write_row(
    0,
    0,
    [
        "order_id",
        "order_date",
        "region",
        "customer",
        "product",
        "qty",
        "unit_price",
        "discount",
        "net_amount",
        "status",
        "sales_rep",
        "notes",
    ],
)
d0 = dt.date(2023, 1, 1)
for i in range(1, rows + 1):
    q = random.randint(1, 50)
    p = round(random.uniform(5, 900), 2)
    disc = random.choice([0, 0, 0.05, 0.1, 0.15])
    ws.write_row(i, 0, [f"ORD-{i:07d}"])
    ws.write_datetime(i, 1, dt.datetime.combine(d0 + dt.timedelta(days=random.randint(0, 1000)), dt.time()), datefmt)
    ws.write_row(
        i,
        2,
        [
            random.choice(regions),
            random.choice(customers),
            random.choice(products),
            q,
            p,
            disc,
            round(q * p * (1 - disc), 2),
            random.choice(["shipped", "pending", "returned", "cancelled"]),
            f"Rep {random.randint(1, 120):03d}",
            random.choice(["", "", "", "rush", "gift wrap", "call before delivery"]),
        ],
    )
ws2 = wb.add_worksheet("Products")
ws2.write_row(0, 0, ["product", "category", "list_price", "launched"])
for j, s in enumerate(products, start=1):
    ws2.write_row(
        j, 0, [s, random.choice(["Hardware", "Software", "Services", "Accessories"]), round(random.uniform(5, 900), 2)]
    )
    ws2.write_datetime(j, 3, dt.datetime.combine(d0 - dt.timedelta(days=random.randint(0, 2000)), dt.time()), datefmt)
ws3 = wb.add_worksheet("Summary")
ws3.write_row(0, 0, ["region", "note"])
for k, r in enumerate(regions, start=1):
    ws3.write_row(k, 0, [r, f"Quarterly targets for {r} are in the Orders sheet."])
wb.close()
