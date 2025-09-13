"""Central parameter schema validation utility.

This lightweight validator provides a single place to define parameter
schemas for tools. It reduces scattered validation logic and produces
consistent error messages. Designed to be minimal and dependency-free.

Schema format (dict):
{
  "param_name": {
      "type": type|tuple[type,...],   # e.g. int, str
      "required": bool,               # default False
      "min": number,                  # for numeric types
      "max": number,                  # for numeric types
      "choices": [..],                # allowed values
      "default": any,                 # applied if missing & not required
      "nullable": bool                # if True allows None
  }, ...
}

Return: (validated_dict, errors_list)
If errors_list is empty, validation succeeded.
"""
from __future__ import annotations
from typing import Any, Dict, Tuple, List


def validate_params(schema: Dict[str, Dict[str, Any]], values: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    validated = {}
    errors: List[str] = []

    for name, spec in schema.items():
        val = values.get(name, None)
        required = spec.get("required", False)
        nullable = spec.get("nullable", False)
        expected_type = spec.get("type", Any)

        # Default handling
        if val is None:
            if name not in values and "default" in spec:
                val = spec["default"]
            elif required and not nullable:
                errors.append(f"'{name}' is required")
                continue
            elif val is None and not required and "default" not in spec:
                validated[name] = None
                continue

        # Null acceptance
        if val is None and nullable:
            validated[name] = None
            continue
        elif val is None and required:
            errors.append(f"'{name}' is required and cannot be null")
            continue

        # Type checking (allow simple coercion for int/float)
        if expected_type is not Any and val is not None:
            if expected_type in (int, float) and isinstance(val, str):
                try:
                    val = expected_type(val)
                except Exception:
                    errors.append(f"'{name}' must be of type {getattr(expected_type,'__name__',expected_type)}")
                    continue
            if not isinstance(val, expected_type):
                # allow tuple of types
                if not (isinstance(expected_type, tuple) and isinstance(val, expected_type)):
                    errors.append(f"'{name}' must be of type {getattr(expected_type,'__name__',expected_type)}")
                    continue

        # Numeric bounds
        if isinstance(val, (int, float)):
            if "min" in spec and val < spec["min"]:
                errors.append(f"'{name}' must be >= {spec['min']}")
            if "max" in spec and val > spec["max"]:
                errors.append(f"'{name}' must be <= {spec['max']}")

        # Choices
        if spec.get("choices") and val not in spec["choices"]:
            choices_list = ", ".join(map(str, spec["choices"]))
            errors.append(f"'{name}' must be one of: {choices_list}")

        validated[name] = val

    return validated, errors


def format_errors(errors: List[str]) -> str:
    return "; ".join(errors)
