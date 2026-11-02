"""
Encrypted backups of what persons-uploader cannot get back by itself.

The nightly Persons.csv is re-uploaded every night, but the change history
(`history.py`) is 48 months of record that exists nowhere else, and the
logins, API keys and custom tag files are typed in by people. A backup holds:

* `history.sqlite` - a consistent online copy, integrity-checked
* `credentials.json`, `.mcp/api_keys.json`, the `tag_*.json` files
* the latest `Persons.csv` and `Emergency-Contacts.csv` (without its notes)

sealed with a passphrase in the same format Shepherd's Notebook uses (scrypt,
then AES-256-GCM over a zip, the header as associated data), so one passphrase
opens both. The host runs it nightly (deploy/persons-uploader-backup):

    docker exec -i persons-uploader-allen python backup.py create < passphrase > file.pubackup
    (passphrase line, then the file) | docker exec -i persons-uploader-allen python backup.py verify
    (passphrase line, then the file) | docker exec -i persons-uploader-allen python backup.py restore

Restoring keeps what it replaces under `$UPLOAD_DIR/.pre-restore-<time>/`;
restart the container afterwards so the logins and keys are reloaded.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

MAGIC = b"PUBACKUP1\n"
MIN_PASSPHRASE = 12
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 17, 8, 1


class BackupError(ValueError):
    pass


def _paths() -> dict[str, Path]:
    upload = Path(os.getenv("UPLOAD_DIR", "./uploads"))
    import history

    return {
        "upload_dir": upload,
        "history": history.history_path(),
        "credentials": Path(os.getenv("CREDENTIALS_FILE", "credentials.json")),
        "api_keys": Path(os.getenv("MCP_STATE_DIR", str(upload / ".mcp"))) / "api_keys.json",
        "persons": upload / "Persons.csv",
        "emergency": upload / "Emergency-Contacts.csv",
    }


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


def _check_db(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise BackupError(f"The history copy failed its integrity check: {ok}")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"imports", "person_versions", "person_changes"} <= tables:
            raise BackupError("That history database is not persons-uploader's.")
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("imports", "person_versions", "person_changes")}
    finally:
        conn.close()


def create(passphrase: str) -> tuple[bytes, dict]:
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters.")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    paths = _paths()
    contents: dict[str, object] = {}
    buffer = io.BytesIO()
    with tempfile.TemporaryDirectory() as work, \
            zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if paths["history"].is_file():
            copy = Path(work) / "history.sqlite"
            source, target = sqlite3.connect(paths["history"]), sqlite3.connect(copy)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            contents["history"] = _check_db(copy)
            archive.write(copy, "history.sqlite")
        for name, path in (("credentials.json", paths["credentials"]),
                           ("api_keys.json", paths["api_keys"]),
                           ("Persons.csv", paths["persons"]),
                           ("Emergency-Contacts.csv", paths["emergency"])):
            if path.is_file():
                archive.write(path, name)
                contents[name] = path.stat().st_size
        tags = sorted(paths["upload_dir"].glob("*.json"))
        for tag in tags:
            archive.write(tag, f"tags/{tag.name}")
        contents["tag_files"] = len(tags)
        manifest = {"format": "persons-uploader/backup", "version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "contents": contents}
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))

    salt, nonce = os.urandom(16), os.urandom(12)
    header = json.dumps({"kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                         "salt": base64.b64encode(salt).decode(),
                         "nonce": base64.b64encode(nonce).decode(),
                         "cipher": "AES-256-GCM", "created_at": manifest["created_at"]}).encode() + b"\n"
    key = _derive(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return MAGIC + header + AESGCM(key).encrypt(nonce, buffer.getvalue(), MAGIC + header), manifest


def open_backup(data: bytes, passphrase: str) -> tuple[zipfile.ZipFile, dict]:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not data.startswith(MAGIC):
        raise BackupError("That file is not a persons-uploader backup.")
    try:
        header_line, sealed = data[len(MAGIC):].split(b"\n", 1)
        h = json.loads(header_line)
        salt, nonce = base64.b64decode(h["salt"]), base64.b64decode(h["nonce"])
        n, r, p = int(h["n"]), int(h["r"]), int(h["p"])
    except (ValueError, KeyError) as exc:
        raise BackupError("That backup's header is damaged.") from exc
    if not (2 ** 14 <= n <= 2 ** 20 and 1 <= r <= 16 and 1 <= p <= 4):
        raise BackupError("That backup's header asks for impossible settings.")
    try:
        plain = AESGCM(_derive(passphrase, salt, n, r, p)).decrypt(nonce, sealed, MAGIC + header_line + b"\n")
    except InvalidTag:
        raise BackupError("Wrong passphrase, or the file has been damaged.") from None
    try:
        archive = zipfile.ZipFile(io.BytesIO(plain))
        manifest = json.loads(archive.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise BackupError("That backup's contents are damaged.") from exc
    if manifest.get("format") != "persons-uploader/backup":
        raise BackupError("That file is not a persons-uploader backup.")
    if "history.sqlite" in archive.namelist():
        with tempfile.TemporaryDirectory() as work:
            copy = Path(work) / "history.sqlite"
            copy.write_bytes(archive.read("history.sqlite"))
            manifest["contents"]["history"] = _check_db(copy)
    return archive, manifest


def restore(data: bytes, passphrase: str) -> dict:
    """Put a backup's files in place, keeping whatever they replace."""
    archive, manifest = open_backup(data, passphrase)
    paths = _paths()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    keep = paths["upload_dir"] / f".pre-restore-{stamp}"
    keep.mkdir(parents=True)

    def put(member: str, dest: Path):
        if dest.exists():
            shutil.copy2(dest, keep / dest.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".restoring")
        tmp.write_bytes(archive.read(member))
        os.replace(tmp, dest)

    names = archive.namelist()
    if "history.sqlite" in names:
        live = paths["history"]
        if live.exists():
            shutil.copy2(live, keep / live.name)
        live.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as work:
            copy = Path(work) / "history.sqlite"
            copy.write_bytes(archive.read("history.sqlite"))
            source, target = sqlite3.connect(copy), sqlite3.connect(live)
            try:
                source.backup(target)      # page for page, under SQLite's own locks
            finally:
                target.close()
                source.close()
    if "credentials.json" in names:
        # A bind-mounted file is overwritten in place, not replaced.
        dest = paths["credentials"]
        if dest.exists():
            shutil.copy2(dest, keep / dest.name)
        dest.write_bytes(archive.read("credentials.json"))
    if "api_keys.json" in names:
        put("api_keys.json", paths["api_keys"])
    if "Persons.csv" in names:
        put("Persons.csv", paths["persons"])
    if "Emergency-Contacts.csv" in names:
        put("Emergency-Contacts.csv", paths["emergency"])
    for member in names:
        if member.startswith("tags/") and member.endswith(".json"):
            put(member, paths["upload_dir"] / Path(member).name)
    return {"restored_from": manifest["created_at"], "contents": manifest["contents"],
            "previous_copies": str(keep)}


def _main(argv: list[str]) -> int:
    usage = ("usage: python backup.py create < passphrase > file.pubackup\n"
             "       (passphrase line, then the backup) | python backup.py verify|restore")
    if not argv or argv[0] not in ("create", "verify", "restore"):
        print(usage, file=sys.stderr)
        return 2
    passphrase = sys.stdin.buffer.readline().decode("utf-8").rstrip("\r\n")
    try:
        if argv[0] == "create":
            data, manifest = create(passphrase)
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            print(f"backup created: {manifest['contents']}, {len(data):,} bytes", file=sys.stderr)
        elif argv[0] == "verify":
            _, manifest = open_backup(sys.stdin.buffer.read(), passphrase)
            print(f"backup ok: made {manifest['created_at']}, {manifest['contents']}", file=sys.stderr)
        else:
            result = restore(sys.stdin.buffer.read(), passphrase)
            print(f"restored: {result}\nRestart the container to reload logins and API keys.",
                  file=sys.stderr)
    except BackupError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
