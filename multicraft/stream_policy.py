"""User stream controls, bounded independently from the OP's rendering profile."""
from __future__ import annotations

import copy
import json
from pathlib import Path

DEFAULT_CONFIG = {"width": 1280, "height": 720, "fps": 30, "bitrateKbps": 8000, "adaptive": True}
LIMITS = {"width": [320, 1920], "height": [180, 1080], "fps": [10, 1000], "bitrateKbps": [500, 20000]}
ABSOLUTE_MAXIMUMS = {"width": 7680, "height": 4320, "fps": 1000, "bitrateKbps": 100000}


class StreamPolicyError(ValueError):
    pass


def validate_limits(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(LIMITS):
        raise StreamPolicyError("limits 必须包含 width、height、fps、bitrateKbps")
    result = {}
    for name, default in LIMITS.items():
        bounds = value[name]
        if (not isinstance(bounds, list) or len(bounds) != 2
                or any(type(item) is not int for item in bounds)
                or bounds[0] != default[0] or not default[0] <= bounds[1] <= ABSOLUTE_MAXIMUMS[name]):
            raise StreamPolicyError(f"{name} 上限必须为 {default[0]}–{ABSOLUTE_MAXIMUMS[name]} 之间的整数，下界固定为 {default[0]}")
        if name in ("width", "height") and bounds[1] % 2:
            raise StreamPolicyError("分辨率上限必须为偶数")
        result[name] = list(bounds)
    return result


def load_policy(path: Path | str | None) -> dict:
    if path is None:
        return {"revision": 0, "limits": copy.deepcopy(LIMITS)}
    try:
        value = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"revision": 0, "limits": copy.deepcopy(LIMITS)}
    except (OSError, ValueError) as exc:
        raise StreamPolicyError("传流限额文件无法读取") from exc
    if (not isinstance(value, dict) or set(value) != {"revision", "limits"}
            or type(value["revision"]) is not int or value["revision"] < 0):
        raise StreamPolicyError("传流限额文件格式无效")
    return {"revision": value["revision"], "limits": validate_limits(value["limits"])}


def load_limits(path: Path | str | None) -> dict:
    return load_policy(path)["limits"]


def clamp_config(value: dict, limits: dict | None = None) -> dict:
    limits = validate_limits(LIMITS if limits is None else limits)
    if not isinstance(value, dict) or set(value) != set(DEFAULT_CONFIG) or type(value["adaptive"]) is not bool:
        raise StreamPolicyError("当前传流参数格式无效")
    result = {}
    for name, bounds in limits.items():
        if type(value[name]) is not int:
            raise StreamPolicyError("当前传流参数必须为整数")
        result[name] = max(bounds[0], min(bounds[1], value[name]))
        if name in ("width", "height"):
            result[name] -= result[name] % 2
    result["adaptive"] = value["adaptive"]
    return result


def validate_config(value: dict, limits: dict | None = None) -> dict:
    limits = validate_limits(LIMITS if limits is None else limits)
    if not isinstance(value, dict) or set(value) != set(DEFAULT_CONFIG):
        raise StreamPolicyError("必须提供 width、height、fps、bitrateKbps 和 adaptive")
    result = {}
    for name, bounds in limits.items():
        item = value[name]
        if type(item) is not int or not bounds[0] <= item <= bounds[1]:
            raise StreamPolicyError(f"{name} 必须为 {bounds[0]}–{bounds[1]} 之间的整数")
        result[name] = item
    if result["width"] % 2 or result["height"] % 2:
        raise StreamPolicyError("视频宽高必须为偶数")
    if type(value["adaptive"]) is not bool:
        raise StreamPolicyError("adaptive 必须为布尔值")
    result["adaptive"] = value["adaptive"]
    return result
