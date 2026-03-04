import json
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Persons Uploader")
security = HTTPBasic()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["Authorization"],
    allow_methods=["GET"],
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./uploads"))
CREDENTIALS_FILE = Path("credentials.json")

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory="templates")
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
# Routes
# ---------------------------------------------------------------------------
def _file_state() -> dict:
    return {
        "persons_exists": (UPLOAD_DIR / "Persons.csv").exists(),
        "tag_files": sorted(f.name for f in UPLOAD_DIR.glob("*.json")),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(require_auth)):
    return templates.TemplateResponse("index.html", {"request": request, "user": user, **_file_state()})


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


# ---------------------------------------------------------------------------
# Admin – user management
# ---------------------------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
async def admin_get(request: Request, user: dict = Depends(require_admin)):
    return templates.TemplateResponse("admin.html", {
        "request": request,
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
        return templates.TemplateResponse("admin.html", {
            "request": request,
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
    return templates.TemplateResponse("edit_user.html", {
        "request": request,
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
        return templates.TemplateResponse("edit_user.html", {
            "request": request,
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
