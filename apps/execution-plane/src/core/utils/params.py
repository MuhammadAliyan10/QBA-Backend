import re
import logging
from typing import Dict, Any

logger = logging.getLogger("params")

def substitute_variables(text: str, available_params: dict[str, Any]) -> str:
    """
    Substitutes variables in a string.
    Supports both {{variable}} and {variable} syntax.
    """
    if not isinstance(text, str):
        return text

    # Regex to match {{var}} or {var}
    # Matches: {{ var }}, {{var}}, { var }, {var}
    pattern = re.compile(r'\{\{?\s*([a-zA-Z0-9_-]+)\s*\}?\}')

    def replace_match(match):
        var_name = match.group(1)
        if var_name in available_params:
            return str(available_params[var_name])
        # If not found, keep the placeholder
        return match.group(0)

    return pattern.sub(replace_match, text)

def validate_and_substitute(step_params: dict[str, Any], available_params: dict[str, Any]) -> dict[str, Any]:
    """
    Substitutes variables in all step parameters.
    """
    validated = {}
    for key, value in step_params.items():
        if isinstance(value, str):
            validated[key] = substitute_variables(value, available_params)
        else:
            validated[key] = value
    return validated
