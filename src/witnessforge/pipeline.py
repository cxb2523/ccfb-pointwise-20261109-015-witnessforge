"""Run the reference pipeline (decode -> map) and compare outputs."""

from __future__ import annotations

import json
from typing import Any, Dict

from .codec import decode_record
from .copybook import Layout
from .mapping import Mapping, apply_mapping

ERROR_KEY = "__error__"


def run_pipeline(data: bytes, layout: Layout, mapping: Mapping) -> Dict[str, Any]:
    """Decode a raw record and apply the mapping.

    Failures are returned as ``{"__error__": "<ExceptionType>"}`` rather than raised,
    so a mutant that crashes on an input counts as producing a different output.
    """
    try:
        return apply_mapping(decode_record(layout, data), mapping, layout)
    except Exception as exc:  # noqa: BLE001 - any failure is an observable difference
        return {ERROR_KEY: type(exc).__name__}


def map_record(record: Dict[str, Any], layout: Layout, mapping: Mapping) -> Dict[str, Any]:
    """Apply the mapping to an already decoded record, with the same error handling."""
    try:
        return apply_mapping(record, mapping, layout)
    except Exception as exc:  # noqa: BLE001
        return {ERROR_KEY: type(exc).__name__}


def is_error(doc: Dict[str, Any]) -> bool:
    return ERROR_KEY in doc


def canonical(doc: Any) -> str:
    """A type-aware canonical form, so 12.3 (float) and Decimal('12.30') differ."""
    return json.dumps(doc, sort_keys=True, default=repr)


def json_ready(value: Any) -> Any:
    """Convert Decimals and other non-JSON values into JSON-safe values."""
    return json.loads(json.dumps(value, default=str))
