"""The small JSON Schema subset the ten pinned tool schemas use, checked before anything runs.

Biorouter does not check tool arguments against the advertised schema, and neither does it
check elicitation answers, so the wrapper does both itself.
"""
import math


def _type_ok(value, kind):
    if kind == "object":
        return isinstance(value, dict)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and math.isfinite(value) and value.is_integer()
    if kind == "number":
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))
    if kind == "array":
        return isinstance(value, list)
    if kind == "null":
        return value is None
    return False


def errors(value, schema, path="arguments"):
    """Every way `value` breaks `schema`, as short sentences; empty when it fits."""
    found = []
    if not isinstance(schema, dict):
        return found
    if "anyOf" in schema:
        if not any(not errors(value, option, path) for option in schema["anyOf"]):
            found.append(f"{path} has an unsupported value")
        return found
    kind = schema.get("type")
    if kind and not _type_ok(value, kind):
        found.append(f"{path} must be {'an' if kind in ('object', 'integer', 'array') else 'a'} {kind}")
        return found
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{path} must be one of: {', '.join(str(v) for v in schema['enum'])}")
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        found.append(f"{path} must not be empty")
    if not isinstance(value, bool) and isinstance(value, (int, float)):
        if "minimum" in schema and value < schema["minimum"]:
            found.append(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            found.append(f"{path} must be at most {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            found.append(f"{path} must be greater than {schema['exclusiveMinimum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                found.append(f"missing required argument: {name}")
        if schema.get("additionalProperties") is False:
            for name in sorted(value):
                if name not in properties:
                    found.append(f"unknown argument: {name}")
        for name, sub in properties.items():
            if name in value:
                found.extend(errors(value[name], sub, name))
    return found
