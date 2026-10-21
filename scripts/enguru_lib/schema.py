"""Mini JSON-schema validator (stdlib only) and schema loader.

Supported keywords: type (string or list), required, properties,
additionalProperties: false, items, enum, minItems, maxItems, minLength,
maxLength, minimum, maximum.
"""
import json
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "schemas"


def _type_ok(value, t):
    if t == "null":
        return value is None
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    return False


def _validate(inst, schema, path, errors):
    if "type" in schema:
        types = schema["type"]
        if isinstance(types, str):
            types = [types]
        if not any(_type_ok(inst, t) for t in types):
            errors.append("%s: expected type %s, got %s" % (path, "|".join(types), type(inst).__name__))
            return
    if "enum" in schema and inst not in schema["enum"]:
        errors.append("%s: %r not in enum %r" % (path, inst, schema["enum"]))
    if isinstance(inst, str):
        if "minLength" in schema and len(inst) < schema["minLength"]:
            errors.append("%s: string shorter than minLength %d" % (path, schema["minLength"]))
        if "maxLength" in schema and len(inst) > schema["maxLength"]:
            errors.append("%s: string longer than maxLength %d" % (path, schema["maxLength"]))
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in schema and inst < schema["minimum"]:
            errors.append("%s: %r below minimum %r" % (path, inst, schema["minimum"]))
        if "maximum" in schema and inst > schema["maximum"]:
            errors.append("%s: %r above maximum %r" % (path, inst, schema["maximum"]))
    if isinstance(inst, dict):
        for key in schema.get("required", []):
            if key not in inst:
                errors.append("%s: missing required property '%s'" % (path, key))
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in inst:
                if key not in props:
                    errors.append("%s: additional property '%s' not allowed" % (path, key))
        for key, sub in props.items():
            if key in inst:
                _validate(inst[key], sub, "%s.%s" % (path, key), errors)
    if isinstance(inst, list):
        if "minItems" in schema and len(inst) < schema["minItems"]:
            errors.append("%s: fewer than minItems %d" % (path, schema["minItems"]))
        if "maxItems" in schema and len(inst) > schema["maxItems"]:
            errors.append("%s: more than maxItems %d" % (path, schema["maxItems"]))
        if "items" in schema:
            for i, item in enumerate(inst):
                _validate(item, schema["items"], "%s[%d]" % (path, i), errors)


def validate(instance, schema):
    """Return a list of error strings, each prefixed with a JSON path ('$...')."""
    errors = []
    _validate(instance, schema, "$", errors)
    return errors


def load_schema(name):
    with open(SCHEMA_DIR / ("%s.schema.json" % name), encoding="utf-8") as f:
        return json.load(f)
