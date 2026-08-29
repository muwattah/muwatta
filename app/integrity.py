"""Source integrity checks — SHA-256 of immutable originals."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .db import get_conn, ROOT


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_source_hashes() -> dict:
    """
    Compare on-disk SHA-256 with registered hashes.
    On mismatch: SOURCE INTEGRITY FAILURE — do not proceed.
    """
    results = {"ok": True, "files": [], "failures": []}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source_id, original_filename, storage_path, sha256, volume_number FROM source_files"
        ).fetchall()
    for row in rows:
        path = ROOT / row["storage_path"]
        entry = {
            "volume": row["volume_number"],
            "filename": row["original_filename"],
            "registered_sha256": row["sha256"],
            "path": str(path),
        }
        if not path.exists():
            entry["status"] = "MISSING"
            results["ok"] = False
            results["failures"].append(entry)
        else:
            actual = sha256_file(path)
            entry["actual_sha256"] = actual
            if actual != row["sha256"]:
                entry["status"] = "SOURCE INTEGRITY FAILURE"
                results["ok"] = False
                results["failures"].append(entry)
            else:
                entry["status"] = "OK"
        results["files"].append(entry)
    return results


def assert_source_integrity() -> None:
    r = verify_source_hashes()
    if not r["ok"]:
        raise RuntimeError(
            "SOURCE INTEGRITY FAILURE: "
            + "; ".join(
                f"vol{f['volume']} {f.get('status')}" for f in r["failures"]
            )
        )
