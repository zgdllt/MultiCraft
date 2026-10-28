#!/usr/bin/env python3
"""Prepare/check only the owned M0 test world. Actual interaction is in browser."""
import argparse
import json
from pathlib import Path
import time

from native_session import server_command

ROOT = Path(__file__).resolve().parents[1]
GAME = ROOT / ".runtime/minecraft/26.3"


def commands(action):
    if action == "prepare":
        return ["time set day", "weather clear", "fill -8 79 -8 8 79 8 minecraft:stone",
                "fill -8 80 -8 8 86 8 minecraft:air", "tp M0Player 0.5 80 4.5 180 2.3",
                "gamemode survival M0Player", "clear M0Player",
                "give M0Player minecraft:iron_pickaxe 1", "give M0Player minecraft:dirt 64",
                "setblock 0 81 0 minecraft:stone", "setblock 0 81 1 minecraft:stone"]
    if action == "break":
        return ["execute if block 0 81 1 minecraft:air run say M0_BREAK_PASS"]
    if action == "place":
        return ["execute if block 0 81 1 minecraft:dirt run say M0_PLACE_PASS", "data get entity M0Player Inventory"]
    if action == "chest":
        return ['setblock 0 81 1 minecraft:chest{Items:[{Slot:0b,id:"minecraft:diamond",count:3}]}']
    if action == "inventory":
        return ["data get entity M0Player Inventory"]
    if action == "position":
        return ["data get entity M0Player Pos"]
    if action == "death":
        return ["kill M0Player"]
    if action in ("nether", "overworld"):
        dimension = "minecraft:the_nether" if action == "nether" else "minecraft:overworld"
        return ["gamemode creative M0Player", f"execute in {dimension} run tp M0Player 0.5 82 4.5 180 0",
                "data get entity M0Player Dimension"]
    if action == "sound":
        return ["execute at M0Player run playsound minecraft:block.note_block.pling master M0Player ~ ~ ~ 1 1"]
    raise ValueError("unknown probe")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "break", "place", "chest", "inventory", "position", "death", "nether", "overworld", "sound"))
    args = parser.parse_args()
    properties = (GAME / "server/server.properties").read_text().splitlines()
    for required in ("server-ip=127.0.0.1", "online-mode=false", "level-name=m0-test-world"):
        if required not in properties:
            raise RuntimeError("refusing to operate on a non-M0 server")
    actions = commands(args.action)
    for command in actions:
        server_command(GAME, command)
    record = {"wallTime": time.time(), "probe": args.action, "consoleCommands": actions,
              "scope": "isolated M0 test server; browser input is verified separately"}
    with (ROOT / ".runtime/gameplay-actions.jsonl").open("a") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
