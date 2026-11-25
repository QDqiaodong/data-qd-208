"""Resource metering settlement and quota admission.

This module attributes integer increments of cumulative service meters to the
deepest active same-service spans, conserves every unit, aggregates those
units to trace accounts, and evaluates independent quota what-if requests.

All time arithmetic uses :class:`fractions.Fraction`.  Input documents are
validated before a result is rendered, and inputs are never mutated.
"""

from fractions import Fraction

from .checks import array, fields, ident, integer, number
from .service import process


SCHEMA = "trace-meter-settlement-v1"
_MAX_INTEGER = 1_000_000_000_000_000

_TOP_FIELDS = {
    "spans",
    "meters",
    "ownership",
    "budgets",
    "requests",
}
_SPAN_FIELDS = ("id", "trace", "parent", "service", "start", "end")
_OWNERSHIP_FIELDS = ("trace", "account")
_METER_FIELDS = ("id", "service", "unit", "modulus", "readings")
_READING_FIELDS = ("at", "value")
_BUDGET_FIELDS = ("account", "service", "unit", "limit", "committed")
_REQUEST_FIELDS = ("id", "account", "items")
_REQUEST_ITEM_FIELDS = ("service", "unit", "units")


def settle_trace_metering(data):
    """Settle meter usage and evaluate independent quota admission requests."""
    model = _validate(data)
    meter_results, required_policies = _attribute_meters(model)
    _verify_policy_coverage(model, required_policies)
    return _render(model, meter_results)


def _validate(data):
    if not isinstance(data, dict) or set(data) != _TOP_FIELDS:
        raise ValueError(
            "metering input must contain exactly spans, meters, ownership, "
            "budgets and requests"
        )

    # Reuse the complete existing span contract and graph/time validation.
    process({"spans": data["spans"]})
    spans, by_id = _parse_spans(data["spans"])
    trace_ids = {span["trace"] for span in spans}
    ownership, accounts = _parse_ownership(data["ownership"], trace_ids)
    services = {span["service"] for span in spans}

    meters, meter_keys = _parse_meters(data["meters"], services)
    budgets = _parse_budgets(data["budgets"], accounts, meter_keys)
    requests = _parse_requests(data["requests"], accounts, budgets)

    return {
        "spans": spans,
        "by_id": by_id,
        "ownership": ownership,
        "accounts": accounts,
        "meters": meters,
        "budgets": budgets,
        "requests": requests,
    }


def _parse_spans(raw_spans):
    spans = []
    by_id = {}
    for raw in raw_spans:
        # process() has already validated this; parsing here gives us exact
        # Fraction values without duplicating graph validation.
        fields(raw, _SPAN_FIELDS)
        span_id = ident(raw["id"])
        parent = None if raw["parent"] is None else ident(raw["parent"])
        start = number(raw["start"])
        end = number(raw["end"])
        if end < start:
            raise ValueError("span end before start")
        span = {
            "id": span_id,
            "trace": ident(raw["trace"]),
            "parent": parent,
            "service": ident(raw["service"]),
            "start": start,
            "end": end,
        }
        spans.append(span)
        by_id[span_id] = span
    return spans, by_id


def _parse_ownership(raw_ownership, trace_ids):
    rows = array(raw_ownership, len(trace_ids), len(trace_ids))
    ownership = {}
    accounts = set()
    for raw in rows:
        fields(raw, _OWNERSHIP_FIELDS)
        trace = ident(raw["trace"])
        account = ident(raw["account"])
        if trace in ownership:
            raise ValueError("duplicate trace ownership")
        ownership[trace] = account
        accounts.add(account)

    if set(ownership) != trace_ids:
        raise ValueError("ownership must contain exactly one entry per trace")
    return ownership, accounts


def _parse_meters(raw_meters, span_services):
    meters = []
    meter_ids = set()
    meter_keys = set()

    for raw in array(raw_meters, 1, 256):
        fields(raw, _METER_FIELDS)
        meter_id = ident(raw["id"])
        if meter_id in meter_ids:
            raise ValueError("duplicate meter id")
        meter_ids.add(meter_id)

        service = ident(raw["service"])
        if service not in span_services:
            raise ValueError("meter service must be mentioned by a span")
        unit = ident(raw["unit"])

        modulus = raw["modulus"]
        if modulus is not None:
            modulus = integer(modulus, 2, _MAX_INTEGER)

        interval_models = _parse_readings(raw["readings"], modulus)
        meter = {
            "id": meter_id,
            "service": service,
            "unit": unit,
            "modulus": modulus,
            "interval_models": interval_models,
        }
        meters.append(meter)
        meter_keys.add((service, unit))

    return meters, meter_keys


def _parse_readings(raw_readings, modulus):
    readings = []
    seen_at = set()
    for raw in array(raw_readings, 2, 10_000):
        fields(raw, _READING_FIELDS)
        at = number(raw["at"])
        if at in seen_at:
            raise ValueError("meter reading times must be distinct")
        seen_at.add(at)

        value = integer(raw["value"], 0, _MAX_INTEGER)
        if modulus is not None and value >= modulus:
            raise ValueError("meter reading must be below modulus")
        readings.append({"at": at, "at_raw": raw["at"], "value": value})

    ordered = sorted(readings, key=lambda reading: reading["at"])
    intervals = []
    for previous, current in zip(ordered, ordered[1:]):
        if current["value"] >= previous["value"]:
            delta = current["value"] - previous["value"]
        else:
            if modulus is None:
                raise ValueError("cumulative meter decreased without modulus")
            # Every reading has already been checked against modulus, so this
            # is the unique single-rollover interpretation.
            delta = modulus - previous["value"] + current["value"]
        intervals.append((previous, current, delta))
    return intervals


def _parse_budgets(raw_budgets, accounts, meter_keys):
    budgets = {}
    for raw in array(raw_budgets, 1, 4096):
        fields(raw, _BUDGET_FIELDS)
        account = ident(raw["account"])
        if account not in accounts:
            raise ValueError("budget account must be declared by ownership")

        service = ident(raw["service"])
        unit = ident(raw["unit"])
        if (service, unit) not in meter_keys:
            raise ValueError("budget service/unit must be declared by a meter")

        key = (account, service, unit)
        if key in budgets:
            raise ValueError("duplicate budget account/service/unit")

        budgets[key] = {
            "account": account,
            "service": service,
            "unit": unit,
            "limit": integer(raw["limit"], 0, _MAX_INTEGER),
            "committed": integer(raw["committed"], 0, _MAX_INTEGER),
        }
    return budgets


def _parse_requests(raw_requests, accounts, budgets):
    requests = []
    request_ids = set()
    for raw in array(raw_requests, 0, 4096):
        fields(raw, _REQUEST_FIELDS)
        request_id = ident(raw["id"])
        if request_id in request_ids:
            raise ValueError("duplicate request id")
        request_ids.add(request_id)

        account = ident(raw["account"])
        if account not in accounts:
            raise ValueError("request account must be declared by ownership")

        items = []
        item_keys = set()
        for raw_item in array(raw["items"], 1, 256):
            fields(raw_item, _REQUEST_ITEM_FIELDS)
            service = ident(raw_item["service"])
            unit = ident(raw_item["unit"])
            item_key = (service, unit)
            if item_key in item_keys:
                raise ValueError("duplicate request service/unit item")
            item_keys.add(item_key)

            units = integer(raw_item["units"], 1, _MAX_INTEGER)
            policy_key = (account, service, unit)
            if policy_key not in budgets:
                raise ValueError("requested combination has no quota policy")
            items.append(
                {"service": service, "unit": unit, "units": units}
            )

        requests.append(
            {"id": request_id, "account": account, "items": items}
        )
    return requests


def _build_ancestor_masks(by_id):
    ordered_ids = sorted(by_id)
    index_by_id = {span_id: i for i, span_id in enumerate(ordered_ids)}
    bit_by_id = {
        span_id: 1 << index_by_id[span_id] for span_id in ordered_ids
    }
    ancestors = {}

    for span_id in ordered_ids:
        if span_id in ancestors:
            continue

        path = []
        current = span_id
        while current is not None and current not in ancestors:
            path.append(current)
            current = by_id[current]["parent"]

        for node in reversed(path):
            parent = by_id[node]["parent"]
            if parent is None:
                ancestors[node] = 0
            else:
                ancestors[node] = bit_by_id[parent] | ancestors[parent]

    return ordered_ids, bit_by_id, ancestors


def _build_service_timeline(service_spans, bit_by_id, ancestors, ids_by_index):
    positive_spans = [
        span for span in service_spans if span["end"] > span["start"]
    ]
    if not positive_spans:
        return []

    start_events = {}
    end_events = {}
    coordinates = set()
    for span in positive_spans:
        bit = bit_by_id[span["id"]]
        start = span["start"]
        end = span["end"]
        start_events[start] = start_events.get(start, 0) | bit
        end_events[end] = end_events.get(end, 0) | bit
        coordinates.add(start)
        coordinates.add(end)

    coordinates = sorted(coordinates)
    timeline = []
    active_mask = 0

    for left, right in zip(coordinates, coordinates[1:]):
        # Intervals are half-open: endings at this boundary are inactive,
        # while starts at this boundary are active.
        active_mask &= ~end_events.get(left, 0)
        active_mask |= start_events.get(left, 0)

        if active_mask == 0:
            continue

        # An active same-service strict ancestor is deprived of this elementary
        # interval whenever any active same-service descendant exists.
        deprived_mask = 0
        mask = active_mask
        while mask:
            bit = mask & -mask
            index = bit.bit_length() - 1
            span_id = ids_by_index[index]
            deprived_mask |= ancestors[span_id]
            mask -= bit
        deprived_mask &= active_mask

        owner_mask = active_mask & ~deprived_mask
        owners = []
        mask = owner_mask
        while mask:
            bit = mask & -mask
            index = bit.bit_length() - 1
            owners.append(ids_by_index[index])
            mask -= bit
        owners.sort()

        if owners:
            timeline.append((left, right, tuple(owners)))

    return timeline


def _collect_pieces(t0, t1, timeline, position):
    while position < len(timeline) and timeline[position][1] <= t0:
        position += 1

    pieces = []
    index = position
    while index < len(timeline) and timeline[index][0] < t1:
        left, right, owners = timeline[index]
        if right > t0:
            begin = max(left, t0)
            end = min(right, t1)
            duration = end - begin
            if duration > 0:
                pieces.append((duration, owners))

        if right <= t1:
            index += 1
        else:
            break

    return pieces, index


def _apportion(delta, pieces):
    """Return integer units by exact largest-remainder apportionment."""
    if not pieces or delta == 0:
        return {}

    # Fast path: one elementary ownership interval gives every owner the exact
    # same exposure.  All fractional remainders tie.
    if len(pieces) == 1:
        owners = pieces[0][1]
        count = len(owners)
        base, extra = divmod(delta, count)
        result = {}
        if base:
            for span_id in owners:
                result[span_id] = base
        for span_id in owners[:extra]:
            result[span_id] = result.get(span_id, 0) + 1
        return result

    exposures = {}
    for duration, owners in pieces:
        for span_id in owners:
            exposures[span_id] = exposures.get(span_id, Fraction(0, 1)) + duration

    total_exposure = sum(exposures.values(), Fraction(0, 1))
    if total_exposure <= 0:
        return {}

    rows = []
    floor_total = 0
    for span_id in sorted(exposures):
        exposure = exposures[span_id]
        numerator = (
            delta
            * exposure.numerator
            * total_exposure.denominator
        )
        denominator = (
            exposure.denominator
            * total_exposure.numerator
        )
        base, remainder_numerator = divmod(numerator, denominator)
        remainder = Fraction(remainder_numerator, denominator)
        rows.append((remainder, span_id, base))
        floor_total += base

    extra = delta - floor_total
    # Sort by descending remainder, then ascending span id for exact ties.
    rows.sort(key=lambda row: (-row[0], row[1]))
    if extra < 0 or extra > len(rows):
        raise RuntimeError("invalid meter apportionment remainder")

    result = {}
    for _remainder, span_id, base in rows:
        if base:
            result[span_id] = base
    for i in range(extra):
        span_id = rows[i][1]
        result[span_id] = result.get(span_id, 0) + 1

    if sum(result.values()) != delta:
        raise RuntimeError("meter apportionment failed conservation")
    return result


def _attribute_meters(model):
    by_id = model["by_id"]
    ownership = model["ownership"]
    ordered_ids, bit_by_id, ancestors = _build_ancestor_masks(by_id)
    ids_by_index = dict(enumerate(ordered_ids))

    spans_by_service = {}
    for span in model["spans"]:
        spans_by_service.setdefault(span["service"], []).append(span)

    meter_services = {meter["service"] for meter in model["meters"]}
    timelines = {
        service: _build_service_timeline(
            spans_by_service.get(service, []),
            bit_by_id,
            ancestors,
            ids_by_index,
        )
        for service in meter_services
    }

    meter_results = []
    required_policies = set()

    for meter in sorted(model["meters"], key=lambda item: item["id"]):
        timeline = timelines[meter["service"]]
        intervals = []
        total_delta = 0
        total_allocated = 0
        total_unassigned = 0
        position = 0

        for previous, current, delta in meter["interval_models"]:
            pieces, position = _collect_pieces(
                previous["at"], current["at"], timeline, position
            )
            units_by_span = _apportion(delta, pieces)

            allocations = []
            allocated = 0
            for span_id in sorted(units_by_span):
                units = units_by_span[span_id]
                if units <= 0:
                    continue
                span = by_id[span_id]
                account = ownership[span["trace"]]
                allocations.append(
                    {
                        "span": span_id,
                        "trace": span["trace"],
                        "account": account,
                        "units": units,
                    }
                )
                allocated += units
                required_policies.add(
                    (account, meter["service"], meter["unit"])
                )

            if pieces:
                unassigned = delta - allocated
                if unassigned != 0:
                    raise RuntimeError("assigned exposure did not conserve delta")
            else:
                unassigned = delta

            intervals.append(
                {
                    "from": previous["at_raw"],
                    "to": current["at_raw"],
                    "delta": delta,
                    "allocations": allocations,
                    "unassigned": unassigned,
                }
            )
            total_delta += delta
            total_allocated += allocated
            total_unassigned += unassigned

        meter_results.append(
            {
                "id": meter["id"],
                "service": meter["service"],
                "unit": meter["unit"],
                "intervals": intervals,
                "total_delta": total_delta,
                "total_allocated": total_allocated,
                "total_unassigned": total_unassigned,
            }
        )

    return meter_results, required_policies


def _verify_policy_coverage(model, required_policies):
    budgets = model["budgets"]
    for key in required_policies:
        if key not in budgets:
            raise ValueError(
                "settled account/service/unit combination has no quota policy"
            )

    # Request policies were checked during parsing; keep the complete policy
    # invariant explicit here as well.
    for request in model["requests"]:
        account = request["account"]
        for item in request["items"]:
            key = (account, item["service"], item["unit"])
            if key not in budgets:
                raise ValueError(
                    "requested combination has no quota policy"
                )


def _render(model, meter_results):
    by_id = model["by_id"]
    ownership = model["ownership"]
    budgets = model["budgets"]

    span_usage = {}
    settled = {}

    for meter in meter_results:
        service = meter["service"]
        unit = meter["unit"]
        for interval in meter["intervals"]:
            for allocation in interval["allocations"]:
                span_id = allocation["span"]
                account = allocation["account"]
                units = allocation["units"]

                usage_by_unit = span_usage.setdefault(span_id, {})
                usage_by_unit[unit] = usage_by_unit.get(unit, 0) + units

                key = (account, service, unit)
                settled[key] = settled.get(key, 0) + units

    spans_output = []
    for span_id in sorted(by_id):
        span = by_id[span_id]
        usage_by_unit = span_usage.get(span_id, {})
        usage = [
            {"unit": unit, "units": usage_by_unit[unit]}
            for unit in sorted(usage_by_unit)
            if usage_by_unit[unit] > 0
        ]
        spans_output.append(
            {
                "id": span_id,
                "trace": span["trace"],
                "service": span["service"],
                "account": ownership[span["trace"]],
                "usage": usage,
            }
        )

    budgets_output = []
    for key in sorted(budgets):
        budget = budgets[key]
        settled_units = settled.get(key, 0)
        used = budget["committed"] + settled_units
        remaining = budget["limit"] - used
        budgets_output.append(
            {
                "account": budget["account"],
                "service": budget["service"],
                "unit": budget["unit"],
                "limit": budget["limit"],
                "committed": budget["committed"],
                "settled": settled_units,
                "used": used,
                "remaining": remaining,
                "status": "within" if used <= budget["limit"] else "exceeded",
            }
        )

    requests_output = []
    for request in sorted(model["requests"], key=lambda item: item["id"]):
        deficits = []
        for item in sorted(
            request["items"], key=lambda entry: (entry["service"], entry["unit"])
        ):
            policy_key = (
                request["account"],
                item["service"],
                item["unit"],
            )
            budget = budgets[policy_key]
            used = (
                budget["committed"]
                + settled.get(
                    (request["account"], item["service"], item["unit"]), 0
                )
            )
            deficit = used + item["units"] - budget["limit"]
            if deficit > 0:
                deficits.append(
                    {
                        "service": item["service"],
                        "unit": item["unit"],
                        "deficit": deficit,
                    }
                )

        requests_output.append(
            {
                "id": request["id"],
                "account": request["account"],
                "status": "blocked" if deficits else "allowed",
                "deficits": deficits,
            }
        )

    return {
        "schema": SCHEMA,
        "meters": meter_results,
        "spans": spans_output,
        "budgets": budgets_output,
        "requests": requests_output,
    }
