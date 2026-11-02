"""Point the app at a throwaway uploads folder before it is imported."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)                                  # templates/ and static/ are relative
TMP = tempfile.mkdtemp(prefix="persons-uploader-tests-")
os.environ["UPLOAD_DIR"] = TMP
os.environ["AUTH_USERNAME"] = "admin"
os.environ["AUTH_PASSWORD"] = "test-password"
os.environ["CREDENTIALS_FILE"] = str(Path(TMP) / "credentials.json")
