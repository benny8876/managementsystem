"""Run Hospital Management System locally: the FastAPI backend plus the built frontend on one port,
mirroring what Caddy does on the server (/api + /health -> backend, everything
else -> frontend/dist with SPA fallback).

    backend/.venv-local/bin/python run_local.py

Data lives in local-data/hospital.db. On the first run the database is
created and filled with demo data (branches, staff, catalog) — see
backend/app/db/seed.py. Demo staff password: ChangeMe@123 (change it).
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "local-data"
DB = DATA / "hospital.db"
DIST = ROOT / "frontend" / "dist"
PORT = int(os.environ.get("PORT", "8002"))

DATA.mkdir(exist_ok=True)
FIRST_RUN = not DB.exists()

os.environ.setdefault("DATABASE_URL", f"sqlite:///{DB}")
os.environ.setdefault("SEED_ON_START", "true" if FIRST_RUN else "false")
os.environ.setdefault("CORS_ORIGINS", f"http://localhost:{PORT},http://127.0.0.1:{PORT}")

sys.path.insert(0, str(ROOT / "backend"))
os.chdir(ROOT / "backend")

from fastapi.responses import FileResponse  # noqa: E402

from app.main import app  # noqa: E402


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str):
    if path.startswith("api/"):
        from fastapi import HTTPException

        raise HTTPException(404, "Not Found")
    file = (DIST / path).resolve()
    if path and file.is_file() and DIST in file.parents:
        return FileResponse(file)
    return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
