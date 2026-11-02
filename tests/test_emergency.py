"""Emergency contacts: notes never stored, linked to persons, queryable by SQL, and the wrong
file at either upload URL is refused rather than overwriting the other."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import emergency
import main
from tests.test_history import export, person

AUTH = ("admin", "test-password")
UPLOADS = Path(os.environ["UPLOAD_DIR"])
COLUMNS = ["PublisherID", "PublisherFirstName", "PublisherLastName", "ID", "CongregationPersonID",
           "Order", "Name", "Email", "Mobile", "Phone", "Relationship", "Notes"]


def contacts(*rows: dict) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS)
    writer.writeheader()
    for i, r in enumerate(rows):
        writer.writerow({"ID": str(9000 + i), "CongregationPersonID": "0", "Email": "", "Mobile": "",
                         "Phone": "", "Relationship": "", "Notes": "", **r})
    return ("﻿" + out.getvalue()).encode()


PERSONS = export(
    person(1, FirstName="Ann", LastName="Boyd", DisplayName="Ann Boyd"),
    person(2, FirstName="Bo", LastName="Carr", DisplayName="Bo Carr"),
    person(3, FirstName="Sam", LastName="Lee", DisplayName="Sam Lee"),
    person(4, FirstName="Sam", LastName="Lee", DisplayName="Sam Lee"),     # two people, one name
)
CONTACTS = contacts(
    # PublisherID 1 is Ann's PersonID and the names agree
    {"PublisherID": "1", "PublisherFirstName": "Ann", "PublisherLastName": "Boyd", "Order": "1",
     "Name": "Ron Boyd", "Relationship": "Husband", "Mobile": "555-0101",
     "Notes": "Allergic to penicillin - SECRET NOTE"},
    {"PublisherID": "1", "PublisherFirstName": "Ann", "PublisherLastName": "Boyd", "Order": "0",
     "Name": "Kay Boyd", "Relationship": "Daughter", "Email": "kay@example.org"},
    # A different numbering: matched by name
    {"PublisherID": "500123", "PublisherFirstName": "bo ", "PublisherLastName": "CARR", "Order": "0",
     "Name": "Di Carr", "Relationship": "Wife", "Phone": "555-0102"},
    # Two people are called Sam Lee
    {"PublisherID": "500124", "PublisherFirstName": "Sam", "PublisherLastName": "Lee", "Order": "0",
     "Name": "Pat Lee"},
    # Nobody by this name
    {"PublisherID": "500125", "PublisherFirstName": "No", "PublisherLastName": "Body", "Order": "0",
     "Name": "X"},
)


@pytest.fixture
def client():
    for name in ("Persons.csv", emergency.FILENAME):
        if (UPLOADS / name).exists():
            (UPLOADS / name).unlink()
    with TestClient(main.app) as c:
        yield c


def upload(client, data: bytes, contacts_url: bool = False, name="file.csv"):
    url = "/upload?emergency_contacts" if contacts_url else "/upload"
    return client.post(url, auth=AUTH, files={"file": (name, data, "text/csv")})


def test_notes_are_never_stored():
    kept, rows = emergency.sanitize(CONTACTS)
    assert rows == 5
    assert b"SECRET NOTE" not in kept and b"Notes" not in kept
    assert kept.splitlines()[0].decode().split(",") == emergency.KEEP


def test_linking_by_id_then_by_unique_name():
    linked, unlinked = emergency.link(PERSONS, CONTACTS)
    assert [(c["person_id"], c["contact_order"], c["name"]) for c in linked] == [
        (1, 0, "Kay Boyd"), (1, 1, "Ron Boyd"), (2, 0, "Di Carr")]
    assert {u["publisher"]: (u["contacts"], u["reason"]) for u in unlinked} == {
        "No Body": (1, "no person with this name"),
        "Sam Lee": (1, "two or more people share this name"),
    }


def test_each_url_refuses_the_other_file(client):
    assert upload(client, PERSONS).status_code == 200
    before = (UPLOADS / "Persons.csv").read_bytes()

    wrong = upload(client, CONTACTS, name="Emergency-Contacts.csv")
    assert wrong.status_code == 400 and "/upload?emergency_contacts" in wrong.json()["detail"]
    assert (UPLOADS / "Persons.csv").read_bytes() == before          # roster untouched

    wrong = upload(client, PERSONS, contacts_url=True, name="Persons.csv")
    assert wrong.status_code == 400 and "upload it to /upload" in wrong.json()["detail"]
    assert not (UPLOADS / emergency.FILENAME).exists()

    junk = upload(client, b"a,b\n1,2\n")
    assert junk.status_code == 400 and (UPLOADS / "Persons.csv").read_bytes() == before


def test_upload_download_and_sql(client):
    upload(client, PERSONS)
    ok = upload(client, CONTACTS, contacts_url=True, name="Emergency-Contacts.csv")
    assert ok.status_code == 200, ok.text
    assert "3 linked to 2 persons" in ok.json()["message"]
    assert ok.json()["emergency_contacts"]["unlinked_contacts"] == 2
    assert b"SECRET NOTE" not in (UPLOADS / emergency.FILENAME).read_bytes()

    listed = client.get("/download?emergency_contacts", auth=AUTH).json()
    assert [(c["person_id"], c["name"]) for c in listed] == [(1, "Kay Boyd"), (1, "Ron Boyd"), (2, "Di Carr")]
    assert "notes" not in json.dumps(listed).lower()

    db_file = UPLOADS / "downloaded.db"
    db_file.write_bytes(client.get("/download/database", auth=AUTH).content)
    conn = sqlite3.connect(db_file)
    rows = conn.execute("""
        SELECT p.display_name, e.contact_order, e.name, e.relationship
          FROM emergency_contacts e JOIN persons p ON p.id = e.person_id
         ORDER BY p.id, e.contact_order""").fetchall()
    assert rows == [("Ann Boyd", 0, "Kay Boyd", "Daughter"), ("Ann Boyd", 1, "Ron Boyd", "Husband"),
                    ("Bo Carr", 0, "Di Carr", "Wife")]
    fk = conn.execute("PRAGMA foreign_key_list(emergency_contacts)").fetchall()
    assert [(f[2], f[3], f[4]) for f in fk] == [("persons", "person_id", "id")]
    conn.close()
    db_file.unlink()

    admin = client.get("/admin", auth=AUTH)
    assert "Sam Lee" in admin.text and "two or more people share this name" in admin.text

    assert client.delete("/upload/emergency_contacts", auth=AUTH).status_code == 200
    assert client.get("/download?emergency_contacts", auth=AUTH).status_code == 404


def test_agents_see_emergency_contacts_only_with_contact_details(client):
    upload(client, PERSONS)
    upload(client, CONTACTS, contacts_url=True)

    def get_person(scopes):
        token = main._create_api_key("t", scopes, None)
        r = client.post("/mcp", headers={"Authorization": f"Bearer {token}",
                                         "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "get_person", "arguments": {"person_id": 1}}})
        return json.loads(r.json()["result"]["content"][0]["text"])

    assert "emergency_contacts" not in get_person(["contact", "address"])
    full = get_person(["contact_details"])["emergency_contacts"]
    assert [(c["order"], c["name"], c["relationship"]) for c in full] == [
        (0, "Kay Boyd", "Daughter"), (1, "Ron Boyd", "Husband")]


def test_a_person_listed_twice_keeps_each_contact_once():
    twice = contacts(
        {"PublisherID": "1", "PublisherFirstName": "Ann", "PublisherLastName": "Boyd", "Order": "0",
         "Name": "Kay Boyd", "Relationship": "Daughter"},
        {"PublisherID": "700001", "PublisherFirstName": "Ann", "PublisherLastName": "Boyd", "Order": "0",
         "Name": "Kay Boyd", "Relationship": "Daughter"},
        {"PublisherID": "700001", "PublisherFirstName": "Ann", "PublisherLastName": "Boyd", "Order": "1",
         "Name": "Ron Boyd", "Relationship": "Husband"},
    )
    linked, _ = emergency.link(PERSONS, twice)
    assert [(c["person_id"], c["name"]) for c in linked] == [(1, "Kay Boyd"), (1, "Ron Boyd")]
