#!/usr/bin/env python3
"""Build the Windows offline site-server packages:
    release/Hospital-Setup.zip and release/Clinic-Setup.zip
(identical except setup/site.env and the starting database).

Run with the local venv (needs Pillow for the .ico):
    backend/.venv-local/bin/python scripts/build_windows.py --work .winbuild

<dir> must already contain (fetched once, see the session notes):
    python-3.12.10-embed-amd64.zip   official Windows embeddable Python
    site-packages/                   win_amd64 wheels installed with
                                     `uv pip install --target site-packages
                                      --python-platform x86_64-pc-windows-msvc
                                      --python-version 3.12 --only-binary :all: ...`
    fonts/fonts-local.css + *.woff2  Noto Sans Myanmar (so text renders offline)
    initial-clinic.db                clinic starting database (scripts/make_clinic_db.py clinic ...;
                                     initial.db is accepted too)
    initial-hospital.db              hospital starting database (scripts/make_site_db.py <cloud copy> ...)
    cloud.env                        CLOUD_URL=... and SYNC_KEY=... (matches the cloud's SYNC_SHARED_KEY)
"""
import argparse
import hashlib
import json
import py_compile
import re
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
WIN = ROOT / "windows"
SITES = ("hospital", "clinic")


def crlf(src: Path, dst: Path, bom: bool = False) -> None:
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))


def copytree(src: Path, dst: Path, ignore=("__pycache__", "*.pyc")) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*ignore), dirs_exist_ok=True)


def strip_source(folder: Path) -> None:
    """Replace every .py with a bytecode-only .pyc next to it (no comments or
    docstrings), so the install has no readable source code. Must be built with
    Python 3.12 — the same bytecode version as the embedded Windows Python."""
    for src in sorted(folder.rglob("*.py")):
        py_compile.compile(str(src), cfile=str(src.with_suffix(".pyc")), doraise=True, optimize=2)
        src.unlink()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--site", choices=SITES, action="append", help="build only this site (default: both)")
    args = ap.parse_args()
    version = app_version()
    for site in args.site or SITES:
        build(args.work, site, version)
    build_update(version, (args.site or SITES)[-1])


def app_version() -> str:
    """e.g. act259-20261120-1745 — the frontend bundle + build time."""
    index = (ROOT / "frontend" / "dist" / "index.html").read_text(encoding="utf-8")
    act = re.search(r'/assets/index-(act\d+)', index)
    return f"{act.group(1) if act else 'app'}-{datetime.now():%Y%m%d-%H%M}"


def build_update(version: str, site: str) -> None:
    """release/update/app-update.zip + manifest.json: the app folder of a site
    install (no Python, no starting database). The deploy puts both on the cloud;
    site servers download it and install it when an admin presses "⬆ Update"."""
    app = ROOT / "release" / f"Hospital-{site.title()}-Setup" / "payload" / "app"
    out = ROOT / "release" / "update"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    pkg = out / "app-update.zip"
    with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(app.rglob("*")):
            if f.is_file() and f.name != "initial.db":
                z.write(f, f.relative_to(app))
    sha = hashlib.sha256(pkg.read_bytes()).hexdigest()
    (out / "manifest.json").write_text(json.dumps({
        "version": version, "sha256": sha, "size": pkg.stat().st_size,
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=1), encoding="utf-8")
    print(f"built {pkg} ({pkg.stat().st_size / 1e6:.1f} MB), version {version}")


def mark_fresh(db_path: Path) -> None:
    """Every shipped starting database carries the go-live marker that
    windows/app/fresh_check.py looks for (a site database without it is set aside once)."""
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS app_meta (key VARCHAR(60) PRIMARY KEY, value VARCHAR(200))")
    con.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES ('site_fresh_start_1', 'done')")
    con.commit()
    con.close()


def initial_db(work: Path, site: str) -> Path:
    for name in (f"initial-{site}.db", "initial.db" if site == "clinic" else ""):
        if name and (work / name).exists():
            return work / name
    raise SystemExit(f"missing {work}/initial-{site}.db")


def build(work: Path, site: str, version: str) -> None:
    name = f"Hospital-{site.title()}-Setup"
    initial = initial_db(work, site)
    out_root = ROOT / "release"
    out = out_root / name
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    # Top level: the two .bat buttons + README (Notepad-friendly UTF-8 BOM).
    for bat in ("Install Hospital.bat", "Uninstall Hospital.bat"):
        crlf(WIN / bat, out / bat)
    crlf(WIN / "README.txt", out / "README.txt", bom=True)

    # setup/: PowerShell 5.1 needs a BOM to read the Myanmar text correctly.
    for ps1 in ("install.ps1", "uninstall.ps1"):
        crlf(WIN / "setup" / ps1, out / "setup" / ps1, bom=True)
    dist = ROOT / "frontend" / "dist"
    shutil.copy(dist / "pwa" / "icon-192.png", out / "setup" / "logo.png")
    # CLOUD_URL + SYNC_KEY for clinic -> cloud sync (kept out of the repo).
    if (work / "cloud.env").exists():
        shutil.copy(work / "cloud.env", out / "setup" / "cloud.env")
    else:
        print("WARNING: no cloud.env in --work: the clinic will not sync to the cloud")
    (out / "setup" / "site.env").write_text(f"SITE={site}\r\n", encoding="ascii")
    ico = out / "setup" / "hospital.ico"
    Image.open(dist / "pwa" / "icon-512.png").convert("RGBA").save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    # payload/python: embeddable Python + packages.
    py = out / "payload" / "python"
    with zipfile.ZipFile(work / "python-3.12.10-embed-amd64.zip") as z:
        z.extractall(py)
    (py / "python312._pth").write_text("python312.zip\r\n.\r\nLib\\site-packages\r\nimport site\r\n", encoding="ascii")
    copytree(work / "site-packages", py / "Lib" / "site-packages", ignore=("__pycache__", "*.pyc", "bin"))

    # payload/app: server, backend, built frontend, starting database.
    app = out / "payload" / "app"
    app.mkdir(parents=True)
    for f in ("server.py", "backup.py", "site_sync.py", "fresh_check.py", "updater.py"):
        shutil.copy(WIN / "app" / f, app / f)
    shutil.copy(ROOT / "scripts" / "reset_data.py", app / "reset_data.py")
    shutil.copy(ROOT / "scripts" / "import_catalog.py", app / "import_catalog.py")
    shutil.copy(WIN / "app" / "tara_catalog.json", app / "tara_catalog.json")
    shutil.copy(ico, app / "hospital.ico")
    shutil.copy(initial, app / "initial.db")
    mark_fresh(app / "initial.db")
    copytree(ROOT / "backend" / "app", app / "backend" / "app")
    strip_source(app)
    (app / "VERSION").write_text(version, encoding="ascii")

    web = app / "frontend" / "dist"
    index = (dist / "index.html").read_text(encoding="utf-8")
    bundle = re.search(r'/assets/(index-[^"]+\.js)', index).group(1)
    # Only the live bundle — older cache-busting copies are dead weight.
    copytree(dist, web, ignore=("__pycache__", "index-act*.js"))
    shutil.copy(dist / "assets" / bundle, web / "assets" / bundle)
    # Fonts from this server instead of Google (the clinic has no internet).
    index = re.sub(r'\s*<link rel="preconnect"[^>]*>', "", index)
    index = re.sub(r'<link href="https://fonts\.googleapis\.com/[^"]+" rel="stylesheet" />',
                   '<link href="/fonts/fonts.css" rel="stylesheet" />', index)
    assert "googleapis" not in index
    (web / "index.html").write_text(index, encoding="utf-8")
    (web / "fonts").mkdir(exist_ok=True)
    shutil.copy(work / "fonts" / "fonts-local.css", web / "fonts" / "fonts.css")
    for woff in (work / "fonts").glob("*.woff2"):
        shutil.copy(woff, web / "fonts" / woff.name)

    # Zip it (one folder inside, so "Extract All" gives Clinic-Setup\...).
    zip_path = out_root / f"{name}.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(out_root))
    print(f"built {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB), bundle {bundle}")


if __name__ == "__main__":
    main()
