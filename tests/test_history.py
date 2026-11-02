"""The monthly change history: every kind of change, read the way the nightly export writes it."""

from __future__ import annotations

import csv
import io
from datetime import date

import pytest

import history

COLUMNS = ["PersonID", "FirstName", "LastName", "DisplayName", "PhoneHome", "PhoneMobile",
           "PhoneWork", "Address", "DateOfBaptism", "FieldServiceGroupID", "GroupName", "Anointed",
           "Moved", "Privilege", "DateOfPrivilege", "Active", "DateOfFirstMonth", "PioneerStatus",
           "Removed", "DateOfRemoved", "Latitude"]

BASE = {
    "FirstName": "Ann", "LastName": "Boyd", "DisplayName": "Ann Boyd", "PhoneHome": "",
    "PhoneMobile": "555-0100", "PhoneWork": "", "Address": "12 Elm Row Allen TX 75013",
    "DateOfBaptism": "2010/05/01", "FieldServiceGroupID": "1", "GroupName": "Gruber",
    "Anointed": "False", "Moved": "False", "Privilege": "PUB", "DateOfPrivilege": "",
    "Active": "True", "DateOfFirstMonth": "", "PioneerStatus": "", "Removed": "False",
    "DateOfRemoved": "", "Latitude": "33.1",
}


def export(*people: dict) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()
    for p in people:
        writer.writerow({**BASE, **p})
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def person(pid: int, **changes) -> dict:
    return {"PersonID": str(pid), **changes}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HISTORY_DB", str(tmp_path / "history.sqlite"))
    return tmp_path / "history.sqlite"


def kinds(day: str) -> list[tuple[str, str]]:
    return sorted((r["person_name"], r["change_type"]) for r in history.changes(day, "9999-12-31"))


def test_the_first_import_is_a_baseline(db):
    result = history.ingest(export(person(1), person(2, DisplayName="Bo Carr")), date(2026, 11, 1))
    assert result["baseline"] is True and result["changes"] == 0
    assert history.changes("0000-01-01", "9999-12-31") == []


def test_every_kind_of_change(db):
    history.ingest(export(
        person(1),
        person(2, DisplayName="Bo Carr", Privilege="MS"),
        person(3, DisplayName="Cy Dunn", Privilege="UBP", DateOfBaptism=""),
        person(4, DisplayName="Di Ennis", Privilege="No", DateOfBaptism="", Active="False"),
        person(5, DisplayName="Ed Fox", Active="False"),
        person(6, DisplayName="Flo Gale"),
        person(7, DisplayName="Gus Hart", PioneerStatus="RegularPioneer"),
        person(8, DisplayName="Hal Ives"),
    ), date(2026, 11, 1))

    history.ingest(export(
        person(1, PhoneMobile="555-0199", Address="9 Oak Lane  Allen TX 75002", GroupName="Moody",
               FieldServiceGroupID="2", Latitude="33.9"),
        person(2, DisplayName="Bo Carr", Privilege="E", DateOfPrivilege="2026/11/01"),
        person(3, DisplayName="Cy Dunn", Privilege="PUB", DateOfBaptism="2026/10/31"),
        person(4, DisplayName="Di Ennis", Privilege="UBP", DateOfBaptism="", Active="True"),
        person(5, DisplayName="Ed Fox", Active="True"),
        person(6, DisplayName="Flo Gale", Active="False", Anointed="True"),
        person(7, DisplayName="Gus Hart", PioneerStatus="SpecialPioneer", DateOfFirstMonth="2026/11/01"),
        person(9, DisplayName="Ivy Jones"),
        # 8 is gone from the export entirely
    ), date(2026, 11, 2))

    assert kinds("2026-11-02") == sorted([
        ("Ann Boyd", "address_changed"), ("Ann Boyd", "group_changed"), ("Ann Boyd", "phone_changed"),
        ("Bo Carr", "appointed_elder"),
        ("Cy Dunn", "baptized"),
        ("Di Ennis", "new_publisher"),
        ("Ed Fox", "reactivated"),
        ("Flo Gale", "anointed_changed"), ("Flo Gale", "became_inactive"),
        ("Gus Hart", "appointed_special_pioneer"), ("Gus Hart", "regular_pioneer_ended"),
        ("Hal Ives", "dropped_from_export"),
        ("Ivy Jones", "added"),
    ])
    by = {(r["person_name"], r["change_type"]): r for r in history.changes("2026-11-02", "9999-12-31")}
    assert by[("Bo Carr", "appointed_elder")]["effective_on"] == "2026-11-01"
    assert by[("Cy Dunn", "baptized")]["effective_on"] == "2026-10-31"
    assert by[("Ann Boyd", "group_changed")]["old_value"] == "Gruber"
    assert by[("Ann Boyd", "group_changed")]["new_value"] == "Moody"
    assert by[("Ann Boyd", "phone_changed")]["new_value"] == "mobile 555-0199"


def test_moved_removed_and_their_reversals(db):
    history.ingest(export(person(1), person(2, DisplayName="Bo Carr")), date(2026, 11, 1))
    history.ingest(export(person(1, Moved="True"),
                          person(2, DisplayName="Bo Carr", Removed="True", DateOfRemoved="2026/11/01")),
                   date(2026, 11, 2))
    assert kinds("2026-11-02") == [("Ann Boyd", "moved"), ("Bo Carr", "removed")]
    history.ingest(export(person(1), person(2, DisplayName="Bo Carr")), date(2026, 11, 3))
    assert kinds("2026-11-03") == [("Ann Boyd", "moved_back"), ("Bo Carr", "reinstated")]


def test_ending_appointments_and_coming_back(db):
    history.ingest(export(person(1, Privilege="E"), person(2, DisplayName="Bo Carr", Privilege="MS")),
                   date(2026, 11, 1))
    history.ingest(export(person(1, Privilege="PUB"), person(2, DisplayName="Bo Carr", Privilege="E")),
                   date(2026, 11, 2))
    # MS -> E is an appointment, not an MS appointment ending
    assert kinds("2026-11-02") == [("Ann Boyd", "no_longer_elder"), ("Bo Carr", "appointed_elder")]
    history.ingest(export(person(2, DisplayName="Bo Carr", Privilege="E")), date(2026, 11, 3))
    history.ingest(export(person(1, Privilege="PUB"), person(2, DisplayName="Bo Carr", Privilege="E")),
                   date(2026, 11, 4))
    assert kinds("2026-11-04") == [("Ann Boyd", "returned_to_export")]


def test_noise_is_not_a_change(db):
    history.ingest(export(person(1)), date(2026, 11, 1))
    result = history.ingest(export(person(1, Latitude="40.0", Address="12 Elm Row  Allen TX 75013 ")),
                            date(2026, 11, 2))
    assert result["changes"] == 0
    # renaming a group is not a person changing group
    assert history.ingest(export(person(1, GroupName="Gruber North")), date(2026, 11, 3))["changes"] == 0


def test_repeats_and_out_of_order_files(db):
    data = export(person(1))
    history.ingest(data, date(2026, 11, 1))
    assert history.ingest(data, date(2026, 11, 1))["skipped"] == "already imported today"
    assert history.ingest(data, date(2026, 11, 2)).get("unchanged") is True
    assert "older than" in history.ingest(export(person(1, Moved="True")), date(2026, 10, 1))["skipped"]
    assert history.month_summary("2026-11")["days_covered"] == ["2026-11-01", "2026-11-02"]


def test_versions_answer_what_a_person_looked_like(db):
    history.ingest(export(person(1)), date(2026, 11, 1))
    history.ingest(export(person(1, Privilege="E")), date(2026, 11, 5))
    versions = history.person_history(1)["versions"]
    assert [(v["valid_from"], v["valid_to"], v["privilege"]) for v in versions] == [
        ("2026-11-01", "2026-11-05", "PUB"), ("2026-11-05", None, "E")]


def test_contact_values_can_be_withheld(db):
    history.ingest(export(person(1)), date(2026, 11, 1))
    history.ingest(export(person(1, PhoneMobile="555-0199", Privilege="E")), date(2026, 11, 2))
    rows = {r["change_type"]: r for r in history.changes("2026-11-01", "2026-12-01", show_contact=False)}
    assert rows["phone_changed"]["new_value"] == "(withheld)"
    assert rows["appointed_elder"]["new_value"] == "E"
    versions = history.person_history(1, show_contact=False)["versions"]
    assert all(v["phone_mobile"] in ("", "(withheld)") for v in versions)


def test_48_months_are_kept(db):
    history.ingest(export(person(1)), date(2022, 1, 3))
    history.ingest(export(person(1, Moved="True")), date(2022, 1, 4))
    history.ingest(export(person(1, Moved="True", Privilege="MS")), date(2026, 1, 31))
    assert len(history.changes("2000-01-01", "9999-12-31")) == 2          # Jan 2022 is inside
    history.ingest(export(person(1, Moved="True", Privilege="E")), date(2026, 2, 1))
    remaining = history.changes("2000-01-01", "9999-12-31")
    assert [r["detected_on"] for r in remaining] == ["2026-01-31", "2026-02-01"]


def test_month_summary_reports_and_partial_months(db):
    history.ingest(export(person(1)), date(2026, 11, 10))
    history.ingest(export(person(1, Privilege="MS")), date(2026, 11, 11))
    november = history.month_summary("2026-11")
    assert november["complete"] is False and november["tracking_since"] == "2026-11-10"
    assert list(november["sections"]) == ["Appointments"]
    md = history.summary_markdown(november)
    assert "Appointed ministerial servant" in md and "partial" in md
    assert history.summary_pdf(november).startswith(b"%PDF")
    history.ingest(export(person(1, Privilege="MS", Active="False")), date(2026, 12, 2))
    assert history.month_summary("2026-12")["complete"] is True
    with pytest.raises(ValueError):
        history.month_bounds("2026-13")
