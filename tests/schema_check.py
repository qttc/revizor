"""Мини-валидатор JSON Schema для тестов: type, const, enum, required, properties, items, $ref. Без зависимостей."""
import json
from pathlib import Path

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "docs" / "review.schema.json").read_text(encoding="utf-8"))
TYPES = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}


def errors(value, schema=SCHEMA, path="$"):
    if "$ref" in schema:
        schema = SCHEMA["$defs"][schema["$ref"].rsplit("/", 1)[-1]]
    out = []
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: {value!r} ≠ {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} не из {schema['enum']}")
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(isinstance(value, TYPES[k]) and not (k == "integer" and isinstance(value, bool)) for k in kinds):
            out.append(f"{path}: {type(value).__name__} вместо {'/'.join(kinds)}")
            return out
    if isinstance(value, dict):
        out += [f"{path}: нет поля {k}" for k in schema.get("required", []) if k not in value]
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                out += errors(value[k], sub, f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for n, v in enumerate(value):
            out += errors(v, schema["items"], f"{path}[{n}]")
    return out
