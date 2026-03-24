import csv
import io
import json
import os
import re
import secrets
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from starlette.background import BackgroundTask
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

app = FastAPI(title="Persons Uploader")
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
@app.get("/admin", response_class=HTMLResponse)
async def admin_get(request: Request, user: dict = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin.html", {
        "user": user,
        "users": _users,
        "error": None,
        "success": None,
    })


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
        return templates.TemplateResponse(request, "admin.html", {
            "user": user,
            "users": _users,
            "error": error,
            "success": success,
        })

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
