import typing
from voluptuous import Invalid, MultipleInvalid
from voluptuous.error import Error
from voluptuous.schema_builder import Schema
MAX_VALIDATION_ERROR_ITEM_LENGTH = 500

def humanize_error(data, validation_error: Invalid, max_sub_error_length: int=MAX_VALIDATION_ERROR_ITEM_LENGTH) -> str:
    """Provide a more helpful + complete validation error message than that provided automatically
    Invalid and MultipleInvalid do not include the offending value in error messages,
    and MultipleInvalid.__str__ only provides the first error.
    """
    if isinstance(validation_error, MultipleInvalid):
        return _format_multiple_invalid(data, validation_error, max_sub_error_length)
    else:
        return _format_single_invalid(data, validation_error, max_sub_error_length)

def _format_multiple_invalid(data, error: MultipleInvalid, max_length: int) -> str:
    errors = []
    for sub_error in error.errors:
        errors.append(_format_single_invalid(data, sub_error, max_length))
    return "Multiple errors:\n" + "\n".join(errors)

def _format_single_invalid(data, error: Invalid, max_length: int) -> str:
    path = " @ data[" + "][".join(repr(p) for p in error.path) + "]" if error.path else ""
    
    # Try to get the invalid value
    try:
        invalid_value = _get_by_path(data, error.path)
        value_str = repr(invalid_value)
        if len(value_str) > max_length:
            value_str = value_str[:max_length] + "..."
    except Exception:
        value_str = "<unavailable>"

    return f"{str(error)}\nGot value: {value_str}{path}"

def _get_by_path(data, path):
    """Retrieve a value from nested dictionaries using a list of keys."""
    for key in path:
        if isinstance(data, dict):
            data = data[key]
        elif isinstance(data, (list, tuple)) and isinstance(key, int):
            data = data[key]
        else:
            raise KeyError(f"Unable to access {key} in {type(data)}")
    return data
