"""
Emergency contacts, from the New World Scheduler `Emergency-Contacts.csv` export.

Only the name, relationship, email, mobile and phone of each contact are kept,
in the export's order. The export's Notes column is **never stored**: it is
dropped before the file is written to disk.

The export identifies the person a contact belongs to by `PublisherID`, which
is mostly a different numbering from `Persons.csv`'s `PersonID`. So each
publisher is linked to a person by:

1. `PublisherID` when it is a PersonID **and** the names agree, else
2. first + last name, when exactly one person has that name.

Anyone left over (no such name, or two people sharing it) is reported, not
guessed - see `link()`. The export sometimes lists one person under two
PublisherIDs with the same contacts under each; exact repeats are kept once.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict

#: The columns kept from the export, in this order. Notes is deliberately absent.
KEEP = ["PublisherID", "PublisherFirstName", "PublisherLastName", "Order",
        "Name", "Relationship", "Email", "Mobile", "Phone"]
REQUIRED = {"PublisherID", "PublisherFirstName", "PublisherLastName", "Order", "Name"}
PERSONS_REQUIRED = {"PersonID", "FirstName", "LastName"}
FILENAME = "Emergency-Contacts.csv"


def _text(data: bytes) -> str:
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def headers(data: bytes) -> set[str]:
    first = _text(data).splitlines()[:1]
    return {h.strip() for h in next(csv.reader(first), [])} if first else set()


def looks_like_persons(data: bytes) -> bool:
    return PERSONS_REQUIRED <= headers(data)


def looks_like_contacts(data: bytes) -> bool:
    return REQUIRED <= headers(data)


def sanitize(data: bytes) -> tuple[bytes, int]:
    """The export with only the kept columns - Notes and anything else dropped."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=KEEP, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    rows = 0
    for row in csv.DictReader(io.StringIO(_text(data))):
        writer.writerow({k: (row.get(k) or "").strip() for k in KEEP})
        rows += 1
    return out.getvalue().encode("utf-8"), rows


def _key(first: str, last: str) -> tuple[str, str]:
    return (" ".join((first or "").lower().split()), " ".join((last or "").lower().split()))


def link(persons_csv: bytes, contacts_csv: bytes) -> tuple[list[dict], list[dict]]:
    """Contacts with the PersonID they belong to, and the publishers that could not be linked.

    Returns (linked, unlinked). Each linked contact has person_id, contact_order,
    name, relationship, email, mobile, phone; unlinked entries name the
    publisher, how many contacts are waiting, and why.
    """
    by_id: dict[str, tuple[str, str]] = {}
    by_name: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in csv.DictReader(io.StringIO(_text(persons_csv))):
        pid = (row.get("PersonID") or "").strip()
        if not pid.isdigit():
            continue
        name = _key(row.get("FirstName", ""), row.get("LastName", ""))
        by_id[pid] = name
        by_name[name].append(int(pid))

    linked: list[dict] = []
    waiting: dict[str, dict] = {}
    resolved: dict[str, int | None] = {}
    for row in csv.DictReader(io.StringIO(_text(contacts_csv))):
        publisher = (row.get("PublisherID") or "").strip()
        name = _key(row.get("PublisherFirstName", ""), row.get("PublisherLastName", ""))
        if publisher not in resolved:
            if publisher in by_id and by_id[publisher] == name:
                resolved[publisher] = int(publisher)
            elif len(by_name.get(name, [])) == 1:
                resolved[publisher] = by_name[name][0]
            else:
                resolved[publisher] = None
                waiting[publisher] = {
                    "publisher": f"{row.get('PublisherFirstName', '').strip()} "
                                 f"{row.get('PublisherLastName', '').strip()}".strip(),
                    "contacts": 0,
                    "reason": ("two or more people share this name"
                               if len(by_name.get(name, [])) > 1 else "no person with this name"),
                }
        person_id = resolved[publisher]
        if person_id is None:
            waiting[publisher]["contacts"] += 1
            continue
        try:
            order = int((row.get("Order") or "0").strip() or 0)
        except ValueError:
            order = 0
        linked.append({
            "person_id": person_id,
            "contact_order": order,
            "name": (row.get("Name") or "").strip(),
            "relationship": (row.get("Relationship") or "").strip(),
            "email": (row.get("Email") or "").strip(),
            "mobile": (row.get("Mobile") or "").strip(),
            "phone": (row.get("Phone") or "").strip(),
        })
    linked.sort(key=lambda c: (c["person_id"], c["contact_order"], c["name"].lower()))
    # The export can list one person under two PublisherIDs with the same contacts under
    # each; keep the first of any exact repeat.
    seen, unique = set(), []
    for c in linked:
        key = (c["person_id"], c["name"].lower(), c["relationship"].lower(), c["email"].lower(),
               c["mobile"], c["phone"])
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique, sorted(waiting.values(), key=lambda w: w["publisher"].lower())
