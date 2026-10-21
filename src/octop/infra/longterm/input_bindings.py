"""Finite JSON input links between explicitly declared workflow dependencies."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

MAX_INPUT_BYTES = 131072


class InputBindingError(ValueError):
    """A link cannot be resolved from a successful declared dependency."""


def validate_input_bindings(value: Any, dependencies: set[str], depth: int = 0) -> None:
    if depth > 24:
        raise InputBindingError("node input nesting exceeds 24 levels")
    if isinstance(value, dict):
        if "$node" in value:
            if (
                set(value) - {"$node", "pointer"}
                or not isinstance(value["$node"], str)
                or value["$node"] not in dependencies
            ):
                raise InputBindingError("input links must reference a declared dependency")
            pointer = value.get("pointer", "")
            if (
                not isinstance(pointer, str)
                or len(pointer) > 1024
                or (pointer and not pointer.startswith("/"))
                or re.search(r"~(?![01])", pointer)
            ):
                raise InputBindingError("input link requires a valid JSON pointer")
        else:
            for item in value.values():
                validate_input_bindings(item, dependencies, depth + 1)
    elif isinstance(value, list):
        for item in value:
            validate_input_bindings(item, dependencies, depth + 1)


def resolve_input_bindings(value: Any, dependencies: dict[str, Any]) -> Any:
    """Copy linked values; never evaluate templates, files, URLs, or Python code."""
    validate_input_bindings(value, set(dependencies))

    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    remaining = MAX_INPUT_BYTES

    def consume(size: int) -> None:
        nonlocal remaining
        remaining -= size
        if remaining < 0:
            raise InputBindingError("resolved node input exceeds 128 KiB")

    def consume_json(item: Any) -> None:
        for chunk in encoder.iterencode(item):
            consume(len(chunk.encode()))

    def resolve(item: Any) -> Any:
        if isinstance(item, dict) and "$node" in item:
            result = dependencies[item["$node"]]
            if not isinstance(result, dict) or result.get("status") != "succeeded":
                raise InputBindingError("input dependency did not succeed")
            pointer = item.get("pointer", "")
            for part in pointer.split("/")[1:] if pointer else []:
                key = part.replace("~1", "/").replace("~0", "~")
                if isinstance(result, dict) and key in result:
                    result = result[key]
                elif isinstance(result, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
                    if len(key) > 9 or int(key) >= len(result):
                        raise InputBindingError("input JSON pointer does not exist")
                    result = result[int(key)]
                else:
                    raise InputBindingError("input JSON pointer does not exist")
            consume_json(result)
            return copy.deepcopy(result)
        if isinstance(item, dict):
            consume(2)
            resolved_dict = {}
            for index, (key, child) in enumerate(item.items()):
                consume_json(key)
                consume(1 + bool(index))
                resolved_dict[key] = resolve(child)
            return resolved_dict
        if isinstance(item, list):
            consume(2)
            resolved_list = []
            for index, child in enumerate(item):
                consume(bool(index))
                resolved_list.append(resolve(child))
            return resolved_list
        consume_json(item)
        return item

    return resolve(value)
