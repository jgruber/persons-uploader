import json
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Persons Uploader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["Authorization"],
    allow_methods=["GET"],
)
security = HTTPBasic()

# ---------------------------------------------------------------------------
# Configuration – override via environment variables
# ---------------------------------------------------------------------------
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./uploads"))
CREDENTIALS_FILE = Path("credentials.json")

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Credentials – persisted to credentials.json, seeded from env vars
# ---------------------------------------------------------------------------
def _load_credentials() -> dict:
    if CREDENTIALS_FILE.exists():
        data = json.loads(CREDENTIALS_FILE.read_text())
        return {"username": data["username"], "password": data["password"]}
    return {
        "username": os.getenv("AUTH_USERNAME", "admin"),
        "password": os.getenv("AUTH_PASSWORD", "changeme"),
    }


def _save_credentials(username: str, password: str) -> None:
    CREDENTIALS_FILE.write_text(json.dumps({"username": username, "password": password}))


_credentials = _load_credentials()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    correct_username = secrets.compare_digest(credentials.username, _credentials["username"])
    correct_password = secrets.compare_digest(credentials.password, _credentials["password"])
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, username: str = Depends(require_auth)):
    return templates.TemplateResponse("index.html", {"request": request, "username": username})


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    username: str = Depends(require_auth),
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


@app.get("/download")
async def download(username: str = Depends(require_auth)):
    dest = UPLOAD_DIR / "Persons.csv"
    if not dest.exists():
        raise HTTPException(status_code=404, detail="Persons.csv has not been uploaded yet.")
    return FileResponse(dest, media_type="text/csv", filename="Persons.csv")


@app.get("/admin", response_class=HTMLResponse)
async def admin_get(request: Request, username: str = Depends(require_auth)):
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "username": username,
        "current_username": _credentials["username"],
        "error": None,
        "success": False,
    })


@app.post("/admin", response_class=HTMLResponse)
async def admin_post(
    request: Request,
    username: str = Depends(require_auth),
    new_username: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    def render(error=None, success=False):
        return templates.TemplateResponse("admin.html", {
            "request": request,
            "username": username,
            "current_username": _credentials["username"],
            "error": error,
            "success": success,
        })

    new_username = new_username.strip()

    if not new_username:
        return render(error="Username cannot be blank.")
    if len(new_password) < 8:
        return render(error="Password must be at least 8 characters.")
    if new_password != confirm_password:
        return render(error="Passwords do not match.")

    global _credentials
    _credentials = {"username": new_username, "password": new_password}
    _save_credentials(new_username, new_password)

    # Render success page – browser will re-prompt on next navigation
    return render(success=True)
