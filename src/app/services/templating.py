import re
from typing import Any


PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]{0,63})\}\}")


class TemplateError(ValueError):
    code = "INVALID_TEMPLATE"


class VariableNotFoundError(TemplateError):
    code = "VARIABLE_NOT_FOUND"


def render_template(value: Any, variables: dict[str, Any]) -> Any:
    """Recursively render values while leaving object keys unchanged."""
    if isinstance(value, str):
        match = PLACEHOLDER.fullmatch(value)
        if match:
            return _lookup(match.group(1), variables)

        def replace(item: re.Match[str]) -> str:
            resolved = _lookup(item.group(1), variables)
            if isinstance(resolved, (dict, list)):
                raise TemplateError(
                    "embedded placeholders require a scalar variable"
                )
            if resolved is None:
                return "null"
            if isinstance(resolved, bool):
                return "true" if resolved else "false"
            return str(resolved)

        return PLACEHOLDER.sub(replace, value)
    if isinstance(value, list):
        return [render_template(item, variables) for item in value]
    if isinstance(value, dict):
        return {
            key: render_template(item, variables)
            for key, item in value.items()
        }
    return value


def _lookup(name: str, variables: dict[str, Any]) -> Any:
    if name not in variables:
        raise VariableNotFoundError(f"variable {name!r} is not defined")
    return variables[name]
