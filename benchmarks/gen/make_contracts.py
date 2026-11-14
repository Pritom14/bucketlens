"""80 synthetic vendor contracts (3-4 pages each) plus the facts needed to grade answers."""

import datetime as dt
import json
import random
import sys
from pathlib import Path

from fpdf import FPDF

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
random.seed(11)
first = ["Arjun", "Meera", "Kabir", "Isha", "Rohan", "Tara", "Vikram", "Ananya", "Dev", "Nisha"]
kinds = [
    "Logistics",
    "Cloud Hosting",
    "Facility Services",
    "Payroll Processing",
    "Security Services",
    "Catering",
    "Printing",
    "Legal Advisory",
    "Data Labelling",
    "Telecom",
    "Office Supplies",
    "Recruitment",
]
cities = ["Bengaluru", "Mumbai", "New Delhi", "Chennai", "Hyderabad", "Pune"]
words = (
    "the parties agree that services shall be delivered in accordance with the service levels set out in this "
    "agreement and any purchase order issued under it, and that each party shall act in good faith, keep "
    "confidential information secret, comply with applicable law, maintain insurance, and cooperate on audits"
).split()
facts = []
used = set()
for i in range(1, 81):
    while True:
        vendor = f"{random.choice(['Apex', 'Blue River', 'Cedar', 'Delta', 'Evergreen', 'Falcon', 'Granite', 'Harbor', 'Indigo', 'Juniper', 'Kestrel', 'Lotus', 'Monsoon', 'Northstar', 'Orchid', 'Peak'])} {random.choice(kinds)} {random.choice(['Pvt Ltd', 'LLP', 'Ltd'])}"
        if vendor not in used:
            used.add(vendor)
            break
    start = dt.date(2024, 1, 1) + dt.timedelta(days=random.randint(0, 600))
    term = random.choice([12, 18, 24, 36])
    end = (start.replace(day=1) + dt.timedelta(days=31 * term)).replace(day=1) - dt.timedelta(days=1)
    value = random.randint(3, 480) * 25_000
    auto = random.random() < 0.35
    notice = random.choice([30, 45, 60, 90])
    city = random.choice(cities)
    cid = f"CTR-2024-{i:03d}"
    facts.append(
        {
            "file": f"contract-{i:03d}.pdf",
            "id": cid,
            "vendor": vendor,
            "start": str(start),
            "end": str(end),
            "annual_value_inr": value,
            "auto_renewal": auto,
            "notice_days": notice,
            "law_city": city,
        }
    )
    pdf = FPDF()
    pdf.set_auto_page_break(True, 18)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 10, "MASTER SERVICES AGREEMENT", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 10.5)
    pdf.multi_cell(
        0,
        6,
        f"Contract ID: {cid}\nThis agreement is made between Northwind Retail Pvt Ltd (the Customer) and {vendor} (the Vendor).",
    )
    for n, title in enumerate(
        [
            "Definitions",
            "Scope of Services",
            "Service Levels",
            "Fees and Payment",
            "Term and Renewal",
            "Confidentiality",
            "Data Protection",
            "Liability",
            "Termination",
            "Governing Law",
            "General",
        ],
        start=1,
    ):
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, f"{n}. {title}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10.5)
        body = " ".join(random.choice(words) for _ in range(random.randint(90, 160))).capitalize() + "."
        if title == "Fees and Payment":
            body = (
                f"The annual contract value is INR {value:,}, payable quarterly in arrears within 30 days of invoice. "
                + body
            )
        if title == "Term and Renewal":
            body = (
                f"This agreement starts on {start:%d %B %Y} and ends on {end:%d %B %Y}. Automatic renewal: {'Yes' if auto else 'No'}. "
                + (
                    "It renews for successive one-year terms unless either party gives notice. "
                    if auto
                    else "Any extension needs a written amendment. "
                )
                + body
            )
        if title == "Termination":
            body = (
                f"Either party may terminate this agreement for convenience by giving {notice} days' written notice. "
                + body
            )
        if title == "Governing Law":
            body = (
                f"This agreement is governed by the laws of India, and the courts at {city} have exclusive jurisdiction. "
                + body
            )
        pdf.multi_cell(0, 5.5, body)
    pdf.ln(4)
    pdf.multi_cell(
        0,
        6,
        f"Signed for Northwind Retail Pvt Ltd: {random.choice(first)} (Director)\nSigned for {vendor}: {random.choice(first)} (Authorised Signatory)",
    )
    pdf.output(str(out / f"contract-{i:03d}.pdf"))
(out.parent / "contracts_facts.json").write_text(json.dumps(facts, indent=1))
top = max(facts, key=lambda f: f["annual_value_inr"])
print(
    "files:",
    len(facts),
    "| top value:",
    top["vendor"],
    top["annual_value_inr"],
    "| ties:",
    sum(f["annual_value_inr"] == top["annual_value_inr"] for f in facts),
)
print("auto-renew count:", sum(f["auto_renewal"] for f in facts))
print("ending Mar 2026:", [f["vendor"] for f in facts if f["end"].startswith("2026-03")])
print("90-day notice + Mumbai:", [f["vendor"] for f in facts if f["notice_days"] == 90 and f["law_city"] == "Mumbai"])
