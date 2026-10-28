#!/usr/bin/env python3
"""Compile the owned M1 host sources using the existing pinned Java 25 toolchain.

No downloads, native launches, Minecraft modifications or package installs.
Run on the GPU host after syncing runtime-host/src and this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

CLIENT_SHA256 = "4508d006323f24fa02876310c192d739af56516eb259000ac50f0909a68c9a2d"


def build(project: Path, minecraft: Path, build_dir: Path, java_home: Path) -> dict:
    project, minecraft, build_dir = project.resolve(), minecraft.resolve(), build_dir.resolve()
    if build_dir == project or project not in build_dir.parents or ".runtime" not in build_dir.parts:
        raise ValueError("Build output must be a project-local .runtime directory")
    javac, jar = java_home / "bin/javac", java_home / "bin/jar"
    if not javac.is_file() or not jar.is_file():
        raise ValueError("Existing Java 25 javac and jar are required; this builder installs nothing")
    client = minecraft / "client.jar"
    with client.open("rb") as source_jar:
        actual_hash = hashlib.file_digest(source_jar, "sha256").hexdigest()
    if actual_hash != CLIENT_SHA256:
        raise ValueError("The unmodified pinned 26.3 client.jar hash did not match")
    lock = json.loads((minecraft / "lock.json").read_text())
    libraries = [(minecraft / "libraries" / path).resolve() for path in lock["libraryPaths"]]
    if not all(path.is_file() for path in libraries):
        raise ValueError("Minecraft locked libraries are incomplete")
    source = project / "runtime-host/src"
    sources = sorted(source.rglob("*.java"))
    if not sources:
        raise ValueError("No owned runtime-host sources found")
    build_dir.mkdir(parents=True, exist_ok=True)
    target = build_dir / "multicraft-host.jar"
    # Stage in an owned temporary directory so removed source files cannot leave
    # stale classes in the next JAR, and failed compilation preserves the last JAR.
    with tempfile.TemporaryDirectory(prefix="compile-", dir=build_dir) as temporary:
        stage = Path(temporary)
        classes = stage / "classes"
        classes.mkdir()
        # javac does not execute or initialize any Minecraft classes.
        subprocess.run([str(javac), "--release", "25", "-encoding", "UTF-8", "-cp", os.pathsep.join(map(str, libraries)),
                        "-d", str(classes), *map(str, sources)], check=True)
        staged_jar = stage / "host.jar"
        subprocess.run([str(jar), "--create", "--file", str(staged_jar), "--main-class", "multicraft.host.Host", "-C", str(classes), "."], check=True)
        os.replace(staged_jar, target)
    with target.open("rb") as built_jar:
        jar_hash = hashlib.file_digest(built_jar, "sha256").hexdigest()
    manifest = {"schema": 1, "clientSha256": CLIENT_SHA256, "javaHome": str(java_home.resolve()),
                "jar": str(target), "jarSha256": jar_hash,
                "sources": {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
                "supportedAudioModes": ["disabled-probe", "loopback-pcm"],
                "supportedSdlQuitPolicies": ["native", "process-exit"],
                "initialDisplayPolicy": "distinct-session-displayIndex-with-native-verification",
                "supportedTeardownModes": ["standard", "defer-window", "defer-instance", "defer-native"], "m1Complete": False}
    (build_dir / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--minecraft", type=Path)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--java-home", type=Path, required=True)
    args = parser.parse_args()
    minecraft = args.minecraft or args.project / ".runtime/minecraft/26.3"
    build_dir = args.build_dir or args.project / ".runtime/m1/build"
    print(json.dumps(build(args.project, minecraft, build_dir, args.java_home), indent=2))


if __name__ == "__main__":
    main()
