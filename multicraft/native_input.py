"""Strict browser-to-native input validation shared by the gateways."""
import math


def validate_input(value: object) -> dict:
    """Validate the whole flat batch before a single native event is submitted."""
    if not isinstance(value, dict):
        raise ValueError("input must be an object")
    if "events" in value:
        if set(value) != {"expectedRuntimeOwner", "expectedEpoch", "inputSeq", "events"} or not isinstance(value["events"], list) or not 1 <= len(value["events"]) <= 128:
            raise ValueError("input requires 1..128 flat events")
        validate_runtime_owner(value["expectedRuntimeOwner"])
        for name, low in (("expectedEpoch", 0), ("inputSeq", 1)):
            if type(value[name]) is not int or not low <= value[name] <= 2**53 - 1:
                raise ValueError("invalid " + name)
        return {"expectedRuntimeOwner": value["expectedRuntimeOwner"], "expectedEpoch": value["expectedEpoch"], "inputSeq": value["inputSeq"],
                "events": [validate_event(event) for event in value["events"]]}
    raise ValueError("input requires an epoch-bound event envelope")


def validate_runtime_owner(value):
    if not isinstance(value, str) or not 16 <= len(value) <= 128 or not value.isascii() or not all(char.isalnum() or char in "_.-" for char in value):
        raise ValueError("invalid runtime owner")


def validate_event(event: object) -> dict:
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise ValueError("invalid input event")
    kind = event["type"]
    fields = {"key": ({"type", "scancode", "keycode", "action"}, {"modifiers"}),
              "button": ({"type", "button", "action"}, set()),
              "move": ({"type", "x", "y"}, {"dx", "dy"}),
              "scroll": ({"type", "x", "y"}, set()), "text": ({"type", "text"}, set()),
              "grab": ({"type"}, set()), "release": ({"type"}, set())}
    if kind not in fields:
        raise ValueError("unsupported input event")
    required, optional = fields[kind]
    if not required <= set(event) <= required | optional:
        raise ValueError("unexpected or missing input fields")
    if kind == "key":
        bounds = {"scancode": (0, 512), "keycode": (0, 2147483647), "action": (-1, 1), "modifiers": (0, 65535)}
    elif kind == "button":
        bounds = {"button": (1, 8), "action": (0, 1)}
    else:
        bounds = {}
    for name, (low, high) in bounds.items():
        if name in event and (type(event[name]) is not int or not low <= event[name] <= high):
            raise ValueError("invalid " + name)
    if kind in {"move", "scroll"}:
        for name in set(event) - {"type"}:
            number = event[name]
            if type(number) not in (float, int) or not math.isfinite(number) or abs(number) > 1_000_000:
                raise ValueError("invalid pointer coordinate")
    if kind == "text" and (not isinstance(event["text"], str) or len(event["text"]) > 16384):
        raise ValueError("invalid text")
    return dict(event)
