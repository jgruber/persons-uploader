# Persons Uploader

A web-based file management service for uploading and sharing `Persons.csv` and JSON tag files, with multi-user support and role-based access control.

> This service is designed to work alongside the [Congregation Directory](https://github.com/jgruber/congregation-directory) application, which consumes the files managed here.

## Features

- Upload and manage a `Persons.csv` file
- Upload and manage multiple JSON tag files
- Download files (with no-cache headers for always-fresh data)
- Role-based access: **Uploaders** (full access) and **Viewers** (download only)
- User management admin panel
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
| `GET` | `/admin` | Uploader | User management |
| `POST` | `/admin/users/add` | Uploader | Create user |
| `POST` | `/admin/users/{username}/delete` | Uploader | Delete user |
| `GET/POST` | `/admin/users/{username}/edit` | Uploader | Edit user |
| `GET` | `/download/database` | Any | Download the directory as SQLite |
| `POST` | `/admin/keys/add` | Uploader | Create an API key (shown once) |
| `POST` | `/admin/keys/{id}/rotate` | Uploader | Replace a key, keeping its name and scopes |
| `POST` | `/admin/keys/{id}/revoke` | Uploader | Revoke a key |
| `POST` | `/mcp` | API key | MCP endpoint (streamable HTTP) |

## MCP Endpoint

`/mcp` serves a read-only MCP server over streamable HTTP (stateless, JSON responses). Every request
needs `Authorization: Bearer <api key>`; create keys under **API Keys** on the admin page. Only a
SHA-256 hash of each key is stored, so the key is shown once — rotate it if it is lost.

Tools: `search_persons`, `get_person`, `get_family`, `list_field_service_groups`, `list_tags`.
People who have moved or been removed are left out unless `include_moved` is set.

Every key sees names, families, field service groups and tags. Scopes add more:

| Scope | Adds |
|-------|------|
| `contact` | Phone and email (`get_person`) |
| `address` | Street address, city and ZIP (`get_person`, `get_family`) |

Birth dates, coordinates and the Incarcerated tag are never exposed. The directory DB is rebuilt
from `Persons.csv` on the first call after each upload. Each tool call is logged with its key to
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
