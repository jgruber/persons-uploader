"""The upload records history, the page and reports render, agents see what their scopes allow,
and a backup brings everything back."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backup
import history
import main
from tests.test_history import export, person

AUTH = ("admin", "test-password")
PASSPHRASE = "correct horse battery staple"


@pytest.fixture(autouse=True)
def fresh_history():
    path = history.history_path()
    for p in (path, Path(str(path) + "-journal")):
        if p.exists():
            p.unlink()
    yield


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def upload(client, data: bytes):
    return client.post("/upload", auth=AUTH, files={"file": ("Persons.csv", data, "text/csv")})


def test_each_upload_is_recorded(client, monkeypatch):
    monkeypatch.setattr(history, "local_today", lambda: date(2026, 11, 1))
    first = upload(client, export(person(1), person(2, DisplayName="Bo Carr")))
    assert first.status_code == 200 and "baseline recorded" in first.json()["message"]

    monkeypatch.setattr(history, "local_today", lambda: date(2026, 11, 2))
    second = upload(client, export(person(1, Privilege="E"), person(2, DisplayName="Bo Carr",
                                                                     PhoneMobile="555-0177")))
    assert "2 change(s) recorded" in second.json()["message"]

    page = client.get("/changes?month=2026-11", auth=AUTH)
    assert page.status_code == 200
    assert "Appointed elder" in page.text and "Phone number changed" in page.text
    assert "Tracking began 2026-11-01" in page.text          # November is partial

    pdf = client.get("/changes/report?month=2026-11&format=pdf", auth=AUTH)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    md = client.get("/changes/report?month=2026-11&format=md", auth=AUTH)
    assert "| Ann Boyd | Appointed elder |" in md.text
    assert client.get("/changes/report?month=2026-11&format=xls", auth=AUTH).status_code == 400
    assert client.get("/changes?month=nope", auth=AUTH).status_code == 400
    assert client.get("/changes").status_code == 401


def test_a_broken_history_never_loses_the_upload(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(history, "ingest", boom)
    response = upload(client, export(person(1)))
    assert response.status_code == 200 and "error" in response.json()["history"]
    assert (Path(os.environ["UPLOAD_DIR"]) / "Persons.csv").exists()


def _agent(scopes):
    token = main._create_api_key("t", scopes, None)
    return token


def _call(client, token, tool, **arguments):
    r = client.post("/mcp", headers={"Authorization": f"Bearer {token}",
                                     "Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": arguments}})
    result = r.json()["result"]
    text = result["content"][0]["text"]
    return {"error": text} if result.get("isError") else json.loads(text)


def test_agents_see_changes_as_their_scopes_allow(client, monkeypatch):
    monkeypatch.setattr(history, "local_today", lambda: date(2026, 11, 1))
    upload(client, export(person(1), person(2, DisplayName="Bo Carr")))
    monkeypatch.setattr(history, "local_today", lambda: date(2026, 11, 2))
    upload(client, export(person(1, Privilege="E", DateOfPrivilege="2026/11/01", Anointed="True"),
                          person(2, DisplayName="Bo Carr", PhoneMobile="555-0177",
                                 Address="1 New St Allen TX")))

    base = _call(client, _agent([]), "person_changes", month="2026-11")
    kinds = {r["change_type"]: r for rows in base["sections"].values() for r in rows}
    assert "anointed_changed" not in kinds                         # personal circumstance
    assert kinds["phone_changed"]["new_value"].startswith("(withheld")
    assert kinds["address_changed"]["new_value"].startswith("(withheld")
    assert kinds["appointed_elder"]["effective_on"] is None
    assert base["complete"] is False and base["nightly_uploads_in_period"] == 2

    full = _call(client, _agent(["contact_details"]), "person_changes", month="2026-11")
    kinds = {r["change_type"]: r for rows in full["sections"].values() for r in rows}
    assert kinds["anointed_changed"]["new_value"] == "True"
    assert kinds["phone_changed"]["new_value"] == "mobile 555-0177"
    assert kinds["appointed_elder"]["effective_on"] == "2026-11-01"

    contact = _call(client, _agent(["contact"]), "person_changes", month="2026-11",
                    change_type="phone_changed")
    assert contact["total"] == 1
    assert "Phone numbers" in contact["sections"]

    one = _call(client, _agent([]), "person_change_history", person_id=1)
    assert [r["change_type"] for r in one["changes"]] == ["appointed_elder"]
    assert "must be one of" in _call(client, _agent([]), "person_changes", change_type="x")["error"]


def test_backup_round_trip(client, monkeypatch, tmp_path):
    monkeypatch.setattr(history, "local_today", lambda: date(2026, 11, 1))
    upload(client, export(person(1)))
    monkeypatch.setattr(history, "local_today", lambda: date(2026, 11, 2))
    upload(client, export(person(1, Privilege="MS")))
    upload_dir = Path(os.environ["UPLOAD_DIR"])
    (upload_dir / "tag_English_Elders.json").write_text('{"version": 1, "tagName": "English Elders", "assignments": []}')

    made = subprocess.run([sys.executable, "backup.py", "create"], input=(PASSPHRASE + "\n").encode(),
                          capture_output=True, env=os.environ.copy())
    assert made.returncode == 0, made.stderr
    sealed = made.stdout
    assert sealed.startswith(backup.MAGIC) and b"Boyd" not in sealed

    with pytest.raises(backup.BackupError, match="Wrong passphrase"):
        backup.open_backup(sealed, "not the right passphrase")
    _, manifest = backup.open_backup(sealed, PASSPHRASE)
    assert manifest["contents"]["history"]["person_changes"] == 1
    assert manifest["contents"]["tag_files"] == 1

    # Disaster: the history and the tag file are lost.
    history.history_path().unlink()
    (upload_dir / "tag_English_Elders.json").unlink()
    result = subprocess.run([sys.executable, "backup.py", "restore"],
                            input=(PASSPHRASE + "\n").encode() + sealed,
                            capture_output=True, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
    assert b"Restart the container" in result.stderr
    assert [r["change_type"] for r in history.changes("2026-11-01", "2026-12-01")] == ["appointed_ms"]
    assert (upload_dir / "tag_English_Elders.json").exists()
    assert any(p.name.startswith(".pre-restore-") for p in upload_dir.iterdir())
