#!/usr/bin/env python3
"""Audit and optionally archive only the public MultiCraft source allowlist.

This is a publication guard, not a complete secret detector. Review the resulting
manifest and Git index before publishing. Runtime state is never an input.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile


ROOT_FILES = {
    "AGENTS.md", "README.md", "LICENSE", "THIRD_PARTY.md", "VERSION",
    ".gitignore", "requirements.txt", "requirements-test.txt", "package.json",
}
ROOT_DIRECTORIES = {".github", "assets", "config", "docs", "multicraft", "runtime-host", "scripts", "tests", "web"}
TEXT_SUFFIXES = {".py", ".java", ".js", ".mjs", ".json", ".md", ".txt", ".yaml", ".yml", ".html", ".css", ".svg"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".ico"}
PRIVATE_DIRECTORIES = {".git", ".runtime", ".venv", "venv", "evidence", "logs", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}
PRIVATE_SUFFIXES = {".key", ".pem", ".crt", ".p12", ".pfx", ".jks", ".token", ".jar", ".class", ".so", ".dll", ".dylib", ".pyc", ".log", ".pid", ".jfr"}
CONTENT_RULES = (
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"), "private key material"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|github_pat_[A-Za-z0-9_]{50,}"), "GitHub credential"),
    (re.compile(r"(?:https?://)?166\.111\.\d{1,3}\.\d{1,3}"), "private deployment address"),
    (re.compile(r"/(?:home|Users)/(?:asta|limh24)(?:/|\b)"), "private deployment home path"),
)


class AuditError(ValueError):
    pass


def contains_private_key(value: object) -> bool:
    """Recognize browser key JSON without confusing source-code property use."""
    if isinstance(value, dict):
        for key, content in value.items():
            normalized = str(key).replace("_", "").replace("-", "").lower()
            if normalized == "privatekey" and content not in (None, "", [], {}):
                return True
            if contains_private_key(content):
                return True
    elif isinstance(value, list):
        return any(contains_private_key(item) for item in value)
    return False


def source_files(root: Path) -> list[Path]:
    """Use a positive source allowlist; reject surprises inside source roots."""
    result = []
    for candidate in sorted(root.iterdir()):
        if candidate.name in ROOT_FILES:
            paths = [candidate]
        elif candidate.name in ROOT_DIRECTORIES:
            if candidate.is_symlink():
                raise AuditError(f"symlink is not publishable: {candidate.name}")
            paths = sorted(candidate.rglob("*"))
        else:
            continue
        for path in paths:
            relative = path.relative_to(root)
            if any(part in PRIVATE_DIRECTORIES for part in relative.parts):
                continue
            if path.is_symlink():
                raise AuditError(f"symlink is not publishable: {relative}")
            if path.is_dir():
                continue
            if path.suffix.lower() in PRIVATE_SUFFIXES:
                raise AuditError(f"private/binary artifact in source roots: {relative}")
            if path.name not in ROOT_FILES and path.suffix.lower() not in TEXT_SUFFIXES | IMAGE_SUFFIXES:
                raise AuditError(f"unrecognized source file type: {relative}")
            if path.suffix.lower() in IMAGE_SUFFIXES and relative.parts[:2] != ("assets", "brand"):
                raise AuditError(f"binary image outside brand assets: {relative}")
            if path.stat().st_size > 8 * 1024 * 1024:
                raise AuditError(f"source file exceeds 8 MiB review limit: {relative}")
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                try:
                    contents = path.read_text(encoding="utf-8")
                except UnicodeError as exc:
                    raise AuditError(f"non-UTF-8 source: {relative}") from exc
                if path.suffix.lower() == ".json":
                    try:
                        document = json.loads(contents)
                    except ValueError as exc:
                        raise AuditError(f"invalid JSON source: {relative}") from exc
                    if contains_private_key(document):
                        raise AuditError(f"private key field in JSON: {relative}")
                for pattern, explanation in CONTENT_RULES:
                    match = pattern.search(contents)
                    if match:
                        line = contents.count("\n", 0, match.start()) + 1
                        raise AuditError(f"{explanation}: {relative}:{line}")
            result.append(path)
    return sorted(result)


def manifest(root: Path, paths: list[Path]) -> dict:
    return {"schema": 1, "project": "MultiCraft", "version": (root / "VERSION").read_text().strip(),
            "files": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}}


def archive(root: Path, paths: list[Path], destination: Path, receipt: dict) -> None:
    """Write reproducible metadata, with no local absolute paths or user ids."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    with destination.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as bundle:
            for name, content in [(str(path.relative_to(root)), path.read_bytes()) for path in paths] + [
                ("SOURCE-MANIFEST.json", (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
            ]:
                entry = tarfile.TarInfo("MultiCraft/" + name)
                entry.size = len(content)
                entry.mode = 0o644
                entry.mtime = epoch
                bundle.addfile(entry, io.BytesIO(content))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--archive", type=Path, help="optional source-only .tar.gz destination")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        paths = source_files(root)
        result = manifest(root, paths)
        if args.archive:
            archive(root, paths, args.archive.resolve(), result)
        print(json.dumps({"ok": True, "version": result["version"], "files": len(paths),
                          "bytes": sum(path.stat().st_size for path in paths),
                          "archive": str(args.archive) if args.archive else None}, indent=2))
    except (AuditError, OSError, ValueError) as exc:
        parser.exit(1, f"Public source audit failed: {exc}\n")


if __name__ == "__main__":
    main()
