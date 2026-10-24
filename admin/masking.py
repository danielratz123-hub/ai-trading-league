"""Mask sensitive columns and nested JSON object keys before serialization."""
from __future__ import annotations

import json


def is_secret_column(name: str) -> bool:
    return any(word in name.lower() for word in (
        "key", "secret", "token", "password", "passwd", "hash", "credential", "auth"))


def mask_value(value):
    return "••••" if value is not None and value != "" else value


def mask_nested(value):
    if isinstance(value, dict):
        return {k: mask_value(v) if is_secret_column(k) else mask_nested(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [mask_nested(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return value
        if isinstance(parsed, (dict, list)):
            return mask_nested(parsed)
    return value


def mask_record(record, *, parse_json=False):
    result = {}
    for name, value in record.items():
        masked = mask_value(value) if is_secret_column(name) else mask_nested(value)
        if isinstance(masked, (dict, list)) and not parse_json:
            masked = json.dumps(masked, ensure_ascii=False)
        if isinstance(masked, bytes):
            masked = masked.hex()
        result[name] = masked
    return result
