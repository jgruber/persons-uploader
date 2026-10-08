import contextlib
import csv
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from starlette.background import BackgroundTask
from starlette.routing import Route
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    # The MCP session manager (defined further down) needs its task group running
    async with _mcp.session_manager.run():
        yield


app = FastAPI(title="Persons Uploader", lifespan=_lifespan)
security = HTTPBasic()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./uploads"))
CREDENTIALS_FILE = Path("credentials.json")

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# API keys, the cached directory DB and the MCP call log live on the uploads volume,
# in a subdirectory so the *.json tag-file listing never sees them.
MCP_STATE_DIR = Path(os.getenv("MCP_STATE_DIR", str(UPLOAD_DIR / ".mcp")))
MCP_STATE_DIR.mkdir(parents=True, exist_ok=True)
API_KEYS_FILE = MCP_STATE_DIR / "api_keys.json"
MCP_DB_PATH = MCP_STATE_DIR / "persons.db"
MCP_CALL_LOG = MCP_STATE_DIR / "calls.log"

_jinja_env = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=True,
    cache_size=0,  # avoids Jinja2 >=3.1.4 bug where globals dict ends up in LRU cache key
)
templates = Jinja2Templates(env=_jinja_env)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# User store  {username: {password, can_upload}}
# ---------------------------------------------------------------------------
def _load_users() -> dict[str, dict]:
    if CREDENTIALS_FILE.exists():
        data = json.loads(CREDENTIALS_FILE.read_text())
        # Migrate from old single-user format
        if isinstance(data, dict) and "username" in data:
            return {data["username"]: {"password": data["password"], "can_upload": True}}
        return {
            u["username"]: {"password": u["password"], "can_upload": u.get("can_upload", False)}
            for u in data
        }
    return {
        os.getenv("AUTH_USERNAME", "admin"): {
            "password": os.getenv("AUTH_PASSWORD", "changeme"),
            "can_upload": True,
        }
    }


def _save_users() -> None:
    data = [
        {"username": uname, "password": u["password"], "can_upload": u["can_upload"]}
        for uname, u in _users.items()
    ]
    CREDENTIALS_FILE.write_text(json.dumps(data, indent=2))


_users: dict[str, dict] = _load_users()


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------
def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> dict:
    user = _users.get(credentials.username)
    if not user or not secrets.compare_digest(credentials.password, user["password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return {"username": credentials.username, **user}


def require_upload(user: dict = Depends(require_auth)) -> dict:
    if not user["can_upload"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload permission required.")
    return user


def require_admin(user: dict = Depends(require_auth)) -> dict:
    if not user["can_upload"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return user


# ---------------------------------------------------------------------------
# CSV → SQLite conversion  (mirrors congregation-directory SPA logic)
# ---------------------------------------------------------------------------
_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
# Sort multi-word state names first so they match before single-word substrings
_STATE_ENTRIES = sorted(_STATE_NAMES.items(), key=lambda x: -len(x[0].split()))

_STREET_TYPES = (
    "street|avenue|boulevard|drive|road|lane|court|place|circle|way|parkway|highway|"
    "freeway|expressway|terrace|trail|loop|pass|path|pike|row|square|crossing|park|"
    "run|hollow|ridge|heights|commons|manor|grove|bend|cove|falls|valley|plaza|"
    "bridge|estates|summit|landing|springs|canyon|point|pointe|"
    "st|ave|blvd|dr|rd|ln|ct|pl|cir|pkwy|hwy|fwy|expy|ter|trl|sq|xing|ste"
)
_STREET_RE = re.compile(rf"\b({_STREET_TYPES})\b", re.IGNORECASE)

_COMPASS = {
    "ne": "Northeast", "nw": "Northwest", "se": "Southeast", "sw": "Southwest",
    "n": "North", "s": "South", "e": "East", "w": "West",
}


def _parse_address(raw: str) -> dict:
    if not raw or not raw.strip():
        return {"address": "", "city": "", "state": "", "postal_code": ""}

    s = re.sub(r"[\r\n]+", " ", raw)
    s = re.sub(r"\s{2,}", " ", s).strip().rstrip(".,; ")

    # Expand full state names to abbreviations (multi-word first)
    for name, abbr in _STATE_ENTRIES:
        s = re.sub(rf"\b{re.escape(name)}\b", abbr, s, flags=re.IGNORECASE)

    # 1. Extract ZIP from end
    zip_m = re.search(r"\b(\d{5}(?:-\d{4})?)\s*$", s)
    if not zip_m:
        return {"address": s, "city": "", "state": "", "postal_code": ""}
    postal_code = zip_m.group(1)
    s = s[: zip_m.start()].strip().rstrip(", ")

    # 2. Extract 2-letter state abbreviation from end
    state_m = re.search(r",?\s+([A-Z]{2})$", s)
    if not state_m:
        return {"address": s, "city": "", "state": "", "postal_code": postal_code}
    state = state_m.group(1)
    s = s[: -len(state_m.group(0))].strip()

    # 3. Expand cardinal direction abbreviations
    s = re.sub(r"\b(NE|NW|SE|SW)\b", lambda m: _COMPASS[m.group(0).lower()], s, flags=re.IGNORECASE)
    s = re.sub(r"\b([NSEW])\b", lambda m: _COMPASS[m.group(0).lower()], s, flags=re.IGNORECASE)

    # 4. Split on last comma if present
    comma_idx = s.rfind(",")
    if comma_idx > 0:
        return {
            "address": s[:comma_idx].strip(),
            "city": s[comma_idx + 1:].strip(),
            "state": state,
            "postal_code": postal_code,
        }

    # 5. Split on last street-type keyword
    last_end = -1
    for m in _STREET_RE.finditer(s):
        last_end = m.end()

    if last_end > 0:
        if last_end < len(s) and s[last_end] == ".":
            last_end += 1
        unit_m = re.match(r"^[\s,]*(apt\.?|apartment|suite|ste\.?|unit|#\.?)\s*#?\s*[\w-]*", s[last_end:], re.IGNORECASE)
        if unit_m:
            last_end += len(unit_m.group(0))
        while last_end < len(s) and s[last_end] in " ,\t":
            last_end += 1
        address = s[:last_end].rstrip(", ")
        city = s[last_end:].strip()
        if city:
            return {"address": address, "city": city, "state": state, "postal_code": postal_code}

    return {"address": s, "city": "", "state": state, "postal_code": postal_code}


def _b(val) -> int:
    return 1 if val in ("True", "true", "1") else 0


_SCHEMA = """
PRAGMA foreign_keys = OFF;
CREATE TABLE IF NOT EXISTS congregations (
  id   INTEGER PRIMARY KEY,
  name TEXT
);
CREATE TABLE IF NOT EXISTS field_service_groups (
  id               INTEGER PRIMARY KEY,
  congregation_id  INTEGER,
  name             TEXT,
  overseer         TEXT,
  overseer_id      INTEGER,
  assistant        TEXT,
  assistant_id     INTEGER,
  phone            TEXT
);
CREATE TABLE IF NOT EXISTS families (
  id                       INTEGER PRIMARY KEY,
  name                     TEXT,
  field_service_group_id   INTEGER,
  field_service_group_name TEXT,
  family_head_id           INTEGER,
  family_head              TEXT,
  address                  TEXT,
  city                     TEXT,
  state                    TEXT,
  postal_code              TEXT,
  moved                    INTEGER
);
CREATE TABLE IF NOT EXISTS persons (
  id                              INTEGER PRIMARY KEY,
  category                        TEXT,
  first_name                      TEXT,
  last_name                       TEXT,
  display_name                    TEXT,
  gender                          TEXT,
  date_of_birth                   TEXT,
  address                         TEXT,
  city                            TEXT,
  state                           TEXT,
  postal_code                     TEXT,
  full_address                    TEXT,
  mobile                          TEXT,
  email                           TEXT,
  congregation_id                 INTEGER,
  field_service_group_id          INTEGER,
  field_service_group_name        TEXT,
  family_id                       INTEGER,
  family_name                     TEXT,
  baptized                        INTEGER,
  date_of_baptism                 TEXT,
  elder                           INTEGER,
  ministerial_servant             INTEGER,
  special_pioneer                 INTEGER,
  pioneer                         INTEGER,
  regular_auxiliary               INTEGER,
  anointed                        INTEGER,
  family_head                     INTEGER,
  clm_student                     INTEGER,
  inactive                        INTEGER,
  infirm                          INTEGER,
  moved                           INTEGER,
  removed                         INTEGER,
  date_removed                    TEXT,
  bind                            INTEGER,
  deaf                            INTEGER,
  child                           INTEGER,
  incarcerated                    INTEGER,
  regular                         INTEGER,
  clm_chairman                    INTEGER,
  clm_auxiliary_counselor         INTEGER,
  prayer                          INTEGER,
  clm_treasures                   INTEGER,
  clm_gems                        INTEGER,
  clm_bible_reading               INTEGER,
  clm_initial_call                INTEGER,
  clm_follow_up                   INTEGER,
  clm_making_disciples            INTEGER,
  clm_explaining_beliefs          INTEGER,
  clm_talk                        INTEGER,
  clm_assistant                   INTEGER,
  clm_living_as_christians_parts  INTEGER,
  clm_cbs_conductor               INTEGER,
  clm_cbs_reader                  INTEGER,
  public_talks_local              INTEGER,
  public_talks_away               INTEGER,
  public_meeting_chairman         INTEGER,
  watchtower_reader               INTEGER,
  public_witnessing               INTEGER,
  public_witnessing_key_person    INTEGER,
  meeting_for_service_conductor   INTEGER,
  meeting_for_service_prayer      INTEGER,
  auditorium_attendant            INTEGER,
  entrance_attendant              INTEGER,
  video_conference_host           INTEGER,
  microphones                     INTEGER,
  av_operator                     INTEGER,
  av_stage                        INTEGER,
  maintenance_volunteer           INTEGER,
  latitude                        REAL,
  longitude                       REAL
);
CREATE TABLE IF NOT EXISTS tags (
  person_id INTEGER,
  type      TEXT,
  name      TEXT,
  value     TEXT
);
CREATE INDEX IF NOT EXISTS idx_tags_pid  ON tags(person_id);
CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
CREATE INDEX IF NOT EXISTS idx_persons_name ON persons(last_name, first_name);
"""


def _csv_to_sqlite(csv_path: Path) -> str:
    """Convert Persons.csv to SQLite; returns a temp file path (caller must delete)."""
    raw = csv_path.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace"))))

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    cur = conn.cursor()
    cur.executescript(_SCHEMA)

    tags: list[tuple] = []
    families: dict = {}
    fs_groups: dict = {}

    # Pass 1 – FSGs, families, overseer/assistant tags
    for row in rows:
        pid = int(row.get("PersonID") or 0) or 0
        fsg_id = int(row.get("FieldServiceGroupID") or 0) or 0
        fsg_name = (row.get("GroupName") or "").strip()
        family_id = int(row.get("FamilyID") or 0) or 0
        is_moved = row.get("Moved") == "True"

        if fsg_id and fsg_id not in fs_groups:
            fs_groups[fsg_id] = dict(
                id=fsg_id, congregation_id=1, name=fsg_name,
                overseer="", overseer_id=0, assistant="", assistant_id=0, phone="",
            )

        if fsg_id in fs_groups and not is_moved:
            resp = (row.get("GroupResponsibility") or "").strip()
            full = f"{(row.get('FirstName') or '').strip()} {(row.get('LastName') or '').strip()}".strip()
            g = fs_groups[fsg_id]
            if resp == "Overseer" and not g["overseer_id"]:
                g["overseer"] = full
                g["overseer_id"] = pid
                g["phone"] = (row.get("PhoneMobile") or "").strip()
                tags.append((pid, "field_service_group", "Field Service Group Overseer", fsg_name))
            elif resp == "Assistant" and not g["assistant_id"]:
                g["assistant"] = full
                g["assistant_id"] = pid
                tags.append((pid, "field_service_group", "Field Service Group Assistant", fsg_name))

        if row.get("FamilyHead") == "True" and family_id > 0 and family_id not in families:
            addr = _parse_address(row.get("Address") or "")
            fname = (row.get("FamilyName") or "").strip() or (row.get("LastName") or "").strip()
            families[family_id] = dict(
                id=family_id, name=fname,
                field_service_group_id=0 if is_moved else fsg_id,
                field_service_group_name="Unassigned" if is_moved else fsg_name,
                family_head_id=pid,
                family_head=f"{(row.get('FirstName') or '').strip()} {(row.get('LastName') or '').strip()}".strip(),
                address=addr["address"], city=addr["city"],
                state=addr["state"], postal_code=addr["postal_code"],
                moved=1 if is_moved else 0,
            )
            tags.append((pid, "family", "Family Head", fname))

    cur.execute("INSERT OR REPLACE INTO congregations VALUES (1, 'Congregation')")
    cur.executemany(
        "INSERT OR REPLACE INTO field_service_groups VALUES (?,?,?,?,?,?,?,?)",
        [(g["id"], g["congregation_id"], g["name"], g["overseer"],
          g["overseer_id"], g["assistant"], g["assistant_id"], g["phone"])
         for g in fs_groups.values()],
    )
    cur.executemany(
        "INSERT OR REPLACE INTO families VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(f["id"], f["name"], f["field_service_group_id"], f["field_service_group_name"],
          f["family_head_id"], f["family_head"], f["address"], f["city"],
          f["state"], f["postal_code"], f["moved"])
         for f in families.values()],
    )

    # Pass 2 – persons + tags
    persons_rows: list[tuple] = []
    for row in rows:
        pid = int(row.get("PersonID") or 0) or 0
        family_id = int(row.get("FamilyID") or 0) or 0
        fsg_id = int(row.get("FieldServiceGroupID") or 0) or 0
        fsg_name = (row.get("GroupName") or "").strip()
        is_moved = row.get("Moved") == "True"
        privilege = (row.get("Privilege") or "").strip()
        pioneer_status = (row.get("PioneerStatus") or "").strip()

        addr = _parse_address(row.get("Address") or "")
        first = (row.get("FirstName") or "").strip()
        last = (row.get("LastName") or "").strip()

        family_name = (row.get("FamilyName") or "").strip()
        if not family_name and family_id in families:
            family_name = families[family_id]["name"]
        if not family_name:
            family_name = last

        eff_fsg_id = 0 if (is_moved or family_id < 1) else fsg_id
        eff_fsg_name = "Unassigned" if (is_moved or family_id < 1) else fsg_name
        eff_fam_id = family_id if family_id > 0 else 0

        elder = 1 if privilege == "E" else 0
        ms = 1 if privilege == "MS" else 0
        baptized = 1 if privilege in ("E", "MS", "PUB") else 0
        pioneer = 1 if pioneer_status == "RegularPioneer" else 0
        reg_aux = 1 if pioneer_status == "AuxiliaryPioneer" else 0
        sp_pioneer = 1 if pioneer_status == "SpecialPioneer" else 0
        inactive = 1 if row.get("Active") == "False" else 0
        category = "associated" if privilege == "No" else "publisher"

        def _lat_lon(v):
            try:
                return float(v) if v and v.strip() else None
            except ValueError:
                return None

        persons_rows.append((
            pid, category, first, last,
            f"{first} {last}".strip(),
            (row.get("Gender") or "").strip(),
            (row.get("DOB") or "").strip(),
            addr["address"], addr["city"], addr["state"], addr["postal_code"],
            f"{addr['address']} {addr['city']}, {addr['state']} {addr['postal_code']}".strip(),
            (row.get("PhoneMobile") or "").strip(),
            (row.get("Email") or "").strip(),
            1, eff_fsg_id, eff_fsg_name, eff_fam_id, family_name,
            baptized, (row.get("DateOfBaptism") or "").strip(),
            elder, ms, sp_pioneer, pioneer, reg_aux,
            1 if row.get("Anointed") == "True" else 0,
            1 if row.get("FamilyHead") == "True" else 0,
            1 if row.get("CLMStudent") == "True" else 0,
            inactive,
            1 if row.get("ElderlyInfirm") == "True" else 0,
            1 if is_moved else 0,
            1 if row.get("Removed") == "True" else 0,
            (row.get("DateOfRemoved") or "").strip(),
            1 if row.get("Blind") == "True" else 0,
            1 if row.get("Deaf") == "True" else 0,
            1 if row.get("Child") == "True" else 0,
            1 if row.get("Incarcerated") == "True" else 0,
            0 if row.get("Regular") == "False" else 1,
            _b(row.get("UseForChairman")),
            _b(row.get("UseForAuxiliaryCounselor")),
            _b(row.get("UseForPrayers")),
            _b(row.get("UseForTreasuresTalk")),
            _b(row.get("UseForTreasuresGems")),
            _b(row.get("UseForTreasuresBR")),
            _b(row.get("UseForApplyIC")),
            _b(row.get("UseForApplyRV")),
            _b(row.get("UseForApplyBS")),
            _b(row.get("UseForApplyExplaining")),
            _b(row.get("UseForApplyStudentTalk")),
            _b(row.get("UseForApplyAssistant")),
            _b(row.get("UseForLivingParts")),
            _b(row.get("UseForCBS")),
            _b(row.get("UseForCBSReader")),
            _b(row.get("UseForPublicTalksLocal")),
            _b(row.get("UseForPublicTalksAway")),
            _b(row.get("UseForWeekendChairman")),
            _b(row.get("UseForWatchtowerReader")),
            _b(row.get("UseForPublicWitnessing")),
            _b(row.get("UseForPublicWitnessingKeyPerson")),
            _b(row.get("UseForConductFSGroups")),
            _b(row.get("UseForFSPrayers")),
            _b(row.get("UseForDuty1")),
            _b(row.get("UseForDuty2")),
            _b(row.get("UseForDuty3")),
            _b(row.get("UseForDuty4")),
            _b(row.get("UseForDuty6")),
            _b(row.get("UseForDuty7")),
            _b(row.get("UseForMaintenance")),
            _lat_lon(row.get("Latitude")),
            _lat_lon(row.get("Longitude")),
        ))

        # tags
        if eff_fsg_id > 0:
            tags.append((pid, "congregation", "Field Service Group", eff_fsg_name))
        gender = (row.get("Gender") or "").strip()
        if gender:
            tags.append((pid, "family", gender, ""))
        if family_name:
            tags.append((pid, "family", "Family", family_name))

        if privilege == "E":
            tags.append((pid, "appointment", "Elder", ""))
        if privilege == "MS":
            tags.append((pid, "appointment", "Ministerial Servant", ""))

        if baptized:
            tags.append((pid, "congregation", "Publisher", ""))
            tags.append((pid, "congregation", "Baptized", ""))
        elif privilege == "UBP":
            tags.append((pid, "congregation", "Unbaptized Publisher", ""))
            tags.append((pid, "congregation", "Publisher", ""))
        elif privilege == "No":
            tags.append((pid, "congregation", "Associated Family", ""))

        if baptized or privilege == "UBP":
            tags.append((pid, "congregation", "Inactive" if inactive else "Active", ""))
            if row.get("Regular") != "False":
                tags.append((pid, "congregation", "Regular", ""))

        if is_moved:
            tags.append((pid, "congregation", "Moved", ""))
        if row.get("Removed") == "True":
            tags.append((pid, "congregation", "Removed", ""))
        if row.get("Incarcerated") == "True":
            tags.append((pid, "congregation", "Incarcerated", ""))
        if pioneer:
            tags.append((pid, "appointment", "Regular Pioneer", ""))
        if reg_aux:
            tags.append((pid, "appointment", "Continuous Auxiliary Pioneer", ""))
        if sp_pioneer:
            tags.append((pid, "appointment", "Special Pioneer", ""))
        if row.get("CLMStudent") == "True":
            tags.append((pid, "student", "OCLM Enrolled", ""))

    placeholders = ",".join(["?"] * 71)
    cur.executemany(f"INSERT OR REPLACE INTO persons VALUES ({placeholders})", persons_rows)
    cur.executemany("INSERT INTO tags VALUES (?,?,?,?)", tags)
    conn.commit()
    conn.close()
    return tmp.name


# ---------------------------------------------------------------------------
# API keys  [{id, name, prefix, hash, scopes, created, expires, last_used, calls}]
# Only a SHA-256 of each key is stored; the key itself is shown once at creation.
# ---------------------------------------------------------------------------
API_KEY_SCOPES = {
    "contact": "Phone and email",
    "address": "Street address, city and ZIP",
    "contact_details": "All contact details: every phone, email and address, birth, baptism, "
                       "appointment, pioneer and removal dates, and personal circumstances",
    "extended_attributes": "How each person is used: meeting parts, assignments and hall duties",
}


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _load_api_keys() -> dict[str, dict]:
    if API_KEYS_FILE.exists():
        return {k["id"]: k for k in json.loads(API_KEYS_FILE.read_text())}
    return {}


def _save_api_keys() -> None:
    tmp = API_KEYS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(list(_api_keys.values()), indent=2))
    os.replace(tmp, API_KEYS_FILE)


def _create_api_key(name: str, scopes: list[str], expires: Optional[str]) -> str:
    key = "pu_" + secrets.token_urlsafe(32)
    key_id = secrets.token_hex(4)
    _api_keys[key_id] = {
        "id": key_id,
        "name": name,
        "prefix": key[:10],
        "hash": _hash_key(key),
        "scopes": [s for s in scopes if s in API_KEY_SCOPES],
        "created": _now(),
        "expires": expires or None,
        "last_used": None,
        "calls": 0,
    }
    _save_api_keys()
    return key


def _check_api_key(key: str) -> Optional[dict]:
    if not key:
        return None
    digest = _hash_key(key)
    for k in _api_keys.values():
        if secrets.compare_digest(k["hash"], digest):
            # A key is valid through the end of its expiry date (UTC)
            if k["expires"] and datetime.now(timezone.utc).date().isoformat() > k["expires"]:
                return None
            return k
    return None


_api_keys: dict[str, dict] = _load_api_keys()


# ---------------------------------------------------------------------------
# MCP endpoint – read-only directory search for remote agents
# ---------------------------------------------------------------------------
# Never exposed, whatever the key's scopes
_HIDDEN_TAGS = {"Incarcerated"}
# Tags that only repeat a field already in the summary
_REDUNDANT_TAGS = {"Family", "Field Service Group"}

_mcp_caller: ContextVar[Optional[dict]] = ContextVar("_mcp_caller", default=None)
_mcp_db_lock = threading.Lock()
_log = logging.getLogger("uvicorn.error")


def _tag_files() -> list[Path]:
    return sorted(UPLOAD_DIR.glob("*.json"))


def _flatten_tag_data(node) -> list[dict]:
    """A tag file holds one tag object, or (bundled exports) a possibly nested list of them."""
    if isinstance(node, list):
        return [t for item in node for t in _flatten_tag_data(item)]
    return [node] if isinstance(node, dict) else []


def _add_custom_tags(db_path: str) -> None:
    """Add the uploaded tag files to the tags table as type 'custom'.

    Mirrors the congregation-directory app: an assignment matches a person by id first,
    then by display name; assignments matching no one in the current CSV are skipped.
    """
    conn = sqlite3.connect(db_path)
    try:
        persons = conn.execute("SELECT id, display_name FROM persons").fetchall()
        valid_ids = {pid for pid, _ in persons}
        name_to_id = {(dn or "").strip().lower(): pid for pid, dn in persons if dn}
        rows: set[tuple] = set()
        for path in _tag_files():
            try:
                tags = _flatten_tag_data(json.loads(path.read_text(encoding="utf-8-sig")))
            except (OSError, ValueError) as e:
                _log.warning("Skipping tag file %s: %s", path.name, e)
                continue
            for tag in tags:
                name = tag.get("tagName")
                if tag.get("version") != 1 or not isinstance(name, str) or not name.strip() \
                        or not isinstance(tag.get("assignments"), list):
                    _log.warning("Skipping invalid tag in %s", path.name)
                    continue
                for a in tag["assignments"]:
                    if not isinstance(a, dict):
                        continue
                    pid = a.get("personId")
                    if pid not in valid_ids:
                        pid = name_to_id.get(str(a.get("name") or "").strip().lower())
                    if pid is not None:
                        rows.add((pid, "custom", name.strip(), ""))
        conn.executemany("INSERT INTO tags VALUES (?,?,?,?)", sorted(rows))
        conn.commit()
    finally:
        conn.close()


# Extended attributes: how each person is used in the congregation. Mirrors the useForMap in
# congregation-directory (same Persons.csv columns, labels and categories). Stored in the MCP DB
# with tag type 'extended' and shown only to keys with the extended_attributes scope.
_EXTENDED_CATEGORIES = {
    "usefor": "Meeting Assignments",
    "treasures": "Treasures from God's Word",
    "student": "OCLM / Student",
    "living": "Living as Christians",
    "cbs": "Congregation Bible Study",
    "public": "Public Meeting",
    "service": "Field Service",
    "duty": "Hall Duties",
}
_EXTENDED_TAGS = [
    ("UseForChairman", "usefor", "Midweek Chairman"),
    ("UseForAuxiliaryCounselor", "usefor", "Midweek Classroom Counselor"),
    ("UseForPrayers", "usefor", "Prayer"),
    ("UseForTreasuresTalk", "treasures", "Treasure from God's Word"),
    ("UseForTreasuresGems", "treasures", "Digging for Spiritual Gems"),
    ("UseForTreasuresBR", "student", "Bible Reading"),
    ("UseForApplyIC", "student", "Initial Call"),
    ("UseForApplyRV", "student", "Follow Up"),
    ("UseForApplyBS", "student", "Making Disciples"),
    ("UseForApplyExplaining", "student", "Explaining Beliefs"),
    ("UseForApplyStudentTalk", "student", "Talks"),
    ("UseForApplyAssistant", "student", "Assistant"),
    ("UseForLivingParts", "living", "Living as Christians Parts"),
    ("UseForCBS", "cbs", "Congregation Bible Study Conductor"),
    ("UseForCBSReader", "cbs", "Congregation Bible Study Reader"),
    ("UseForPublicTalksLocal", "public", "Local Public Talks"),
    ("UseForPublicTalksAway", "public", "Away Public Talks"),
    ("UseForWeekendChairman", "public", "Weekend Chairman"),
    ("UseForWatchtowerReader", "public", "Watchtower Reader"),
    ("UseForPublicWitnessing", "service", "Local Public Witnessing"),
    ("UseForPublicWitnessingKeyPerson", "service", "Local Public Witnessing Key Person"),
    ("UseForConductFSGroups", "service", "Meeting for Field Service Conductor"),
    ("UseForFSPrayers", "service", "Meeting for Field Service Prayer"),
    ("UseForMaintenance", "service", "Local Maintenance Volunteer"),
    ("UseForDuty1", "duty", "Auditorium Attendant"),
    ("UseForDuty2", "duty", "Entrance Attendant"),
    ("UseForDuty3", "duty", "Video Conference Host"),
    ("UseForDuty4", "duty", "Microphone Carrier"),
    ("UseForDuty6", "duty", "Audio/Video Operator"),
    ("UseForDuty7", "duty", "Stage Attendant"),
    ("UseForHospitality", "duty", "Hospitality"),
    ("UseForCleaningType1", "duty", "Weekly Hall Clean"),
    ("UseForCleaningType2", "duty", "After Meeting Clean"),
    ("UseForCleaningType3", "duty", "Monthly Hall Clean"),
    ("UseForCleaningType4", "duty", "Quarterly Hall Clean"),
    ("UseForGardenCareType1", "duty", "Flowerbeds"),
    ("UseForGardenCareType2", "duty", "Lawn"),
]
_EXTENDED_CATEGORY_OF = {label: _EXTENDED_CATEGORIES[cat] for _, cat, label in _EXTENDED_TAGS}


def _add_csv_extras(db_path: str, csv_path: Path) -> None:
    """Add what the MCP needs from Persons.csv beyond the shared schema: the contact-details
    fields with no persons column, and the extended-attribute tags."""
    raw = csv_path.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace"))))
    extras, tags = [], []
    for row in rows:
        pid = int(row.get("PersonID") or 0) or 0
        if not pid:
            continue
        extras.append((
            pid,
            (row.get("PhoneHome") or "").strip(),
            (row.get("PhoneWork") or "").strip(),
            (row.get("Email2") or "").strip(),
            (row.get("DateOfPrivilege") or "").strip(),
            (row.get("DateOfFirstMonth") or "").strip(),
        ))
        for col, _cat, label in _EXTENDED_TAGS:
            if row.get(col) == "True":
                tags.append((pid, "extended", label, ""))
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""CREATE TABLE person_extras (
            person_id INTEGER PRIMARY KEY, phone_home TEXT, phone_work TEXT, email2 TEXT,
            date_of_privilege TEXT, date_of_first_month TEXT)""")
        conn.executemany("INSERT OR REPLACE INTO person_extras VALUES (?,?,?,?,?,?)", extras)
        conn.executemany("INSERT INTO tags VALUES (?,?,?,?)", tags)
        conn.commit()
    finally:
        conn.close()


# Bump when the MCP DB layout changes, so caches built by an older release are rebuilt
_MCP_DB_VERSION = 2


def _mcp_sources_signature() -> str:
    """Changes whenever Persons.csv or any tag file is replaced, added or deleted."""
    parts = [f"v{_MCP_DB_VERSION}"]
    for path in [UPLOAD_DIR / "Persons.csv", *_tag_files()]:
        try:
            st = path.stat()
        except FileNotFoundError:  # deleted since the listing
            continue
        parts.append(f"{path.name}:{st.st_mtime_ns}:{st.st_size}")
    return "|".join(parts)


def _mcp_db() -> sqlite3.Connection:
    """Read-only connection to the cached DB, rebuilt when Persons.csv or a tag file changes."""
    csv_path = UPLOAD_DIR / "Persons.csv"
    if not csv_path.exists():
        raise ToolError("No Persons.csv has been uploaded yet.")
    with _mcp_db_lock:
        signature = _mcp_sources_signature()
        sig_path = MCP_DB_PATH.with_suffix(".sig")
        current = sig_path.read_text() if sig_path.exists() else None
        if not MCP_DB_PATH.exists() or current != signature:
            tmp_path = _csv_to_sqlite(csv_path)
            _add_csv_extras(tmp_path, csv_path)
            _add_custom_tags(tmp_path)
            staged = MCP_DB_PATH.with_suffix(".tmp")
            shutil.move(tmp_path, staged)
            os.replace(staged, MCP_DB_PATH)
            sig_path.write_text(signature)
    conn = sqlite3.connect(f"file:{MCP_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _mcp_scopes() -> set[str]:
    key = _mcp_caller.get()
    return set(key["scopes"]) if key else set()


def _tag_type_filter(alias: str = "") -> str:
    """SQL condition hiding extended-attribute tags from keys without that scope."""
    if "extended_attributes" in _mcp_scopes():
        return "1"
    return f"{alias}type != 'extended'"


def _log_call(tool: str, **args) -> None:
    key = _mcp_caller.get()
    if key is not None:
        key["calls"] = key.get("calls", 0) + 1
        key["last_used"] = _now()
        _save_api_keys()
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "key_id": key["id"] if key else None,
        "key_name": key["name"] if key else None,
        "tool": tool,
        "args": args,
    }
    with MCP_CALL_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    _log.info("MCP %s by %s %s", tool, entry["key_name"], json.dumps(args))


def _tags_for(conn: sqlite3.Connection, ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    if not ids:
        return out
    rows = conn.execute(
        f"SELECT person_id, name, value FROM tags WHERE person_id IN ({','.join('?' * len(ids))}) "
        f"AND {_tag_type_filter()}",
        ids,
    )
    for r in rows:
        if r["name"] in _HIDDEN_TAGS or r["name"] in _REDUNDANT_TAGS:
            continue
        label = f"{r['name']}: {r['value']}" if r["value"] else r["name"]
        tags = out.setdefault(r["person_id"], [])
        if label not in tags:
            tags.append(label)
    return out


def _person_summaries(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    tags = _tags_for(conn, [r["id"] for r in rows])
    out = []
    for r in rows:
        p = {
            "id": r["id"],
            "name": r["display_name"],
            "family_id": r["family_id"] or None,
            "family": r["family_name"],
            "group": r["field_service_group_name"],
            "tags": tags.get(r["id"], []),
        }
        if r["moved"]:
            p["moved"] = True
        if r["removed"]:
            p["removed"] = True
        out.append(p)
    return out


_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

_mcp = MCPServer(
    name="persons-directory",
    title="Congregation Directory",
    instructions=(
        "Read-only search over the congregation directory, built from the latest Persons.csv. "
        "Start with search_persons, search_families or list_field_service_groups, then use get_person "
        "or get_family for details. search_families answers family-level questions (e.g. families with "
        "an elder, and how many people are in them) in one call instead of one get_family per family. "
        "People who have moved away or been removed are left out unless include_moved "
        "is true. Tags include custom tags uploaded by the congregation (marked custom in list_tags). "
        "Phone, email and address appear only when this API key has that scope; the contact_details "
        "scope adds personal dates and circumstances, and extended_attributes adds tags for how each "
        "person is used (meeting parts, assignments, hall duties)."
    ),
)


@_mcp.tool(annotations=_READ_ONLY)
def search_persons(
    query: str = "",
    group: Optional[str] = None,
    tag: Optional[str] = None,
    include_moved: bool = False,
    limit: int = 25,
) -> dict:
    """Search people by name, optionally filtered by field service group and tag.

    query: words matched against first, last and display name (every word must match).
    group: exact field service group name, e.g. "Gruber" (see list_field_service_groups).
    tag: exact tag name, e.g. "Elder", "Regular Pioneer", "Unbaptized Publisher" (see list_tags).
    include_moved: include people who have moved away or been removed.
    limit: maximum results to return (1-100).
    """
    _log_call("search_persons", query=query, group=group, tag=tag, include_moved=include_moved, limit=limit)
    if tag in _HIDDEN_TAGS:
        return {"total": 0, "results": []}
    where, params = [], []
    for word in query.split():
        where.append("(first_name LIKE ? OR last_name LIKE ? OR display_name LIKE ?)")
        params += [f"%{word}%"] * 3
    if group:
        where.append("field_service_group_name = ? COLLATE NOCASE")
        params.append(group)
    if tag:
        where.append(f"id IN (SELECT person_id FROM tags WHERE name = ? COLLATE NOCASE AND {_tag_type_filter()})")
        params.append(tag)
    if not include_moved:
        where.append("moved = 0 AND removed = 0")
    sql = "SELECT * FROM persons"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_name, first_name"
    with closing(_mcp_db()) as conn:
        rows = conn.execute(sql, params).fetchall()
        limit = max(1, min(limit, 100))
        return {"total": len(rows), "results": _person_summaries(conn, rows[:limit])}


@_mcp.tool(annotations=_READ_ONLY)
def get_person(person_id: int) -> dict:
    """Get one person's details: family, group and tags. Depending on this API key's scopes it
    also includes phone and email (contact), address (address), or every phone, email and address
    plus birth, baptism, appointment, pioneer and removal dates and personal circumstances
    (contact_details)."""
    _log_call("get_person", person_id=person_id)
    scopes = _mcp_scopes()
    with closing(_mcp_db()) as conn:
        row = conn.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()
        if row is None:
            raise ToolError(f"No person with id {person_id}.")
        person = _person_summaries(conn, [row])[0]
        extras = conn.execute("SELECT * FROM person_extras WHERE person_id = ?", (person_id,)).fetchone()
    person["first_name"] = row["first_name"]
    person["last_name"] = row["last_name"]
    person["family_head"] = bool(row["family_head"])
    full = "contact_details" in scopes
    if full or "contact" in scopes:
        person["mobile"] = row["mobile"] or None
        person["email"] = row["email"] or None
    if full or "address" in scopes:
        person["address"] = row["address"] or None
        person["city"] = row["city"] or None
        person["state"] = row["state"] or None
        person["postal_code"] = row["postal_code"] or None
    if full:
        person["phone_home"] = (extras["phone_home"] if extras else "") or None
        person["phone_work"] = (extras["phone_work"] if extras else "") or None
        person["email2"] = (extras["email2"] if extras else "") or None
        person["gender"] = row["gender"] or None
        person["date_of_birth"] = row["date_of_birth"] or None
        person["date_of_baptism"] = row["date_of_baptism"] or None
        person["date_of_appointment"] = (extras["date_of_privilege"] if extras else "") or None
        person["pioneer_start_date"] = (extras["date_of_first_month"] if extras else "") or None
        person["date_removed"] = row["date_removed"] or None
        person["anointed"] = bool(row["anointed"])
        person["elderly_infirm"] = bool(row["infirm"])
        person["blind"] = bool(row["bind"])
        person["deaf"] = bool(row["deaf"])
        person["child"] = bool(row["child"])
    return person


@_mcp.tool(annotations=_READ_ONLY)
def search_families(
    query: str = "",
    group: Optional[str] = None,
    member_tag: Optional[str] = None,
    include_moved: bool = False,
    limit: int = 50,
) -> dict:
    """Search families, optionally keeping only those with a member who has a given tag.

    query: words matched against the family name (every word must match).
    group: exact field service group name of the family, e.g. "Gruber".
    member_tag: exact tag name, e.g. "Elder" or "Regular Pioneer"; keeps families with at least
        one current member carrying it, and lists those members in matched_members.
    include_moved: include members who have moved away or been removed, in matching and counts.
    limit: maximum families to return (1-200).

    Each result has member_count, equal to the number of members get_family returns with the same
    include_moved. total_families and total_members cover every match, not just the returned page.
    To combine tags (e.g. elder OR regular pioneer), call once per tag and merge on family id.
    """
    _log_call("search_families", query=query, group=group, member_tag=member_tag,
              include_moved=include_moved, limit=limit)
    if member_tag in _HIDDEN_TAGS:
        return {"total_families": 0, "total_members": 0, "results": []}
    # Families are built from their members, so a family whose head has no row in the
    # families table is still found, and counts always agree with get_family.
    member_filter = "family_id > 0" + ("" if include_moved else " AND moved = 0 AND removed = 0")
    where, params = [], []
    for word in query.split():
        where.append("name LIKE ?")
        params.append(f"%{word}%")
    if group:
        where.append("grp = ? COLLATE NOCASE")
        params.append(group)
    sql = f"""
        WITH m AS (SELECT * FROM persons WHERE {member_filter}),
        fam AS (
            SELECT m.family_id AS id,
                   COALESCE(f.name, MAX(m.family_name)) AS name,
                   f.family_head AS head, f.family_head_id AS head_id,
                   COALESCE(f.field_service_group_name, MAX(m.field_service_group_name)) AS grp,
                   f.moved AS moved,
                   COUNT(*) AS member_count
              FROM m LEFT JOIN families f ON f.id = m.family_id
             GROUP BY m.family_id
        )
        SELECT * FROM fam"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name, id"
    with closing(_mcp_db()) as conn:
        families = conn.execute(sql, params).fetchall()
        matched: dict[int, list[dict]] = {}
        if member_tag:
            rows = conn.execute(
                f"""SELECT id, display_name, family_id FROM persons
                     WHERE {member_filter}
                       AND id IN (SELECT person_id FROM tags
                                   WHERE name = ? COLLATE NOCASE AND {_tag_type_filter()})
                     ORDER BY first_name""",
                (member_tag,),
            ).fetchall()
            for r in rows:
                matched.setdefault(r["family_id"], []).append({"id": r["id"], "name": r["display_name"]})
            families = [f for f in families if f["id"] in matched]
    limit = max(1, min(limit, 200))
    results = []
    for f in families[:limit]:
        fam = {
            "id": f["id"],
            "name": f["name"],
            "head_id": f["head_id"],
            "head": f["head"],
            "group": f["grp"],
            "member_count": f["member_count"],
        }
        if f["moved"]:
            fam["moved"] = True
        if member_tag:
            fam["matched_members"] = matched[f["id"]]
        results.append(fam)
    return {
        "total_families": len(families),
        "total_members": sum(f["member_count"] for f in families),
        "results": results,
    }


@_mcp.tool(annotations=_READ_ONLY)
def get_family(family_id: int, include_moved: bool = False) -> dict:
    """Get a family: its name, head, field service group and members, plus its address
    when this API key has the address or contact_details scope."""
    _log_call("get_family", family_id=family_id, include_moved=include_moved)
    with closing(_mcp_db()) as conn:
        fam = conn.execute("SELECT * FROM families WHERE id = ?", (family_id,)).fetchone()
        sql = "SELECT * FROM persons WHERE family_id = ?"
        if not include_moved:
            sql += " AND moved = 0 AND removed = 0"
        members = conn.execute(sql + " ORDER BY family_head DESC, first_name", (family_id,)).fetchall()
        if fam is None and not members:
            raise ToolError(f"No family with id {family_id}.")
        family = {
            "id": family_id,
            "name": fam["name"] if fam else members[0]["family_name"],
            "head_id": fam["family_head_id"] if fam else None,
            "head": fam["family_head"] if fam else None,
            "group": fam["field_service_group_name"] if fam else members[0]["field_service_group_name"],
            "members": _person_summaries(conn, members),
        }
    if fam is not None and fam["moved"]:
        family["moved"] = True
    if fam is not None and _mcp_scopes() & {"address", "contact_details"}:
        family["address"] = fam["address"] or None
        family["city"] = fam["city"] or None
        family["state"] = fam["state"] or None
        family["postal_code"] = fam["postal_code"] or None
    return family


@_mcp.tool(annotations=_READ_ONLY)
def list_field_service_groups() -> dict:
    """List field service groups with their overseer, assistant and number of members
    (people who have moved away are not counted)."""
    _log_call("list_field_service_groups")
    with closing(_mcp_db()) as conn:
        rows = conn.execute(
            """SELECT g.id, g.name, g.overseer, g.overseer_id, g.assistant, g.assistant_id,
                      (SELECT COUNT(*) FROM persons p
                        WHERE p.field_service_group_id = g.id AND p.moved = 0 AND p.removed = 0) AS members
                 FROM field_service_groups g ORDER BY g.name"""
        ).fetchall()
    return {"groups": [
        {
            "id": r["id"],
            "name": r["name"],
            "overseer": r["overseer"] or None,
            "overseer_id": r["overseer_id"] or None,
            "assistant": r["assistant"] or None,
            "assistant_id": r["assistant_id"] or None,
            "members": r["members"],
        }
        for r in rows
    ]}


@_mcp.tool(annotations=_READ_ONLY)
def list_tags() -> dict:
    """List the tag names usable with search_persons and search_families, with how many current
    members have each. Tags marked custom come from tag files uploaded by the congregation
    (e.g. "English Elders") rather than from Persons.csv. With the extended_attributes scope this
    also lists how people are used (meeting parts, assignments, hall duties), each with its
    category."""
    _log_call("list_tags")
    hidden = _HIDDEN_TAGS | _REDUNDANT_TAGS
    with closing(_mcp_db()) as conn:
        rows = conn.execute(
            f"""SELECT t.name, MAX(t.type = 'custom') AS custom, MAX(t.type = 'extended') AS extended,
                       COUNT(DISTINCT t.person_id) AS people
                  FROM tags t JOIN persons p ON p.id = t.person_id
                 WHERE p.moved = 0 AND p.removed = 0 AND {_tag_type_filter('t.')}
                   AND t.name NOT IN ({','.join('?' * len(hidden))})
                 GROUP BY t.name ORDER BY t.name""",
            sorted(hidden),
        ).fetchall()
    tags = []
    for r in rows:
        tag = {"tag": r["name"], "people": r["people"]}
        if r["custom"]:
            tag["custom"] = True
        if r["extended"]:
            tag["category"] = _EXTENDED_CATEGORY_OF.get(r["name"])
        tags.append(tag)
    return {"tags": tags}


class _MCPKeyAuth:
    """ASGI wrapper: requires `Authorization: Bearer <api key>` on every MCP request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            header = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
            scheme, _, token = header.partition(" ")
            key = _check_api_key(token.strip()) if scheme.lower() == "bearer" else None
            if key is None:
                response = JSONResponse(
                    {"error": "invalid_token", "error_description": "A valid API key is required."},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="persons-uploader"'},
                )
                await response(scope, receive, send)
                return
            _mcp_caller.set(key)
        await self.app(scope, receive, send)


# Stateless JSON responses: no sessions to lose on restart, and each call stands alone.
# host="0.0.0.0" skips the SDK's localhost-only Host check; the API key is the access control.
_mcp_http_app = _mcp.streamable_http_app(
    streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0"
)
app.router.routes.append(Route("/mcp", endpoint=_MCPKeyAuth(_mcp_http_app)))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _file_state() -> dict:
    return {
        "persons_exists": (UPLOAD_DIR / "Persons.csv").exists(),
        "tag_files": sorted(f.name for f in UPLOAD_DIR.glob("*.json")),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(require_auth)):
    return templates.TemplateResponse(request, "index.html", {"user": user, **_file_state()})


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    user: dict = Depends(require_upload),
):
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted.")

    content_type = file.content_type or ""
    allowed_types = {"text/csv", "application/csv", "text/plain", "application/octet-stream"}
    if content_type and content_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"Invalid content type: {content_type}")

    contents = await file.read()
    (UPLOAD_DIR / "Persons.csv").write_bytes(contents)

    return JSONResponse({"message": f"Persons.csv saved successfully ({len(contents):,} bytes)."})


@app.post("/upload/tags")
async def upload_tags(
    files: list[UploadFile] = File(...),
    user: dict = Depends(require_upload),
):
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    saved = []
    for file in files:
        filename = Path(file.filename or "").name
        if not filename.lower().endswith(".json"):
            raise HTTPException(status_code=400, detail=f"Only JSON files are accepted (got: {filename}).")
        contents = await file.read()
        try:
            json.loads(contents)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail=f"{filename} is not valid JSON.")
        (UPLOAD_DIR / filename).write_bytes(contents)
        saved.append(filename)

    return JSONResponse({"message": f"{len(saved)} tag file(s) saved.", "files": saved})


@app.delete("/upload/persons")
async def delete_persons(user: dict = Depends(require_upload)):
    dest = UPLOAD_DIR / "Persons.csv"
    if not dest.exists():
        raise HTTPException(status_code=404, detail="Persons.csv not found.")
    dest.unlink()
    return JSONResponse({"message": "Persons.csv deleted."})


@app.delete("/upload/tags/{filename}")
async def delete_tag(filename: str, user: dict = Depends(require_upload)):
    safe_name = Path(filename).name
    if not safe_name.lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    dest = UPLOAD_DIR / safe_name
    if not dest.exists():
        raise HTTPException(status_code=404, detail=f"{safe_name} not found.")
    dest.unlink()
    return JSONResponse({"message": f"{safe_name} deleted."})


_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/download")
async def download(
    user: dict = Depends(require_auth),
    tags: Optional[str] = Query(default=None),
):
    if tags is not None:
        tag_files = sorted(UPLOAD_DIR.glob("*.json"))
        if not tag_files:
            raise HTTPException(status_code=404, detail="No tag files found.")
        combined = [json.loads(f.read_text()) for f in tag_files]
        return JSONResponse(
            combined,
            headers={**_NO_CACHE_HEADERS, "Content-Disposition": 'attachment; filename="tags.json"'},
        )

    dest = UPLOAD_DIR / "Persons.csv"
    if not dest.exists():
        raise HTTPException(status_code=404, detail="Persons.csv has not been uploaded yet.")
    return FileResponse(
        dest,
        media_type="text/csv",
        filename="Persons.csv",
        headers=_NO_CACHE_HEADERS,
    )


@app.get("/download/database")
async def download_database(user: dict = Depends(require_auth)):
    csv_path = UPLOAD_DIR / "Persons.csv"
    if not csv_path.exists():
        raise HTTPException(status_code=404, detail="Persons.csv has not been uploaded yet.")
    tmp_path = _csv_to_sqlite(csv_path)
    return FileResponse(
        tmp_path,
        media_type="application/octet-stream",
        filename="persons.db",
        headers=_NO_CACHE_HEADERS,
        background=BackgroundTask(os.unlink, tmp_path),
    )


# ---------------------------------------------------------------------------
# Admin – user management
# ---------------------------------------------------------------------------
def _render_admin(request: Request, user: dict, error=None, success=None, new_key=None):
    return templates.TemplateResponse(request, "admin.html", {
        "user": user,
        "users": _users,
        "api_keys": sorted(_api_keys.values(), key=lambda k: k["created"]),
        "scope_labels": API_KEY_SCOPES,
        "new_key": new_key,
        "today": datetime.now(timezone.utc).date().isoformat(),
        "error": error,
        "success": success,
    })


@app.get("/admin", response_class=HTMLResponse)
async def admin_get(request: Request, user: dict = Depends(require_admin)):
    return _render_admin(request, user)


@app.post("/admin/users/add", response_class=HTMLResponse)
async def admin_add_user(
    request: Request,
    user: dict = Depends(require_admin),
    new_username: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    can_upload: Optional[str] = Form(default=None),
):
    def render(error=None, success=None):
        return _render_admin(request, user, error=error, success=success)

    new_username = new_username.strip()
    if not new_username:
        return render(error="Username cannot be blank.")
    if new_username in _users:
        return render(error=f"Username '{new_username}' already exists.")
    if len(new_password) < 8:
        return render(error="Password must be at least 8 characters.")
    if new_password != confirm_password:
        return render(error="Passwords do not match.")

    _users[new_username] = {"password": new_password, "can_upload": can_upload is not None}
    _save_users()
    return render(success=f"User '{new_username}' added.")


@app.post("/admin/users/{target_username}/delete")
async def admin_delete_user(
    target_username: str,
    user: dict = Depends(require_admin),
):
    if target_username not in _users:
        raise HTTPException(status_code=404, detail="User not found.")
    if _users[target_username]["can_upload"]:
        remaining = sum(1 for u, v in _users.items() if v["can_upload"] and u != target_username)
        if remaining == 0:
            raise HTTPException(status_code=400, detail="Cannot delete the last user with upload access.")
    del _users[target_username]
    _save_users()
    # If the user deleted their own account, send them to / which will trigger a re-auth prompt
    redirect_to = "/" if target_username == user["username"] else "/admin"
    return RedirectResponse(redirect_to, status_code=303)


@app.get("/admin/users/{target_username}/edit", response_class=HTMLResponse)
async def admin_edit_get(
    target_username: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    if target_username not in _users:
        raise HTTPException(status_code=404, detail="User not found.")
    return templates.TemplateResponse(request, "edit_user.html", {
        "user": user,
        "target_username": target_username,
        "target_user": _users[target_username],
        "error": None,
        "success": False,
    })


@app.post("/admin/users/{target_username}/edit", response_class=HTMLResponse)
async def admin_edit_post(
    target_username: str,
    request: Request,
    user: dict = Depends(require_admin),
    new_password: str = Form(default=""),
    confirm_password: str = Form(default=""),
    can_upload: Optional[str] = Form(default=None),
):
    if target_username not in _users:
        raise HTTPException(status_code=404, detail="User not found.")

    def render(error=None, success=False):
        return templates.TemplateResponse(request, "edit_user.html", {
            "user": user,
            "target_username": target_username,
            "target_user": _users[target_username],
            "error": error,
            "success": success,
        })

    can_upload_bool = can_upload is not None

    # Guard: don't remove the last admin
    if not can_upload_bool and _users[target_username]["can_upload"]:
        remaining = sum(1 for u, v in _users.items() if v["can_upload"] and u != target_username)
        if remaining == 0:
            return render(error="Cannot remove upload access from the last admin user.")

    if new_password:
        if len(new_password) < 8:
            return render(error="Password must be at least 8 characters.")
        if new_password != confirm_password:
            return render(error="Passwords do not match.")
        _users[target_username]["password"] = new_password

    _users[target_username]["can_upload"] = can_upload_bool
    _save_users()
    return render(success=True)


# ---------------------------------------------------------------------------
# Admin – API keys for the MCP endpoint
# ---------------------------------------------------------------------------
@app.post("/admin/keys/add", response_class=HTMLResponse)
async def admin_add_key(
    request: Request,
    user: dict = Depends(require_admin),
    key_name: str = Form(...),
    expires: str = Form(default=""),
    scopes: list[str] = Form(default=[]),
):
    key_name = key_name.strip()
    if not key_name:
        return _render_admin(request, user, error="Key name cannot be blank.")
    expires = expires.strip()
    if expires:
        try:
            datetime.strptime(expires, "%Y-%m-%d")
        except ValueError:
            return _render_admin(request, user, error="Expiry must be a date (YYYY-MM-DD).")
    key = _create_api_key(key_name, scopes, expires)
    return _render_admin(request, user, success=f"API key '{key_name}' created.",
                         new_key={"name": key_name, "key": key})


@app.post("/admin/keys/{key_id}/rotate", response_class=HTMLResponse)
async def admin_rotate_key(key_id: str, request: Request, user: dict = Depends(require_admin)):
    old = _api_keys.get(key_id)
    if old is None:
        raise HTTPException(status_code=404, detail="API key not found.")
    key = _create_api_key(old["name"], old["scopes"], old["expires"])
    del _api_keys[key_id]
    _save_api_keys()
    return _render_admin(request, user, success=f"API key '{old['name']}' rotated; the old key no longer works.",
                         new_key={"name": old["name"], "key": key})


@app.post("/admin/keys/{key_id}/revoke")
async def admin_revoke_key(key_id: str, user: dict = Depends(require_admin)):
    if key_id not in _api_keys:
        raise HTTPException(status_code=404, detail="API key not found.")
    del _api_keys[key_id]
    _save_api_keys()
    return RedirectResponse("/admin", status_code=303)
