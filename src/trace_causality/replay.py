"""Offline cross-clock evidence replay planning.

The planner validates the complete input document before deriving tight offset
bounds, possible span matches, causal ancestor closures, and minimum-cost
segment cover sets.  All decimal arithmetic uses :class:`fractions.Fraction`;
binary floating point is never used.
"""

from bisect import bisect_left, bisect_right
from fractions import Fraction

from .checks import array, ident, integer, number


SCHEMA = "trace-replay-plan-v1"
_DECIMAL_SCALE = 1_000_000

_TOP_FIELDS = {"reference", "clocks", "constraints", "spans", "segments", "requests"}
_CONSTRAINT_FIELDS = {"left", "right", "min", "max"}
_SPAN_FIELDS = {"id", "trace", "parent", "service", "start", "end", "clock"}
_SEGMENT_FIELDS = {"id", "clock", "from", "to", "bytes"}
_REQUEST_FIELDS = {"id", "trace", "from", "to"}


def plan_trace_replay(data):
    """Return a deterministic, minimum-read replay manifest."""
    model = _validate(data)

    clocks = model["clocks"]
    reference = model["reference"]
    span_by_id = model["span_by_id"]

    bounds = _clock_bounds(clocks, reference, model["constraints"])
    contexts = _build_clock_contexts(model)

    clock_output = [
        {
            "clock": clock,
            "min_offset": canonical_decimal(bounds[clock][0]),
            "max_offset": canonical_decimal(bounds[clock][1]),
        }
        for clock in sorted(clocks)
    ]

    requests_output = []
    cover_cache = {}
    for request in sorted(model["requests"], key=lambda item: item["id"]):
        trace = request["trace"]
        trace_spans = model["spans_by_trace"][trace]

        matched_ids = []
        for span in trace_spans:
            min_offset, max_offset = bounds[span["clock"]]
            # [start + O, end + O] intersects [request_from, request_to]
            # exactly when O lies in this interval.
            offset_lower = request["from"] - span["end"]
            offset_upper = request["to"] - span["start"]
            if max(min_offset, offset_lower) <= min(max_offset, offset_upper):
                matched_ids.append(span["id"])

        matched_set = set(matched_ids)
        protected_set = set(matched_set)
        stack = list(matched_set)
        while stack:
            span_id = stack.pop()
            parent_id = span_by_id[span_id]["parent"]
            if parent_id is not None and parent_id not in protected_set:
                protected_set.add(parent_id)
                stack.append(parent_id)

        matched_sorted = sorted(matched_ids)
        protected_sorted = sorted(protected_set)

        result = {
            "id": request["id"],
            "trace": trace,
            "from": canonical_decimal(request["from"]),
            "to": canonical_decimal(request["to"]),
            "status": "ready",
            "matched_spans": matched_sorted,
            "protected_spans": protected_sorted,
        }

        if not matched_sorted:
            result["segments"] = []
            result["total_bytes"] = 0
            requests_output.append(result)
            continue

        required_by_clock = {}
        for span_id in protected_sorted:
            span = span_by_id[span_id]
            required_by_clock.setdefault(span["clock"], []).append(
                (span["start"], span["end"])
            )

        missing = []
        cover_jobs = []
        for clock in sorted(required_by_clock):
            context = contexts[clock]
            coordinates = context["coordinates"]
            coordinate_index = context["coordinate_index"]

            required_ranges = _merge_intervals(required_by_clock[clock])
            required_elements = []
            for start, end in required_ranges:
                start_index = coordinate_index[start]
                end_index = coordinate_index[end]
                required_elements.extend(
                    range(2 * start_index, 2 * end_index + 1)
                )

            missing_ranges = _missing_ranges(
                required_elements, context["covered"], coordinates
            )
            if missing_ranges:
                for start, end in missing_ranges:
                    missing.append((clock, start, end))
            else:
                range_key = (clock, tuple(required_ranges))
                cover_jobs.append((clock, range_key, required_elements))

        if missing:
            missing.sort(key=lambda item: (item[0], item[1], item[2]))
            result["status"] = "incomplete"
            result["missing"] = [
                {
                    "clock": clock,
                    "from": canonical_decimal(start),
                    "to": canonical_decimal(end),
                }
                for clock, start, end in missing
            ]
            requests_output.append(result)
            continue

        total_bytes = 0
        selected_mask = 0
        for clock, cache_key, required_elements in cover_jobs:
            label = cover_cache.get(cache_key)
            if label is None:
                label = _optimal_cover(contexts[clock], required_elements)
                cover_cache[cache_key] = label
            if label is None:
                # Covered-element checks guarantee that a plan exists.
                raise RuntimeError("failed to plan an available segment cover")
            byte_cost, _count, mask = label
            total_bytes += byte_cost
            selected_mask |= mask

        result["segments"] = _decode_mask(
            selected_mask, model["segment_position_by_bit"]
        )
        result["total_bytes"] = total_bytes
        requests_output.append(result)

    return {
        "schema": SCHEMA,
        "clocks": clock_output,
        "requests": requests_output,
    }


def canonical_decimal(value):
    """Format an exact bounded decimal without floats or redundant zeros."""
    if value == 0:
        return "0"

    scaled = abs(value) * _DECIMAL_SCALE
    if not isinstance(scaled, Fraction) or scaled.denominator != 1:
        raise ValueError("decimal is not on the bounded decimal grid")

    whole, fraction = divmod(scaled.numerator, _DECIMAL_SCALE)
    sign = "-" if value < 0 else ""
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


def _validate(data):
    if not isinstance(data, dict) or set(data) != _TOP_FIELDS:
        raise ValueError("trace replay data must contain exactly the declared fields")

    raw_clocks = array(data["clocks"], 1, 64)
    clocks = []
    clock_set = set()
    for raw_clock in raw_clocks:
        clock = ident(raw_clock)
        if clock in clock_set:
            raise ValueError("clocks must be distinct")
        clock_set.add(clock)
        clocks.append(clock)

    reference = ident(data["reference"])
    if reference not in clock_set:
        raise ValueError("reference must be a declared clock")

    raw_constraints = array(data["constraints"], 0, 4096)
    constraints = []
    for raw in raw_constraints:
        if not isinstance(raw, dict) or set(raw) != _CONSTRAINT_FIELDS:
            raise ValueError("constraint must contain exactly left, right, min and max")
        left = ident(raw["left"])
        right = ident(raw["right"])
        if left not in clock_set or right not in clock_set:
            raise ValueError("constraint clocks must be declared")
        if left == right:
            raise ValueError("constraint left and right clocks must differ")
        minimum = number(raw["min"])
        maximum = number(raw["max"])
        if minimum > maximum:
            raise ValueError("constraint min must not exceed max")
        constraints.append(
            {"left": left, "right": right, "min": minimum, "max": maximum}
        )

    raw_spans = array(data["spans"], 1, 2000)
    spans = []
    span_ids = set()
    for raw in raw_spans:
        if not isinstance(raw, dict) or set(raw) != _SPAN_FIELDS:
            raise ValueError(
                "span must contain exactly id, trace, parent, service, "
                "start, end and clock"
            )

        span_id = ident(raw["id"])
        if span_id in span_ids:
            raise ValueError("duplicate span id")
        span_ids.add(span_id)

        trace = ident(raw["trace"])
        service = ident(raw["service"])
        parent = None if raw["parent"] is None else ident(raw["parent"])
        clock = ident(raw["clock"])
        if clock not in clock_set:
            raise ValueError("span clock must be declared")

        start = number(raw["start"])
        end = number(raw["end"])
        if end < start:
            raise ValueError("span end must not precede start")

        spans.append(
            {
                "id": span_id,
                "trace": trace,
                "parent": parent,
                "service": service,
                "clock": clock,
                "start": start,
                "end": end,
            }
        )

    span_by_id = {span["id"]: span for span in spans}
    spans_by_trace = {}
    for span in spans:
        spans_by_trace.setdefault(span["trace"], []).append(span)

    for span in spans:
        parent_id = span["parent"]
        if parent_id is not None:
            parent = span_by_id.get(parent_id)
            if parent is None or parent["trace"] != span["trace"]:
                raise ValueError("span parent must exist in the same trace")

    for span in spans:
        seen = {span["id"]}
        parent_id = span["parent"]
        while parent_id is not None:
            if parent_id in seen:
                raise ValueError("parent graph must not contain a cycle")
            seen.add(parent_id)
            parent_id = span_by_id[parent_id]["parent"]

    raw_segments = array(data["segments"], 1, 10000)
    segments = []
    segment_ids = set()
    for raw in raw_segments:
        if not isinstance(raw, dict) or set(raw) != _SEGMENT_FIELDS:
            raise ValueError("segment must contain exactly id, clock, from, to and bytes")

        segment_id = ident(raw["id"])
        if segment_id in segment_ids:
            raise ValueError("duplicate segment id")
        segment_ids.add(segment_id)

        clock = ident(raw["clock"])
        if clock not in clock_set:
            raise ValueError("segment clock must be declared")

        start = number(raw["from"])
        end = number(raw["to"])
        if start > end:
            raise ValueError("segment from must not exceed to")

        byte_count = integer(raw["bytes"], 1, 1_000_000_000)
        segments.append(
            {
                "id": segment_id,
                "clock": clock,
                "from": start,
                "to": end,
                "bytes": byte_count,
            }
        )

    raw_requests = array(data["requests"], 1, 200)
    requests = []
    request_ids = set()
    for raw in raw_requests:
        if not isinstance(raw, dict) or set(raw) != _REQUEST_FIELDS:
            raise ValueError("request must contain exactly id, trace, from and to")

        request_id = ident(raw["id"])
        if request_id in request_ids:
            raise ValueError("duplicate request id")
        request_ids.add(request_id)

        trace = ident(raw["trace"])
        start = number(raw["from"])
        end = number(raw["to"])
        if start > end:
            raise ValueError("request from must not exceed to")

        requests.append(
            {"id": request_id, "trace": trace, "from": start, "to": end}
        )

    for request in requests:
        if request["trace"] not in spans_by_trace:
            raise ValueError("request trace must be mentioned by spans")

    sorted_segment_ids = sorted(segment["id"] for segment in segments)
    bit_count = len(sorted_segment_ids)
    bit_by_segment_id = {}
    segment_position_by_bit = {}
    for rank, segment_id in enumerate(sorted_segment_ids):
        bit_position = bit_count - 1 - rank
        bit = 1 << bit_position
        bit_by_segment_id[segment_id] = bit
        segment_position_by_bit[bit_position] = segment_id

    return {
        "clocks": clocks,
        "reference": reference,
        "constraints": constraints,
        "spans": spans,
        "span_by_id": span_by_id,
        "spans_by_trace": spans_by_trace,
        "segments": segments,
        "requests": requests,
        "bit_by_segment_id": bit_by_segment_id,
        "segment_position_by_bit": segment_position_by_bit,
    }


def _clock_bounds(clocks, reference, constraints):
    """Compute tight reference-relative bounds with Floyd-Warshall."""
    index = {clock: position for position, clock in enumerate(sorted(clocks))}
    count = len(index)
    distance = [[None] * count for _ in range(count)]
    for i in range(count):
        distance[i][i] = Fraction(0)

    for constraint in constraints:
        left = index[constraint["left"]]
        right = index[constraint["right"]]

        # O_right <= O_left + max
        weight = constraint["max"]
        current = distance[left][right]
        if current is None or weight < current:
            distance[left][right] = weight

        # O_left <= O_right - min
        weight = -constraint["min"]
        current = distance[right][left]
        if current is None or weight < current:
            distance[right][left] = weight

    for k in range(count):
        row_k = distance[k]
        for i in range(count):
            through_k = distance[i][k]
            if through_k is None:
                continue
            row_i = distance[i]
            for j in range(count):
                prefix = row_k[j]
                if prefix is None:
                    continue
                candidate = through_k + prefix
                if row_i[j] is None or candidate < row_i[j]:
                    row_i[j] = candidate

    for i in range(count):
        if distance[i][i] is not None and distance[i][i] < 0:
            raise ValueError("clock constraints are unsatisfiable")

    reference_index = index[reference]
    bounds = {}
    for clock, clock_index in index.items():
        upper_path = distance[reference_index][clock_index]
        to_reference = distance[clock_index][reference_index]
        if upper_path is None or to_reference is None:
            raise ValueError("every clock must have finite bounds relative to reference")

        lower = -to_reference
        upper = upper_path
        if lower > upper:
            raise ValueError("clock constraints are unsatisfiable")
        bounds[clock] = (lower, upper)

    return bounds


def _build_clock_contexts(model):
    coordinates_by_clock = {clock: set() for clock in model["clocks"]}
    for span in model["spans"]:
        coordinates_by_clock[span["clock"]].add(span["start"])
        coordinates_by_clock[span["clock"]].add(span["end"])
    for segment in model["segments"]:
        coordinates_by_clock[segment["clock"]].add(segment["from"])
        coordinates_by_clock[segment["clock"]].add(segment["to"])

    segments_by_clock = {}
    for segment in model["segments"]:
        segments_by_clock.setdefault(segment["clock"], []).append(segment)

    contexts = {}
    bit_by_id = model["bit_by_segment_id"]
    for clock, coordinate_set in coordinates_by_clock.items():
        coordinates = sorted(coordinate_set)
        coordinate_index = {value: i for i, value in enumerate(coordinates)}
        element_count = max(0, 2 * len(coordinates) - 1)

        candidates = []
        difference = [0] * (element_count + 1)
        for segment in segments_by_clock.get(clock, ()):  # declared clock may be unused
            start_index = coordinate_index[segment["from"]]
            end_index = coordinate_index[segment["to"]]
            left = 2 * start_index
            right = 2 * end_index
            bit = bit_by_id[segment["id"]]

            # (right element, left element, id, byte cost, lex-order bit)
            candidates.append((right, left, segment["id"], segment["bytes"], bit))
            difference[left] += 1
            difference[right + 1] -= 1

        candidates.sort(key=lambda item: (item[0], item[1], item[2]))

        covered = bytearray(element_count)
        active = 0
        for i in range(element_count):
            active += difference[i]
            if active > 0:
                covered[i] = 1

        contexts[clock] = {
            "coordinates": coordinates,
            "coordinate_index": coordinate_index,
            "element_count": element_count,
            "candidates": candidates,
            "covered": covered,
        }

    return contexts


def _merge_intervals(intervals):
    """Merge closed intervals where they overlap or touch."""
    if not intervals:
        return []

    ordered = sorted(intervals, key=lambda item: (item[0], item[1]))
    merged = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        current_end = merged[-1][1]
        if start <= current_end:
            if end > current_end:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _missing_ranges(required_elements, covered, coordinates):
    """Convert uncovered atomic element runs to endpoint-bounded intervals."""
    if not required_elements:
        return []

    runs = []
    run_start = None
    run_end = None

    def append_run(start_element, end_element):
        start_coordinate = coordinates[start_element // 2]
        end_coordinate = coordinates[(end_element + 1) // 2]
        runs.append((start_coordinate, end_coordinate))

    for element in required_elements:
        if covered[element]:
            if run_start is not None:
                append_run(run_start, run_end)
                run_start = None
                run_end = None
            continue

        if run_start is None:
            run_start = element
            run_end = element
        elif element == run_end + 1:
            run_end = element
        else:
            append_run(run_start, run_end)
            run_start = element
            run_end = element

    if run_start is not None:
        append_run(run_start, run_end)

    # Runs separated only by a covered point produce equal boundary endpoints;
    # the contract asks for overlapping or touching gaps to be merged.
    return _merge_intervals(runs)


def _better_label(left, right):
    """Choose min bytes, then count, then lex-smallest sorted id list."""
    if left is None:
        return right
    if right is None:
        return left

    if left[0] != right[0]:
        return left if left[0] < right[0] else right
    if left[1] != right[1]:
        return left if left[1] < right[1] else right

    # A higher reverse-rank bitmask is a lexicographically smaller sorted list.
    return left if left[2] >= right[2] else right


def _optimal_cover(context, required_elements):
    """Shortest-path style weighted interval cover over atomic elements."""
    if not required_elements:
        return (0, 0, 0)

    element_count = context["element_count"]
    size = 1
    while size < element_count:
        size *= 2
    tree = [None] * (2 * size)

    def update(position, label):
        node = position + size
        tree[node] = _better_label(tree[node], label)
        node //= 2
        while node:
            tree[node] = _better_label(tree[node * 2], tree[node * 2 + 1])
            node //= 2

    def range_min(left, right):
        left += size
        right += size
        result = None
        while left < right:
            if left & 1:
                result = _better_label(result, tree[left])
                left += 1
            if right & 1:
                right -= 1
                result = _better_label(result, tree[right])
            left //= 2
            right //= 2
        return result

    required = required_elements
    candidates = context["candidates"]
    best_complete = None
    index = 0

    while index < len(candidates):
        right = candidates[index][0]
        next_index = index + 1
        while next_index < len(candidates) and candidates[next_index][0] == right:
            next_index += 1

        best_at_right = None
        for candidate_index in range(index, next_index):
            right_pos, left_pos, _segment_id, byte_cost, bit = candidates[candidate_index]

            before_left = bisect_left(required, left_pos) - 1
            if before_left < 0:
                predecessor = (0, 0, 0)
            else:
                threshold = required[before_left]
                # Strict right bound keeps same-right candidates sequential.
                predecessor = range_min(threshold, right_pos)

            if predecessor is None:
                continue

            label = (
                predecessor[0] + byte_cost,
                predecessor[1] + 1,
                predecessor[2] | bit,
            )
            best_at_right = _better_label(best_at_right, label)

        if best_at_right is not None:
            update(right, best_at_right)
            if bisect_right(required, right) == len(required):
                best_complete = _better_label(best_complete, best_at_right)

        index = next_index

    return best_complete


def _decode_mask(mask, id_by_bit_position):
    """Decode reverse-rank bits in ascending lexicographic id order."""
    ids = []
    while mask:
        position = mask.bit_length() - 1
        ids.append(id_by_bit_position[position])
        mask ^= 1 << position
    return ids
