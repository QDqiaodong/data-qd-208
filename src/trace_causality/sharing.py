"""Keyed pseudonymous projection for limited trace sharing."""

import hashlib
import hmac

from .checks import number
from .service import process


_SPAN_ID_NAMESPACE = b"trace-causality.sharing/v1/span-id"
_TRACE_NAMESPACE = b"trace-causality.sharing/v1/trace"
_SERVICE_NAMESPACE = b"trace-causality.sharing/v1/service"
_NAMESPACE_SEPARATOR = b"\x00"
_DECIMAL_SCALE = 1_000_000


def _pseudonym(key, namespace, value):
    """Return the fixed-length truncated HMAC pseudonym for one value."""
    message = namespace + _NAMESPACE_SEPARATOR + value.encode("ascii")
    digest = hmac.new(key, message, hashlib.sha256).hexdigest()
    return "x" + digest[:63]


class _PseudonymRegistry:
    """Map values in isolated namespaces while detecting output collisions."""

    def __init__(self, key):
        self._key = key
        self._values = {}
        self._owners_by_pseudonym = {}

    def map_value(self, namespace, value):
        category = self._values.setdefault(namespace, {})
        existing = category.get(value)
        if existing is not None:
            return existing

        token = _pseudonym(self._key, namespace, value)
        identity = (namespace, value)
        owner = self._owners_by_pseudonym.get(token)
        if owner is not None and owner != identity:
            raise ValueError("pseudonym collision for distinct identities")

        category[value] = token
        self._owners_by_pseudonym[token] = identity
        return token


def _format_decimal(value):
    """Format an exact bounded decimal value without binary floating point."""
    if value == 0:
        return "0"

    sign = "-" if value < 0 else ""
    scaled = abs(value) * _DECIMAL_SCALE
    if scaled.denominator != 1:
        raise ValueError("timestamp is not an exact decimal string")

    whole, fraction = divmod(scaled.numerator, _DECIMAL_SCALE)
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


def project_for_sharing(data, key):
    # Run the complete existing validator first.  It does not mutate data.
    process(data)

    if not isinstance(key, bytes) or len(key) == 0:
        raise ValueError("key must be non-empty bytes")

    spans = data["spans"]
    registry = _PseudonymRegistry(key)

    # Parent values are foreign keys into the same span-id identity space;
    # using a separate parent namespace would break parent references.
    for span in spans:
        registry.map_value(_SPAN_ID_NAMESPACE, span["id"])
        if span["parent"] is not None:
            registry.map_value(_SPAN_ID_NAMESPACE, span["parent"])
        registry.map_value(_TRACE_NAMESPACE, span["trace"])
        registry.map_value(_SERVICE_NAMESPACE, span["service"])

    earliest_start = {}
    for span in spans:
        trace = span["trace"]
        start = number(span["start"])
        if trace not in earliest_start or start < earliest_start[trace]:
            earliest_start[trace] = start

    projected_spans = []
    for span in spans:
        offset = earliest_start[span["trace"]]
        projected_spans.append({
            "id": registry.map_value(_SPAN_ID_NAMESPACE, span["id"]),
            "trace": registry.map_value(_TRACE_NAMESPACE, span["trace"]),
            "parent": (
                None
                if span["parent"] is None
                else registry.map_value(_SPAN_ID_NAMESPACE, span["parent"])
            ),
            "service": registry.map_value(_SERVICE_NAMESPACE, span["service"]),
            "start": _format_decimal(number(span["start"]) - offset),
            "end": _format_decimal(number(span["end"]) - offset),
        })

    projected = {"spans": projected_spans}

    # Defensive contract check: uniform translation and identity mapping must
    # always leave a document accepted by the unchanged processing API.
    process(projected)
    return projected
