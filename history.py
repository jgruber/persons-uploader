"""
Change history for the congregation's persons, from the nightly Persons.csv.

Every upload is compared with the last state recorded here, person by person
(PersonID), and two tables are kept in `$UPLOAD_DIR/.history/history.sqlite`:

* **person_versions** - the source of truth. One row per person per stretch of
  time in which the tracked fields stayed the same (`valid_from` .. `valid_to`;
  the open row has `valid_to` NULL). Answers "what did this person look like on
  a given day", and lets a new kind of change be derived retroactively.
* **person_changes** - one row per detected change, with the day it was seen,
  the day it took effect when the CSV says (DateOfBaptism, DateOfPrivilege,
  DateOfFirstMonth, DateOfRemoved), and the old and new values. Monthly
  summaries are a query over this table.

The very first import is a baseline and reports nothing. Changes, and versions
that ended, older than RETENTION_MONTHS are deleted on every import.

The history cannot be recreated from anywhere else - the nightly exports are
only archived for a week - so it is in the nightly encrypted backup
(`backup.py`).
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

RETENTION_MONTHS = 48

#: Field in the history -> column in Persons.csv
TRACKED = {
    "first_name": "FirstName",
    "last_name": "LastName",
    "display_name": "DisplayName",
    "privilege": "Privilege",
    "pioneer_status": "PioneerStatus",
    "active": "Active",
    "moved": "Moved",
    "removed": "Removed",
    "anointed": "Anointed",
    "group_id": "FieldServiceGroupID",
    "group_name": "GroupName",
    "address": "Address",
    "phone_mobile": "PhoneMobile",
    "phone_home": "PhoneHome",
    "phone_work": "PhoneWork",
    "date_of_baptism": "DateOfBaptism",
    "date_of_privilege": "DateOfPrivilege",
    "date_of_first_month": "DateOfFirstMonth",
    "date_of_removed": "DateOfRemoved",
}
FIELDS = list(TRACKED)

BAPTIZED = {"E", "MS", "PUB"}

#: change_type -> (label, the heading it is reported under)
CHANGE_TYPES = {
    "added": ("Added", "Added"),
    "returned_to_export": ("Back in the export", "Added"),
    "moved": ("Moved", "Moved"),
    "moved_back": ("No longer marked moved", "Moved"),
    "removed": ("Removed", "Removed"),
    "reinstated": ("No longer marked removed", "Removed"),
    "dropped_from_export": ("No longer in the export", "Removed"),
    "appointed_elder": ("Appointed elder", "Appointments"),
    "appointed_ms": ("Appointed ministerial servant", "Appointments"),
    "appointed_regular_pioneer": ("Appointed regular pioneer", "Appointments"),
    "appointed_special_pioneer": ("Appointed special pioneer", "Appointments"),
    "no_longer_elder": ("No longer an elder", "Appointments ended"),
    "no_longer_ms": ("No longer a ministerial servant", "Appointments ended"),
    "regular_pioneer_ended": ("No longer a regular pioneer", "Appointments ended"),
    "special_pioneer_ended": ("No longer a special pioneer", "Appointments ended"),
    "baptized": ("Newly baptized", "Newly baptized"),
    "new_publisher": ("New publisher", "Publishers"),
    "reactivated": ("Reactivated", "Publishers"),
    "became_inactive": ("Became inactive", "Publishers"),
    "group_changed": ("Field service group changed", "Field service groups"),
    "address_changed": ("Home address changed", "Addresses"),
    "phone_changed": ("Phone number changed", "Phone numbers"),
    "anointed_changed": ("Anointed status changed", "Anointed"),
}
SECTIONS = list(dict.fromkeys(section for _, section in CHANGE_TYPES.values()))
#: Changes whose values are contact details - shown only to callers allowed to see them.
CONTACT_TYPES = {"address_changed", "phone_changed"}

_lock = threading.Lock()

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS imports (
  id           INTEGER PRIMARY KEY,
  as_of        TEXT NOT NULL,
  imported_at  TEXT NOT NULL,
  source       TEXT NOT NULL,
  sha256       TEXT NOT NULL,
  rows         INTEGER NOT NULL,
  changes      INTEGER NOT NULL,
  baseline     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS person_versions (
  id          INTEGER PRIMARY KEY,
  person_id   INTEGER NOT NULL,
  valid_from  TEXT NOT NULL,
  valid_to    TEXT,
  {", ".join(f"{f} TEXT NOT NULL DEFAULT ''" for f in FIELDS)}
);
CREATE INDEX IF NOT EXISTS idx_versions_person ON person_versions(person_id, valid_from);
CREATE INDEX IF NOT EXISTS idx_versions_open ON person_versions(valid_to);
CREATE TABLE IF NOT EXISTS person_changes (
  id            INTEGER PRIMARY KEY,
  person_id     INTEGER NOT NULL,
  person_name   TEXT NOT NULL,
  change_type   TEXT NOT NULL,
  detected_on   TEXT NOT NULL,
  effective_on  TEXT,
  old_value     TEXT,
  new_value     TEXT,
  import_id     INTEGER NOT NULL REFERENCES imports(id)
);
CREATE INDEX IF NOT EXISTS idx_changes_detected ON person_changes(detected_on);
CREATE INDEX IF NOT EXISTS idx_changes_person ON person_changes(person_id, detected_on);
CREATE INDEX IF NOT EXISTS idx_changes_type ON person_changes(change_type, detected_on);
"""


def history_path() -> Path:
    upload_dir = Path(os.getenv("UPLOAD_DIR", "./uploads"))
    return Path(os.getenv("HISTORY_DB", str(upload_dir / ".history" / "history.sqlite")))


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def local_today() -> date:
    """Today where the congregation is - the nightly upload lands just after midnight there."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(os.getenv("HISTORY_TZ", "America/Chicago"))).date()


# --- reading a CSV ------------------------------------------------------------

def _norm(field: str, value: str | None) -> str:
    value = (value or "").strip()
    if field == "address":
        return re.sub(r"\s+", " ", value)
    if field.startswith("phone_"):
        return re.sub(r"\s+", " ", value)
    if field in ("active", "moved", "removed", "anointed"):
        return "True" if value.lower() in ("true", "1", "yes") else "False"
    if field == "group_id":
        return "" if value in ("", "0") else value
    return value


def read_csv(data: bytes) -> dict[int, dict[str, str]]:
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    people: dict[int, dict[str, str]] = {}
    for row in csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace"))):
        try:
            pid = int(row.get("PersonID") or 0)
        except ValueError:
            continue
        if pid:
            people[pid] = {f: _norm(f, row.get(col)) for f, col in TRACKED.items()}
    return people


def _name(p: dict[str, str]) -> str:
    return p.get("display_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()


def _iso(value: str) -> str | None:
    """The export's 2018/04/21 as 2018-04-21; anything else as None."""
    m = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", value or "")
    return f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}" if m else None


def _phones(p: dict[str, str]) -> str:
    parts = [f"{label} {p[f]}" for f, label in
             (("phone_mobile", "mobile"), ("phone_home", "home"), ("phone_work", "work")) if p.get(f)]
    return ", ".join(parts)


def _group(p: dict[str, str]) -> str:
    return p.get("group_name") or ("" if not p.get("group_id") else f"group {p['group_id']}")


# --- the comparison -----------------------------------------------------------

def diff(old: dict[str, str] | None, new: dict[str, str] | None) -> list[tuple[str, str | None, str | None, str | None]]:
    """The changes between two states of one person: (type, old, new, effective date)."""
    if old is None and new is not None:
        return [("added", None, _group(new) or None, None)]
    if new is None:
        return [("dropped_from_export", _group(old) or None, None, None)]

    out: list[tuple[str, str | None, str | None, str | None]] = []
    o, n = old, new

    def flag(field: str, on: str, off: str, eff: str | None = None):
        if o[field] != n[field]:
            out.append((on if n[field] == "True" else off, o[field], n[field], eff))

    flag("moved", "moved", "moved_back")
    flag("removed", "removed", "reinstated", _iso(n["date_of_removed"]))

    op, np_ = o["privilege"], n["privilege"]
    eff_priv = _iso(n["date_of_privilege"])
    if np_ != op:
        if np_ == "E":
            out.append(("appointed_elder", op, np_, eff_priv))
        elif np_ == "MS":
            out.append(("appointed_ms", op, np_, eff_priv))
        if op == "E" and np_ != "E":
            out.append(("no_longer_elder", op, np_, None))
        if op == "MS" and np_ not in ("MS", "E"):
            out.append(("no_longer_ms", op, np_, None))
        if op == "No" and np_ == "UBP":
            out.append(("new_publisher", op, np_, None))
    if (op not in BAPTIZED and np_ in BAPTIZED) or (not o["date_of_baptism"] and n["date_of_baptism"]):
        out.append(("baptized", o["date_of_baptism"] or None, n["date_of_baptism"] or None,
                    _iso(n["date_of_baptism"])))

    ops, nps = o["pioneer_status"], n["pioneer_status"]
    if nps != ops:
        eff = _iso(n["date_of_first_month"])
        if nps == "RegularPioneer":
            out.append(("appointed_regular_pioneer", ops or None, nps, eff))
        if nps == "SpecialPioneer":
            out.append(("appointed_special_pioneer", ops or None, nps, eff))
        if ops == "RegularPioneer":
            out.append(("regular_pioneer_ended", ops, nps or None, None))
        if ops == "SpecialPioneer":
            out.append(("special_pioneer_ended", ops, nps or None, None))

    if o["active"] != n["active"]:
        publisher = np_ not in ("", "No")
        if n["active"] == "True" and publisher and not any(c[0] == "new_publisher" for c in out):
            out.append(("reactivated", "inactive", "active", None))
        elif n["active"] == "False" and publisher:
            out.append(("became_inactive", "active", "inactive", None))

    if (o["group_id"], o["group_name"]) != (n["group_id"], n["group_name"]):
        if o["group_id"] != n["group_id"] or o["group_id"] == "":
            out.append(("group_changed", _group(o) or None, _group(n) or None, None))
    if o["address"] != n["address"]:
        out.append(("address_changed", o["address"] or None, n["address"] or None, None))
    if _phones(o) != _phones(n):
        out.append(("phone_changed", _phones(o) or None, _phones(n) or None, None))
    flag("anointed", "anointed_changed", "anointed_changed")
    return out


def ingest(data: bytes, as_of: date | None = None, source: str = "upload",
           conn: sqlite3.Connection | None = None) -> dict:
    """Record one Persons.csv as the state on `as_of` (default: today, locally).

    Returns a summary. Re-importing the same file, or a file dated before the
    last import, changes nothing.
    """
    as_of = as_of or local_today()
    day = as_of.isoformat()
    digest = hashlib.sha256(data).hexdigest()
    people = read_csv(data)
    if not people:
        return {"skipped": "no persons in that file"}

    with _lock, closing(conn or connect()) as db:
        last = db.execute("SELECT as_of, sha256 FROM imports ORDER BY id DESC LIMIT 1").fetchone()
        if last and day < last["as_of"]:
            return {"skipped": f"older than the last import ({last['as_of']})"}
        if last and last["sha256"] == digest:
            if last["as_of"] == day:
                return {"skipped": "already imported today", "as_of": day}
            # The same file on a new day is still a night checked: record it, change nothing.
            db.execute(
                "INSERT INTO imports (as_of, imported_at, source, sha256, rows, changes, baseline) "
                "VALUES (?, ?, ?, ?, ?, 0, 0)",
                (day, datetime.now(timezone.utc).isoformat(timespec="seconds"), source, digest,
                 len(people)),
            )
            db.commit()
            return {"as_of": day, "persons": len(people), "changes": 0, "baseline": False,
                    "unchanged": True}

        baseline = last is None
        current = {r["person_id"]: dict(r) for r in
                   db.execute("SELECT * FROM person_versions WHERE valid_to IS NULL")}
        cur = db.execute(
            "INSERT INTO imports (as_of, imported_at, source, sha256, rows, changes, baseline) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (day, datetime.now(timezone.utc).isoformat(timespec="seconds"), source, digest,
             len(people), int(baseline)),
        )
        import_id = cur.lastrowid
        changes = 0

        def close(version_id: int):
            db.execute("UPDATE person_versions SET valid_to = ? WHERE id = ?", (day, version_id))

        def open_(pid: int, p: dict[str, str]):
            db.execute(
                f"INSERT INTO person_versions (person_id, valid_from, {', '.join(FIELDS)}) "
                f"VALUES (?, ?, {', '.join('?' * len(FIELDS))})",
                (pid, day, *[p[f] for f in FIELDS]),
            )

        def record(pid: int, name: str, found):
            nonlocal changes
            for kind, old, new, eff in found:
                db.execute(
                    "INSERT INTO person_changes (person_id, person_name, change_type, detected_on, "
                    "effective_on, old_value, new_value, import_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (pid, name, kind, day, eff, old, new, import_id),
                )
                changes += 1

        for pid, p in people.items():
            was = current.get(pid)
            if was is None:
                returning = db.execute(
                    "SELECT 1 FROM person_versions WHERE person_id = ? LIMIT 1", (pid,)).fetchone()
                open_(pid, p)
                if not baseline:
                    record(pid, _name(p), [("returned_to_export" if returning else "added",
                                            None, _group(p) or None, None)])
                continue
            old = {f: was[f] for f in FIELDS}
            if old != p:
                found = diff(old, p)
                close(was["id"])
                open_(pid, p)
                if found:
                    record(pid, _name(p), found)

        for pid, was in current.items():
            if pid not in people:
                close(was["id"])
                old = {f: was[f] for f in FIELDS}
                record(pid, _name(old), diff(old, None))

        db.execute("UPDATE imports SET changes = ? WHERE id = ?", (changes, import_id))
        purged = purge(db, as_of)
        db.commit()
        return {"as_of": day, "persons": len(people), "changes": changes,
                "baseline": baseline, "purged": purged}


def _months_before(day: date, months: int) -> date:
    y, m = divmod(day.year * 12 + day.month - 1 - months, 12)
    return date(y, m + 1, 1)


def purge(db: sqlite3.Connection, today: date) -> int:
    """Delete changes, and versions that ended, before the retention window."""
    cutoff = _months_before(today, RETENTION_MONTHS).isoformat()
    a = db.execute("DELETE FROM person_changes WHERE detected_on < ?", (cutoff,)).rowcount
    b = db.execute("DELETE FROM person_versions WHERE valid_to IS NOT NULL AND valid_to < ?",
                   (cutoff,)).rowcount
    return a + b


# --- reading it back ----------------------------------------------------------

def month_bounds(month: str) -> tuple[str, str]:
    m = re.match(r"^(\d{4})-(\d{2})$", month or "")
    if not m or not 1 <= int(m[2]) <= 12:
        raise ValueError("month must be YYYY-MM, e.g. 2026-11")
    y, mo = int(m[1]), int(m[2])
    start = date(y, mo, 1)
    end = date(y + (mo == 12), 1 if mo == 12 else mo + 1, 1)
    return start.isoformat(), end.isoformat()


def changes(since: str, until: str, *, change_type: str | None = None,
            person_id: int | None = None, show_contact: bool = True,
            conn: sqlite3.Connection | None = None) -> list[dict]:
    """Changes detected on or after `since` and before `until` (ISO dates)."""
    sql = "SELECT * FROM person_changes WHERE detected_on >= ? AND detected_on < ?"
    params: list = [since, until]
    if change_type:
        sql += " AND change_type = ?"
        params.append(change_type)
    if person_id is not None:
        sql += " AND person_id = ?"
        params.append(person_id)
    sql += " ORDER BY detected_on, person_name, id"
    with closing(conn or connect()) as db:
        rows = [dict(r) for r in db.execute(sql, params)]
    for r in rows:
        r["label"], r["section"] = CHANGE_TYPES.get(r["change_type"], (r["change_type"], "Other"))
        if r["change_type"] in CONTACT_TYPES and not show_contact:
            r["old_value"] = r["new_value"] = "(withheld)"
    return rows


def month_summary(month: str, *, show_contact: bool = True,
                  conn: sqlite3.Connection | None = None) -> dict:
    since, until = month_bounds(month)
    with closing(conn or connect()) as db:
        rows = changes(since, until, show_contact=show_contact, conn=_Keep(db))
        imports = [dict(r) for r in db.execute(
            "SELECT as_of, rows, changes, baseline FROM imports WHERE as_of >= ? AND as_of < ? "
            "ORDER BY as_of", (since, until))]
        first = db.execute("SELECT MIN(as_of) FROM imports").fetchone()[0]
    sections = {s: [] for s in SECTIONS}
    for r in rows:
        sections.setdefault(r["section"], []).append(r)
    counts = {}
    for r in rows:
        counts[r["change_type"]] = counts.get(r["change_type"], 0) + 1
    return {
        "month": month,
        "since": since,
        "until": until,
        "tracking_since": first,
        "complete": bool(first) and first < since,
        "imports": len(imports),
        "days_covered": [i["as_of"] for i in imports],
        "total": len(rows),
        "counts": counts,
        "sections": {k: v for k, v in sections.items() if v},
    }


def person_history(person_id: int, *, show_contact: bool = True,
                   conn: sqlite3.Connection | None = None) -> dict:
    with closing(conn or connect()) as db:
        rows = changes("0000-01-01", "9999-12-31", person_id=person_id, show_contact=show_contact,
                       conn=_Keep(db))
        versions = [dict(r) for r in db.execute(
            "SELECT * FROM person_versions WHERE person_id = ? ORDER BY valid_from", (person_id,))]
    if not show_contact:
        for v in versions:
            for f in ("address", "phone_mobile", "phone_home", "phone_work"):
                v[f] = "(withheld)" if v[f] else ""
    return {"person_id": person_id, "changes": rows, "versions": versions}


class _Keep:
    """Lend an open connection to a helper that closes what it is given."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


# --- reports ------------------------------------------------------------------

def _value(r: dict) -> str:
    old, new = r.get("old_value"), r.get("new_value")
    if r["change_type"] in ("added", "returned_to_export"):
        return f"group: {new}" if new else ""
    if r["change_type"] == "dropped_from_export":
        return f"was in {old}" if old else ""
    if old is None and new is None:
        return ""
    if r["change_type"] in ("baptized",):
        return f"baptized {new}" if new else ""
    return f"{old or '-'} -> {new or '-'}"


def summary_markdown(summary: dict) -> str:
    out = [f"# Congregation changes - {summary['month']}", ""]
    if not summary["tracking_since"]:
        out.append("_No uploads have been recorded yet._")
        return "\n".join(out) + "\n"
    if not summary["complete"]:
        out.append(f"_Tracking began {summary['tracking_since']}; changes before then are not "
                   "known, so this month is partial._")
        out.append("")
    out.append(f"{summary['total']} change(s) from {summary['imports']} nightly upload(s).")
    out.append("")
    for section, rows in summary["sections"].items():
        out.append(f"## {section} ({len(rows)})")
        out.append("")
        out.append("| Detected | Person | Change | Details | Effective |")
        out.append("| --- | --- | --- | --- | --- |")
        for r in rows:
            out.append(f"| {r['detected_on']} | {r['person_name']} | {r['label']} | "
                       f"{_value(r).replace('|', '/')} | {r['effective_on'] or ''} |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def summary_pdf(summary: dict) -> bytes:
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    pdf = FPDF(format="A4", orientation="L")
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.set_margins(14, 14, 14)
    font_dir = Path(os.getenv("PDF_FONT_DIR", "/usr/share/fonts/truetype/dejavu"))
    if (font_dir / "DejaVuSans.ttf").is_file():
        pdf.add_font("Body", "", str(font_dir / "DejaVuSans.ttf"))
        pdf.add_font("Body", "B", str(font_dir / "DejaVuSans-Bold.ttf"))
        pdf.add_font("Body", "I", str(font_dir / "DejaVuSans-Oblique.ttf"))
        family, safe = "Body", (lambda s: s or "")
    else:
        family = "helvetica"
        safe = (lambda s: (s or "").encode("latin-1", "replace").decode("latin-1"))
    pdf.set_title(f"Congregation changes - {summary['month']}")
    pdf.alias_nb_pages()

    def footer():
        pdf.set_y(-10)
        pdf.set_font(family, "I", 8)
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 5, f"Confidential  -  page {pdf.page_no()} of {{nb}}", align="C")

    pdf.footer = footer
    pdf.add_page()
    pdf.set_font(family, "B", 16)
    pdf.cell(0, 9, safe(f"Congregation changes - {summary['month']}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, "", 9.5)
    pdf.set_text_color(90, 90, 90)
    lines = [f"{summary['total']} change(s) from {summary['imports']} nightly upload(s)."]
    if summary["tracking_since"] and not summary["complete"]:
        lines.append(f"Tracking began {summary['tracking_since']}; this month is partial.")
    for line in lines:
        pdf.cell(0, 5, safe(line), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    for section, rows in summary["sections"].items():
        if pdf.get_y() > pdf.h - 40:
            pdf.add_page()
        pdf.set_font(family, "B", 12)
        pdf.set_text_color(31, 41, 55)
        pdf.cell(0, 7, safe(f"{section} ({len(rows)})"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(family, "", 8.5)
        with pdf.table(col_widths=(18, 40, 46, 105, 20), line_height=4.6, padding=1.2,
                       headings_style=FontFace(emphasis="BOLD", fill_color=(243, 244, 246))) as t:
            t.row(["Detected", "Person", "Change", "Details", "Effective"])
            for r in rows:
                t.row([r["detected_on"], safe(r["person_name"]), safe(r["label"]),
                       safe(_value(r)), r["effective_on"] or ""])
        pdf.ln(3)
    return bytes(pdf.output())


# --- command line: seeding from archived exports ------------------------------

def _main(argv: list[str]) -> int:
    """python history.py backfill FILE... - files named YYYY-MM-DD_*.csv, imported in date order."""
    import sys

    if len(argv) < 2 or argv[0] != "backfill":
        print(_main.__doc__, file=sys.stderr)
        return 2
    dated = []
    for name in argv[1:]:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", Path(name).name)
        if not m:
            print(f"skipping {name}: no date in its name", file=sys.stderr)
            continue
        dated.append((m[1], name))
    for day, name in sorted(dated):
        result = ingest(Path(name).read_bytes(), date.fromisoformat(day), source=f"backfill {Path(name).name}")
        print(f"{day}: {result}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_main(sys.argv[1:]))
