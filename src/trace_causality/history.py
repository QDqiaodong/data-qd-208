"""Compile out-of-order, repeated span revisions into frozen trace histories."""

from bisect import bisect_right
from collections import defaultdict

from .checks import array, fields, ident, integer, number
from .service import process


SCHEMA = "trace-history-v1"
_TOP_FIELDS = {"sources", "events", "releases"}
_EVENT_BASE_FIELDS = ("source", "seq", "event_id", "trace", "op")
_SPAN_FIELDS = ("id", "trace", "parent", "service", "start", "end")
_MAX_ID = 1_000_000_000


def compile_trace_history(data):
    """Validate a revision ledger and compile current heads and frozen releases."""
    if not isinstance(data, dict) or set(data) != _TOP_FIELDS:
        raise ValueError("trace history must contain exactly sources, events and releases")

    source_values = array(data["sources"], 1, 16)
    source_names = sorted(ident(source) for source in source_values)
    if len(set(source_names)) != len(source_names):
        raise ValueError("sources must be distinct")
    source_set = set(source_names)

    raw_events = array(data["events"], 1, 100_000)
    raw_releases = array(data["releases"], 0, 2_000)

    raw_by_source_seq = {}
    event_ids = set()
    records = []
    owners = {}
    mentioned_traces = set()

    # Validate and collapse only exact transport resends before materialization.
    for raw_event in raw_events:
        record = _validate_event(raw_event, source_set)
        source_seq = (record["source"], record["seq"])
        previous_raw = raw_by_source_seq.get(source_seq)
        if previous_raw is not None:
            if raw_event != previous_raw:
                raise ValueError("conflicting event at same source and seq")
            continue

        event_id = record["event_id"]
        if event_id in event_ids:
            raise ValueError("event_id identifies conflicting logical events")

        ownership_key = (record["trace"], record["span_id"])
        owner = owners.get(ownership_key)
        if owner is not None and owner != record["source"]:
            raise ValueError("span is owned by another source")

        owners[ownership_key] = record["source"]
        raw_by_source_seq[source_seq] = raw_event
        event_ids.add(event_id)
        records.append(record)
        mentioned_traces.add(record["trace"])

    release_groups = _validate_releases(raw_releases, source_names)

    seqs_by_source = {source: set() for source in source_names}
    for record in records:
        seqs_by_source[record["source"]].add(record["seq"])

    sorted_seqs = {}
    gaps = {}
    gap_starts = {}
    for source in source_names:
        seqs = sorted(seqs_by_source[source])
        sorted_seqs[source] = seqs

        source_gaps = []
        previous = 0
        for seq in seqs:
            if seq > previous + 1:
                source_gaps.append((previous + 1, seq - 1))
            previous = seq
        gaps[source] = source_gaps
        gap_starts[source] = [start for start, _end in source_gaps]

    head_through = {}
    for source in source_names:
        contiguous = 0
        present = seqs_by_source[source]
        while contiguous + 1 in present:
            contiguous += 1
        head_through[source] = contiguous

    events_by_trace = defaultdict(list)
    events_by_trace_source = defaultdict(list)
    for record in records:
        events_by_trace[record["trace"]].append(record)
        events_by_trace_source[(record["trace"], record["source"])].append(record)

    for trace_events in events_by_trace.values():
        trace_events.sort(key=lambda item: (item["source"], item["seq"]))

    source_index = {}
    for key, items in events_by_trace_source.items():
        items.sort(key=lambda item: item["seq"])
        source_index[key] = ([item["seq"] for item in items], items)

    heads = []
    for trace in sorted(mentioned_traces):
        active = _materialize_complete(
            events_by_trace.get(trace, ()), head_through
        )
        spans = _snapshot(active)
        try:
            analysis = process({"spans": spans})
        except ValueError as exc:
            heads.append({
                "trace": trace,
                "status": "invalid",
                "error": str(exc),
            })
        else:
            heads.append({
                "trace": trace,
                "status": "ready",
                "spans": spans,
                "analysis": analysis,
            })

    releases = []
    for trace in sorted(release_groups):
        active = {}
        last_through = {source: 0 for source in source_names}

        for release in release_groups[trace]:
            through = release["through"]
            missing = []
            for source in source_names:
                for start, end in _missing_ranges(
                    source, through[source], sorted_seqs, gaps, gap_starts
                ):
                    missing.append({
                        "source": source,
                        "from": start,
                        "to": end,
                    })

            item = {
                "id": release["id"],
                "trace": trace,
                "version": release["version"],
                "through": dict(through),
                "status": "pending",
            }

            if missing:
                item["missing"] = missing
                releases.append(item)
                continue

            # Versions are componentwise monotonic, so only the new seq window
            # is needed.  Ownership makes source application order irrelevant.
            for source in source_names:
                seqs, items = source_index.get((trace, source), ((), ()))
                start_index = bisect_right(seqs, last_through[source])
                end_index = bisect_right(seqs, through[source])
                for record in items[start_index:end_index]:
                    _apply(record, active)
                last_through[source] = through[source]

            spans = _snapshot(active)
            try:
                analysis = process({"spans": spans})
            except ValueError as exc:
                item["status"] = "invalid"
                item["error"] = str(exc)
            else:
                item["status"] = "ready"
                item["spans"] = spans
                item["analysis"] = analysis
            releases.append(item)

    return {
        "schema": SCHEMA,
        "sources": list(source_names),
        "head_through": dict(head_through),
        "heads": heads,
        "releases": releases,
    }


def _validate_event(raw_event, source_set):
    if not isinstance(raw_event, dict):
        raise ValueError("event must be an object")

    missing = [field for field in _EVENT_BASE_FIELDS if field not in raw_event]
    if missing:
        raise ValueError("event is missing required fields")

    op = raw_event["op"]
    if op == "upsert":
        extra_field = "span"
    elif op == "delete":
        extra_field = "span_id"
    else:
        raise ValueError("event op must be upsert or delete")

    if set(raw_event) != set(_EVENT_BASE_FIELDS) | {extra_field}:
        raise ValueError("event has unknown or missing fields")

    source = raw_event["source"]
    if not isinstance(source, str) or source not in source_set:
        raise ValueError("event source must be one of the declared sources")

    seq = integer(raw_event["seq"], 1, _MAX_ID)
    event_id = ident(raw_event["event_id"])
    trace = ident(raw_event["trace"])

    record = {
        "source": source,
        "seq": seq,
        "event_id": event_id,
        "trace": trace,
        "op": op,
    }

    if op == "delete":
        record["span_id"] = ident(raw_event["span_id"])
        return record

    raw_span = raw_event["span"]
    fields(raw_span, _SPAN_FIELDS)

    span_id = ident(raw_span["id"])
    span_trace = ident(raw_span["trace"])
    parent = raw_span["parent"]
    if parent is not None:
        parent = ident(parent)
    service = ident(raw_span["service"])
    start = number(raw_span["start"])
    end = number(raw_span["end"])
    if end < start:
        raise ValueError("end before start")
    if span_trace != trace:
        raise ValueError("span trace must equal event trace")

    record["span_id"] = span_id
    record["span"] = {
        "id": span_id,
        "trace": span_trace,
        "parent": parent,
        "service": service,
        "start": raw_span["start"],
        "end": raw_span["end"],
    }
    return record


def _validate_releases(raw_releases, source_names):
    seen_ids = set()
    groups = defaultdict(list)

    for raw_release in raw_releases:
        if not isinstance(raw_release, dict) or set(raw_release) != {
            "id", "trace", "version", "through"
        }:
            raise ValueError("release must contain exactly id, trace, version and through")

        release_id = ident(raw_release["id"])
        if release_id in seen_ids:
            raise ValueError("release id must be globally unique")
        seen_ids.add(release_id)

        trace = ident(raw_release["trace"])
        version = raw_release["version"]
        if type(version) is not int or version < 1:
            raise ValueError("release version must be a positive integer")

        raw_through = raw_release["through"]
        fields(raw_through, tuple(source_names))
        through = {
            source: integer(raw_through[source], 0, _MAX_ID)
            for source in source_names
        }

        groups[trace].append({
            "id": release_id,
            "trace": trace,
            "version": version,
            "through": through,
        })

    for trace, releases in groups.items():
        ordered = sorted(releases, key=lambda release: release["version"])
        versions = [release["version"] for release in ordered]
        if versions != list(range(1, len(ordered) + 1)):
            raise ValueError("release versions for a trace must start at one and be consecutive")

        previous_through = None
        for release in ordered:
            through = release["through"]
            if previous_through is not None:
                increased = False
                for source in source_names:
                    if through[source] < previous_through[source]:
                        raise ValueError("release through vectors must be monotonic")
                    if through[source] > previous_through[source]:
                        increased = True
                if not increased:
                    raise ValueError("a later release vector must increase at least one component")
            previous_through = through

        groups[trace] = ordered

    return groups


def _missing_ranges(source, cutoff, sorted_seqs, gaps, gap_starts):
    if cutoff <= 0:
        return

    source_gaps = gaps[source]
    count = bisect_right(gap_starts[source], cutoff)
    for index in range(count):
        start, end = source_gaps[index]
        yield start, min(end, cutoff)

    seqs = sorted_seqs[source]
    last = seqs[-1] if seqs else 0
    if cutoff > last:
        yield last + 1, cutoff


def _materialize_complete(trace_events, through):
    active = {}
    for record in trace_events:
        if record["seq"] <= through[record["source"]]:
            _apply(record, active)
    return active


def _apply(record, active):
    span_id = record["span_id"]
    if record["op"] == "delete":
        active.pop(span_id, None)
    else:
        active[span_id] = dict(record["span"])


def _snapshot(active):
    return [dict(active[span_id]) for span_id in sorted(active)]
