"""Each gateway TypedDict says exactly what its JSON Schema says: same keys, same required keys,
same JSON types, enums and nesting. The schema stays the source of truth."""

from __future__ import annotations

import ast
import types
import typing
from pathlib import Path
from typing import Any, Literal, NotRequired, Required, get_args, get_origin, get_type_hints

import pytest

from relaydeck.contracts.schemas import SCHEMAS, TYPED_SCHEMAS

GATEWAY = Path(__file__).resolve().parents[2] / "src" / "relaydeck" / "gateway"
SCALARS = {"string": str, "integer": int, "boolean": bool, "number": float}


def _unwrap(hint: Any) -> tuple[Any, bool]:
    """The type without Required/NotRequired, and whether the key is required."""
    if get_origin(hint) is NotRequired:
        return get_args(hint)[0], False
    if get_origin(hint) is Required:
        return get_args(hint)[0], True
    return hint, True


def _is_typed_dict(hint: Any) -> bool:
    return typing.is_typeddict(hint)


def _check(schema: dict[str, Any], hint: Any, root: dict[str, Any], where: str) -> None:
    if "$ref" in schema:
        schema = root["$defs"][schema["$ref"].split("/")[-1]]
    if "enum" in schema or "const" in schema:
        values = schema["enum"] if "enum" in schema else [schema["const"]]
        assert get_origin(hint) is Literal, f"{where}: expected Literal, got {hint}"
        assert set(get_args(hint)) == set(values), f"{where}: enum {get_args(hint)} != {values}"
        return
    kind = schema.get("type")
    if isinstance(kind, list):
        assert "null" in kind, where
        assert get_origin(hint) in (typing.Union, types.UnionType), f"{where}: expected | None"
        assert type(None) in get_args(hint), f"{where}: expected | None"
        inner = [arg for arg in get_args(hint) if arg is not type(None)]
        assert len(inner) == 1, where
        (kind,) = [k for k in kind if k != "null"]
        _check({**schema, "type": kind}, inner[0], root, where)
        return
    if kind in SCALARS:
        assert hint is SCALARS[kind], f"{where}: expected {SCALARS[kind]}, got {hint}"
    elif kind == "array":
        assert get_origin(hint) is list, f"{where}: expected list, got {hint}"
        _check(schema["items"], get_args(hint)[0], root, f"{where}[]")
    elif kind == "object" and "properties" in schema:
        assert _is_typed_dict(hint), f"{where}: expected a TypedDict, got {hint}"
        _check_object(schema, hint, root, where)
    elif kind == "object":
        assert get_origin(hint) is dict, f"{where}: expected dict, got {hint}"
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict) and extra.get("type") == "string":
            assert get_args(hint) == (str, str), f"{where}: expected dict[str, str]"
    else:
        raise AssertionError(f"{where}: unmapped schema {schema}")


def _check_object(schema: dict[str, Any], typed: Any, root: dict[str, Any], where: str) -> None:
    hints = get_type_hints(typed, include_extras=True)
    props = schema["properties"]
    assert set(hints) == set(props), f"{where}: keys differ: {set(hints) ^ set(props)}"
    required = set(schema.get("required", []))
    assert typed.__required_keys__ == required, f"{where}: required keys differ"
    for key, prop in props.items():
        hint, is_required = _unwrap(hints[key])
        assert is_required == (key in required), f"{where}.{key}: required flag differs"
        _check(prop, hint, root, f"{where}.{key}")


@pytest.mark.parametrize("name", sorted(TYPED_SCHEMAS))
def test_typed_dict_matches_json_schema(name: str) -> None:
    schema = SCHEMAS[name]
    typed = TYPED_SCHEMAS[name]
    assert _is_typed_dict(typed), name
    _check_object(schema, typed, schema, name)


def test_every_schema_has_a_typed_dict() -> None:
    assert set(SCHEMAS) == set(TYPED_SCHEMAS)


def _validated_names() -> set[str]:
    names: set[str] = set()
    for file in GATEWAY.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "validate"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(node.args[0].value)
    return names


def test_every_schema_the_gateway_validates_is_typed() -> None:
    names = _validated_names()
    assert names, "no validate() calls found"
    assert names - set(TYPED_SCHEMAS) == set()
