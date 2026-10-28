#!/usr/bin/env python3
"""Prepare private, SHA-1 verified Mojang assets for an isolated M0 session.

This launcher deliberately targets Linux x86_64 and Vanilla 26.3. It neither
installs into a user's normal Minecraft directory nor reads account credentials.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import time
import urllib.request
import uuid

VERSION = "26.3"
METADATA_SHA1 = "5df1795e16eea19d75b923d66ecca9ce183353c5"
METADATA_URL = f"https://piston-meta.mojang.com/v1/packages/{METADATA_SHA1}/{VERSION}.json"
FEATURES = {
    "has_custom_resolution": True,
    "has_quick_plays_support": True,
    "is_quick_play_multiplayer": True,
}


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rules_allow(rules: list[dict] | None, *, system: str = "linux",
                arch: str = "x86_64", features: dict | None = None) -> bool:
    if not rules:
        return True
    features = FEATURES if features is None else features
    allowed = False
    for rule in rules:
        os_rule = rule.get("os", {})
        if os_rule.get("name", system) != system:
            continue
        if "arch" in os_rule and not re.fullmatch(os_rule["arch"], arch):
            continue
        if "version" in os_rule and not re.search(os_rule["version"], platform.release()):
            continue
        if any(features.get(key, False) != value
               for key, value in rule.get("features", {}).items()):
            continue
        allowed = rule["action"] == "allow"
    return allowed


def download_verified(url: str, destination: Path, expected_sha1: str,
                      *, borrow: Path | None = None) -> str:
    if destination.is_file() and sha1_file(destination) == expected_sha1:
        return "cached"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".part")
    try:
        if borrow and borrow.is_file() and sha1_file(borrow) == expected_sha1:
            shutil.copyfile(borrow, temporary)
            result = "borrowed-verified-copy"
        else:
            for attempt in range(4):
                try:
                    request = urllib.request.Request(url, headers={"User-Agent": "MultiCraft-M0/0.1"})
                    with urllib.request.urlopen(request, timeout=90) as response, temporary.open("wb") as stream:
                        shutil.copyfileobj(response, stream)
                    break
                except Exception:
                    if attempt == 3:
                        raise
                    time.sleep(0.5 * (attempt + 1))
            result = "downloaded"
        if sha1_file(temporary) != expected_sha1:
            raise ValueError(f"SHA-1 mismatch: {destination.name}")
        os.replace(temporary, destination)
        return result
    finally:
        temporary.unlink(missing_ok=True)


def selected_libraries(metadata: dict) -> list[dict]:
    result = []
    for library in metadata["libraries"]:
        if not rules_allow(library.get("rules")):
            continue
        artifact = library.get("downloads", {}).get("artifact")
        if artifact:
            result.append(artifact)
        # Older manifest support: modern 26.3 puts native JARs directly in the
        # artifact list. Retaining classifiers here makes missing native files
        # explicit rather than silently relying on a preinstalled launcher.
        classifier = library.get("natives", {}).get("linux")
        if classifier:
            classifier = classifier.replace("${arch}", "64")
            native = library.get("downloads", {}).get("classifiers", {}).get(classifier)
            if native:
                result.append(native)
    return list({artifact["path"]: artifact for artifact in result}.values())


def prepare(runtime: Path, *, workers: int = 12, borrow_client: Path | None = None,
            borrow_server: Path | None = None) -> dict:
    runtime = runtime.resolve()
    version_dir = runtime / VERSION
    version_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = version_dir / "version.json"
    download_verified(METADATA_URL, metadata_path, METADATA_SHA1)
    metadata = json.loads(metadata_path.read_text())
    tasks: list[tuple[str, Path, str, Path | None]] = []
    for name, borrow in [("client", borrow_client), ("server", borrow_server)]:
        item = metadata["downloads"][name]
        tasks.append((item["url"], version_dir / f"{name}.jar", item["sha1"], borrow))
    libraries = selected_libraries(metadata)
    for artifact in libraries:
        tasks.append((artifact["url"], version_dir / "libraries" / artifact["path"], artifact["sha1"], None))
    asset_index = metadata["assetIndex"]
    index_path = version_dir / "assets" / "indexes" / f"{asset_index['id']}.json"
    download_verified(asset_index["url"], index_path, asset_index["sha1"])
    objects = json.loads(index_path.read_text())["objects"]
    hashes = sorted({value["hash"] for value in objects.values()})
    for digest in hashes:
        tasks.append((f"https://resources.download.minecraft.net/{digest[:2]}/{digest}",
                      version_dir / "assets" / "objects" / digest[:2] / digest, digest, None))
    counts: dict[str, int] = {}

    def fetch(task: tuple[str, Path, str, Path | None]) -> str:
        url, path, digest, borrow = task
        return download_verified(url, path, digest, borrow=borrow)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for index, result in enumerate(executor.map(fetch, tasks), 1):
            counts[result] = counts.get(result, 0) + 1
            if index % 250 == 0 or index == len(tasks):
                print(json.dumps({"prepared": index, "total": len(tasks), "results": counts}), flush=True)
    for subdir in ["java", "jna", "lwjgl", "netty"]:
        (version_dir / "natives" / subdir).mkdir(parents=True, exist_ok=True)
    lock = {
        "schema": 1, "minecraft": VERSION, "loader": "vanilla", "javaMajor": 25,
        "platform": "linux-x86_64", "metadataUrl": METADATA_URL,
        "metadataSha1": METADATA_SHA1, "mainClass": metadata["mainClass"],
        "clientSha1": metadata["downloads"]["client"]["sha1"],
        "serverSha1": metadata["downloads"]["server"]["sha1"],
        "assetIndex": asset_index["id"], "assetIndexSha1": asset_index["sha1"],
        "libraryPaths": [item["path"] for item in libraries],
        "assetObjects": len(hashes), "verifiedFiles": len(tasks) + 2,
        "privateAssetDirectory": str(version_dir),
    }
    (version_dir / "lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    return lock


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=Path(".runtime/minecraft"))
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--borrow-client", type=Path)
    parser.add_argument("--borrow-server", type=Path)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        parser.error("preparation targets the Linux x86_64 M0 host")
    if not 1 <= args.workers <= 24:
        parser.error("workers must be between 1 and 24")
    print(json.dumps(prepare(args.runtime_dir, workers=args.workers,
                             borrow_client=args.borrow_client, borrow_server=args.borrow_server), indent=2))


if __name__ == "__main__":
    main()
