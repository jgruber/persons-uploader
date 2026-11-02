# Persons Uploader

A web-based file management service for uploading and sharing `Persons.csv` and JSON tag files, with multi-user support and role-based access control.

> This service is designed to work alongside the [Congregation Directory](https://github.com/jgruber/congregation-directory) application, which consumes the files managed here.

## Features

- Upload and manage a `Persons.csv` file
- Upload and manage multiple JSON tag files
- Download files (with no-cache headers for always-fresh data)
- Role-based access: **Uploaders** (full access) and **Viewers** (download only)
- User management admin panel
- Emergency contacts from `Emergency-Contacts.csv` (`POST /upload?emergency_contacts`), linked to
  persons in an `emergency_contacts` table - see [Emergency contacts](#emergency-contacts)
- Monthly change history: every upload is compared with the last, and what changed among the
  persons (added, moved, removed, appointments, baptisms, activity, group, address, phone, anointed)
  is kept for 48 months - see [Change history](#change-history)
- Nightly encrypted backup of the history, logins, API keys and tag files to a NAS folder
- Read-only MCP endpoint (`/mcp`) so remote agents can search the directory, with API keys managed in the admin panel
- Drag-and-drop upload UI with progress tracking

## Roles

| Role | Upload | Download | Delete | Admin |
|------|--------|----------|--------|-------|
| Uploader | Yes | Yes | Yes | Yes |
| Viewer | No | Yes | No | No |

## Configuration

Copy `.env.example` to `.env` and set the initial admin credentials:

```env
AUTH_USERNAME=admin
AUTH_PASSWORD=changeme
```

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_USERNAME` | `admin` | Initial admin username |
| `AUTH_PASSWORD` | `changeme` | Initial admin password |
| `UPLOAD_DIR` | `./uploads` | Directory for stored files |
| `MCP_STATE_DIR` | `$UPLOAD_DIR/.mcp` | API keys, cached directory DB and MCP call log |

> **Note:** Change the default password before exposing the service publicly.

## Deployment

### Docker Compose (recommended)

```bash
cp .env.example .env
# Edit .env with your desired credentials

docker-compose up --build -d
```

Service is available at `http://localhost:8000`.

Uploaded files are persisted to `./uploads/` and user accounts to `./credentials.json` on the host via volume mounts.

### Local Development

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

## API Endpoints

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `GET` | `/` | Any | Main UI |
| `POST` | `/upload` | Uploader | Upload `Persons.csv` |
| `POST` | `/upload/tags` | Uploader | Upload JSON tag files |
| `DELETE` | `/upload/persons` | Uploader | Delete `Persons.csv` |
| `DELETE` | `/upload/tags/{filename}` | Uploader | Delete a tag file |
| `GET` | `/download` | Any | Download `Persons.csv` |
| `GET` | `/download?tags` | Any | Download combined `tags.json` |
| `POST` | `/upload?emergency_contacts` | Uploader | Upload `Emergency-Contacts.csv` (notes dropped) |
| `DELETE` | `/upload/emergency_contacts` | Uploader | Delete the emergency contacts |
| `GET` | `/download?emergency_contacts` | Any | Emergency contacts linked to persons (JSON) |
| `GET` | `/admin` | Uploader | User management |
| `POST` | `/admin/users/add` | Uploader | Create user |
| `POST` | `/admin/users/{username}/delete` | Uploader | Delete user |
| `GET/POST` | `/admin/users/{username}/edit` | Uploader | Edit user |
| `GET` | `/download/database` | Any | Download the directory as SQLite |
| `POST` | `/admin/keys/add` | Uploader | Create an API key (shown once) |
| `POST` | `/admin/keys/{id}/rotate` | Uploader | Replace a key, keeping its name and scopes |
| `POST` | `/admin/keys/{id}/revoke` | Uploader | Revoke a key |
| `POST` | `/mcp` | API key | MCP endpoint (streamable HTTP) |
| `GET` | `/changes?month=YYYY-MM` | Any | Monthly changes page |
| `GET` | `/changes/report?month=YYYY-MM&format=pdf\|md\|json` | Any | Monthly changes download |

## Emergency contacts

`POST /upload?emergency_contacts` takes the New World Scheduler `Emergency-Contacts.csv`. Only each
contact's name, relationship, email, mobile and phone are kept, in the export's `Order`; **the
Notes column is dropped before the file is written**. Each upload URL checks the file's columns,
so `Persons.csv` and `Emergency-Contacts.csv` can never overwrite one another.

The export's `PublisherID` is mostly a different numbering from `PersonID`, so a contact's person
is found by `PublisherID` when it is a PersonID with the same name, otherwise by a unique first +
last name. People who share a name are listed on the Admin page instead of guessed, and exact
repeats (one person exported under two PublisherIDs) are kept once.

```sql
SELECT p.display_name, e.contact_order, e.name, e.relationship, e.mobile, e.phone, e.email
  FROM emergency_contacts e JOIN persons p ON p.id = e.person_id
 ORDER BY p.last_name, p.first_name, e.contact_order;
```

The table is in `/download/database`; `GET /download?emergency_contacts` returns the linked contacts
as JSON for congregation-directory; agents get them in `get_person` with the `contact_details`
scope.

## Change history

`history.py` compares each uploaded `Persons.csv` with the last state it recorded (by PersonID) and
keeps two tables in `$UPLOAD_DIR/.history/history.sqlite`:

- `person_versions` - one row per person per stretch of time in which the tracked fields stayed the
  same (`valid_from`..`valid_to`). The source of truth: what a person looked like on any day.
- `person_changes` - one row per change: who, what, the day it was detected, the day it took effect
  when the export says (baptism, appointment, pioneer and removal dates), old and new values.

| Section | Change types |
|---------|--------------|
| Added | `added`, `returned_to_export` |
| Moved | `moved`, `moved_back` |
| Removed | `removed`, `reinstated`, `dropped_from_export` (no longer in the export at all) |
| Appointments | `appointed_elder`, `appointed_ms`, `appointed_regular_pioneer`, `appointed_special_pioneer` |
| Appointments ended | `no_longer_elder`, `no_longer_ms`, `regular_pioneer_ended`, `special_pioneer_ended` |
| Newly baptized | `baptized` |
| Publishers | `new_publisher` (Privilege No -> UBP), `reactivated`, `became_inactive` |
| Field service groups / Addresses / Phone numbers | `group_changed`, `address_changed`, `phone_changed` |
| Anointed | `anointed_changed` |

The first import is a baseline. An identical file on a new night is recorded as a night checked
with no changes. Anything older than 48 months is deleted on each import. **Monthly changes** on the
home page shows a month with PDF, Markdown and JSON downloads; agents use `person_changes` and
`person_change_history` (address and phone values need the `address` / `contact` scope; dates and
anointed status need `contact_details`). Seed it from archived exports, oldest first:

```bash
python history.py backfill archive/2026-10-26_Persons.csv archive/2026-10-27_Persons.csv ...
```

## Backups

`backup.py` seals the history, `credentials.json`, the API keys, the tag files and the latest CSV
with a passphrase (scrypt, then AES-256-GCM). `deploy/persons-uploader-backup` runs nightly on the
host at 02:45 Central (after the upload), writes to `/media/docs/persons-uploader`, verifies each
file and keeps 30. It shares Shepherd's Notebook's passphrase file
(`shepherd-backup set-passphrase`).

```bash
install -m 0755 deploy/persons-uploader-backup /usr/local/bin/
install -m 0644 deploy/persons-uploader-backup.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now persons-uploader-backup.timer

persons-uploader-backup restore /media/docs/persons-uploader/persons-uploader-....pubackup
```

## MCP Endpoint

`/mcp` serves a read-only MCP server over streamable HTTP (stateless, JSON responses). Every request
needs `Authorization: Bearer <api key>`; create keys under **API Keys** on the admin page. Only a
SHA-256 hash of each key is stored, so the key is shown once — rotate it if it is lost.

Tools: `search_persons`, `search_families`, `get_person`, `get_family`, `list_field_service_groups`,
`list_tags`. `search_families` filters families by name, group or a member tag (e.g. families with
an elder) and returns each family's `member_count`, so family-level questions take one call per tag.
People who have moved or been removed are left out unless `include_moved` is set.

Every key sees names, families, field service groups and tags. Scopes add more:

| Scope | Adds |
|-------|------|
| `contact` | Phone and email (`get_person`) |
| `address` | Street address, city and ZIP (`get_person`, `get_family`) |
| `contact_details` | Everything in `contact` and `address`, plus home and work phones, second email, gender, dates of birth, baptism, appointment, pioneer start and removal, and the anointed, elderly/infirm, blind, deaf and child flags |
| `extended_attributes` | Tags for how each person is used — meeting parts, student assignments, public meeting, field service and hall duties (same labels and categories as congregation-directory) — in all tag lists and filters |

Uploaded tag files (`tag_*.json`) are included as custom tags: they appear in `list_tags` marked
`custom`, work with `search_persons(tag=...)` and `search_families(member_tag=...)`, and show in each
person's tags. Assignments match by person id, then by display name, as in congregation-directory;
`/download/database` is unchanged and still contains only the CSV-derived tags.

Coordinates, the Notes field and the Incarcerated tag are never exposed. The directory DB is rebuilt
from `Persons.csv` and the tag files on the first call after any of them is uploaded or deleted. Each tool call is logged with its key to
`$MCP_STATE_DIR/calls.log` and the container log.

## Project Structure

```
.
├── main.py              # FastAPI application
├── requirements.txt     # Python dependencies
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── templates/
│   ├── index.html       # Main upload/download UI
│   ├── admin.html       # User management
│   └── edit_user.html   # Edit user form
├── static/
│   └── favicon.svg
├── uploads/             # Stored files (git-ignored)
└── credentials.json     # User database (git-ignored)
```

## Tech Stack

- **Python 3.12** + **FastAPI**
- **Uvicorn** (ASGI server)
- **Jinja2** templates + Tailwind CSS
- **Docker** / Docker Compose
