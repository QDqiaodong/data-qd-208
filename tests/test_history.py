import copy
import json
import unittest

from trace_causality import compile_trace_history, process


def make_span(
    span_id,
    trace="trace-a",
    parent=None,
    service="api",
    start="0",
    end="10",
):
    return {
        "id": span_id,
        "trace": trace,
        "parent": parent,
        "service": service,
        "start": start,
        "end": end,
    }


def make_event(
    source,
    seq,
    event_id=None,
    trace="trace-a",
    op="upsert",
    span=None,
    span_id=None,
):
    event = {
        "source": source,
        "seq": seq,
        "event_id": event_id or f"{source}-{seq}",
        "trace": trace,
        "op": op,
    }
    if op == "upsert":
        event["span"] = (
            span
            if span is not None
            else make_span(f"span-{source}-{seq}", trace=trace)
        )
    else:
        event["span_id"] = span_id or f"span-{source}-{seq}"
    return event


def make_release(release_id, trace, version, through):
    return {
        "id": release_id,
        "trace": trace,
        "version": version,
        "through": through,
    }


def base_data(events=None, releases=None, sources=("src-a", "src-b")):
    return {
        "sources": list(sources),
        "events": events or [],
        "releases": releases or [],
    }


def zero_through(**overrides):
    through = {"src-a": 0, "src-b": 0}
    through.update(overrides)
    return through


class HistoryCompilationTest(unittest.TestCase):
    def test_gap_withholds_later_head_events_and_makes_vector_pending(self):
        data = base_data(
            events=[
                make_event("src-a", 1, "a1", span=make_span("root")),
                make_event("src-a", 3, "a3", span=make_span("late", parent="root")),
            ],
            releases=[
                make_release("ready", "trace-a", 1, zero_through(**{"src-a": 1})),
                make_release("pending", "trace-a", 2, zero_through(**{"src-a": 3})),
            ],
        )

        result = compile_trace_history(data)

        self.assertEqual(result["schema"], "trace-history-v1")
        self.assertEqual(result["sources"], ["src-a", "src-b"])
        self.assertEqual(result["head_through"], {"src-a": 1, "src-b": 0})

        head = result["heads"][0]
        self.assertEqual(head["status"], "ready")
        self.assertEqual([span["id"] for span in head["spans"]], ["root"])

        ready, pending = result["releases"]
        self.assertEqual(ready["status"], "ready")
        self.assertEqual([span["id"] for span in ready["spans"]], ["root"])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(
            pending["missing"],
            [{"source": "src-a", "from": 2, "to": 2}],
        )
        self.assertNotIn("spans", pending)
        self.assertNotIn("analysis", pending)

    def test_missing_numbers_merge_adjacent_ranges_and_sort_by_source(self):
        data = base_data(events=[
            make_event("src-a", 1, "a1", span=make_span("one")),
            make_event("src-a", 4, "a4", span=make_span("four")),
        ], releases=[
            make_release("pending", "trace-a", 1,
                         {"src-a": 6, "src-b": 2}),
        ])

        release = compile_trace_history(data)["releases"][0]
        self.assertEqual(release["status"], "pending")
        self.assertEqual(release["missing"], [
            {"source": "src-a", "from": 2, "to": 3},
            {"source": "src-a", "from": 5, "to": 6},
            {"source": "src-b", "from": 1, "to": 2},
        ])

    def test_filled_gap_applies_delete_absent_delete_and_revival_in_seq_order(self):
        data = base_data(events=[
            make_event("src-a", 1, "a1", op="delete", span_id="missing"),
            make_event("src-a", 2, "a2", span=make_span("revived", end="1")),
            make_event("src-a", 3, "a3", op="delete", span_id="revived"),
            make_event("src-a", 4, "a4", span=make_span("revived", end="2")),
        ], releases=[
            make_release("v1", "trace-a", 1, zero_through(**{"src-a": 1})),
            make_release("v2", "trace-a", 2, zero_through(**{"src-a": 2})),
            make_release("v3", "trace-a", 3, zero_through(**{"src-a": 3})),
            make_release("v4", "trace-a", 4, zero_through(**{"src-a": 4})),
        ])

        result = compile_trace_history(data)

        self.assertEqual(result["head_through"]["src-a"], 4)
        head = result["heads"][0]
        self.assertEqual(head["status"], "ready")
        self.assertEqual(len(head["spans"]), 1)
        self.assertEqual(head["spans"][0]["id"], "revived")
        self.assertEqual(head["spans"][0]["end"], "2")

        statuses = [item["status"] for item in result["releases"]]
        self.assertEqual(statuses, ["invalid", "ready", "invalid", "ready"])
        self.assertEqual(result["releases"][1]["spans"][0]["end"], "1")
        self.assertEqual(result["releases"][3]["spans"][0]["end"], "2")

    def test_exact_resend_collapses_but_conflicting_resend_rejects_all(self):
        event = make_event("src-a", 1, "same", span=make_span("root"))
        data = base_data(events=[event, copy.deepcopy(event)])

        result = compile_trace_history(data)
        self.assertEqual(len(result["heads"][0]["spans"]), 1)

        conflicting = copy.deepcopy(data)
        conflicting["events"].append(copy.deepcopy(event))
        conflicting["events"][-1]["span"]["end"] = "9"
        before = copy.deepcopy(conflicting)
        with self.assertRaises(ValueError):
            compile_trace_history(conflicting)
        self.assertEqual(conflicting, before)

        reused_id = base_data(events=[
            make_event("src-a", 1, "duplicate-id", span=make_span("one")),
            make_event("src-a", 2, "duplicate-id", span=make_span("two")),
        ])
        with self.assertRaises(ValueError):
            compile_trace_history(reused_id)

    def test_cross_source_ownership_conflict_is_rejected_including_initial_delete(self):
        upsert_conflict = base_data(events=[
            make_event("src-a", 1, "a1", span=make_span("shared")),
            make_event("src-b", 1, "b1", op="delete", span_id="shared"),
        ])
        with self.assertRaises(ValueError):
            compile_trace_history(upsert_conflict)

        delete_first = base_data(events=[
            make_event("src-b", 1, "b1", op="delete", span_id="owned-by-b"),
            make_event("src-a", 2, "a2", span=make_span("owned-by-b")),
        ])
        with self.assertRaises(ValueError):
            compile_trace_history(delete_first)

    def test_unordered_releases_are_sorted_by_trace_and_version(self):
        releases = [
            make_release("a-v2", "trace-a", 2, zero_through(**{"src-a": 2})),
            make_release("b-v1", "trace-b", 1, zero_through()),
            make_release("a-v1", "trace-a", 1, zero_through(**{"src-a": 1})),
        ]
        events = [
            make_event("src-a", 1, "a1", span=make_span("a1", trace="trace-a")),
            make_event("src-a", 2, "a2", span=make_span("a2", trace="trace-a")),
            make_event("src-b", 1, "b1", trace="trace-b",
                       span=make_span("b-root", trace="trace-b")),
        ]
        data = base_data(events=events, releases=releases)

        result = compile_trace_history(data)
        self.assertEqual(
            [(item["trace"], item["version"]) for item in result["releases"]],
            [("trace-a", 1), ("trace-a", 2), ("trace-b", 1)],
        )

        reordered = copy.deepcopy(data)
        reordered["releases"] = list(reversed(reordered["releases"]))
        reordered["events"] = list(reversed(reordered["events"]))
        self.assertEqual(
            json.dumps(result, sort_keys=True),
            json.dumps(compile_trace_history(reordered), sort_keys=True),
        )

    def test_frozen_low_version_survives_later_parent_and_time_corrections(self):
        data = base_data(events=[
            make_event("src-a", 1, "e1", span=make_span("root", end="5")),
            make_event("src-a", 2, "e2",
                       span=make_span("child", parent="ghost", end="2")),
            make_event("src-a", 3, "e3",
                       span=make_span("child", parent="root", end="2")),
            make_event("src-a", 4, "e4", span=make_span("root", end="10")),
        ], releases=[
            make_release("v1", "trace-a", 1, zero_through(**{"src-a": 1})),
            make_release("v2", "trace-a", 2, zero_through(**{"src-a": 2})),
            make_release("v3", "trace-a", 3, zero_through(**{"src-a": 3})),
            make_release("v4", "trace-a", 4, zero_through(**{"src-a": 4})),
        ])

        result = compile_trace_history(data)
        v1, v2, v3, v4 = result["releases"]

        self.assertEqual(v1["status"], "ready")
        self.assertEqual(v1["spans"][0]["end"], "5")
        self.assertEqual(v1["analysis"]["traces"][0]["wall_time"], "5")

        self.assertEqual(v2["status"], "invalid")
        self.assertEqual(v2["error"], "invalid parent")
        self.assertNotIn("spans", v2)
        self.assertNotIn("analysis", v2)

        self.assertEqual(v3["status"], "ready")
        self.assertEqual(v3["analysis"]["traces"][0]["wall_time"], "5")
        self.assertEqual(v4["status"], "ready")
        v4_spans = {span["id"]: span for span in v4["spans"]}
        self.assertEqual(v4_spans["root"]["end"], "10")
        self.assertEqual(v4["analysis"]["traces"][0]["wall_time"], "10")

        head = result["heads"][0]
        self.assertEqual(head["status"], "ready")
        self.assertEqual(head["analysis"]["traces"][0]["wall_time"], "10")

        # The frozen v1 object was not changed by later materialization.
        v1_spans = {span["id"]: span for span in result["releases"][0]["spans"]}
        self.assertEqual(v1_spans["root"]["end"], "5")

    def test_invalid_trace_is_isolated_from_other_head_and_release_versions(self):
        data = base_data(events=[
            make_event("src-a", 1, "bad1", trace="bad",
                       span=make_span("a", trace="bad", parent=None)),
            make_event("src-a", 2, "bad2", trace="bad",
                       span=make_span("b", trace="bad", parent="a")),
            make_event("src-a", 3, "bad3", trace="bad",
                       span=make_span("a", trace="bad", parent="b")),
            make_event("src-b", 1, "good1", trace="good",
                       span=make_span("g-root", trace="good")),
        ], releases=[
            make_release("bad-v1", "bad", 1,
                         {"src-a": 2, "src-b": 0}),
            make_release("bad-v2", "bad", 2,
                         {"src-a": 3, "src-b": 0}),
            make_release("good-v1", "good", 1,
                         {"src-a": 0, "src-b": 1}),
        ])

        result = compile_trace_history(data)
        heads = {item["trace"]: item for item in result["heads"]}
        self.assertEqual(heads["bad"]["status"], "invalid")
        self.assertEqual(heads["bad"]["error"], "parent cycle")
        self.assertEqual(heads["good"]["status"], "ready")

        releases = {(item["trace"], item["version"]): item
                    for item in result["releases"]}
        self.assertEqual(releases[("bad", 1)]["status"], "ready")
        self.assertEqual(releases[("bad", 2)]["status"], "invalid")
        self.assertEqual(releases[("bad", 2)]["error"], "parent cycle")
        self.assertEqual(releases[("good", 1)]["status"], "ready")

    def test_exact_decimals_zero_duration_and_crossing_child_keep_process_semantics(self):
        spans = [
            make_span("root", start="0", end="10"),
            make_span("db", parent="root", service="db", start="2", end="8"),
            make_span("cache", parent="root", service="cache", start="3", end="5"),
            make_span("zero", parent="root", service="cache", start="4", end="4"),
        ]
        data = base_data(events=[
            make_event("src-a", index + 1, f"e{index}", span=span)
            for index, span in enumerate(spans)
        ])

        result = compile_trace_history(data)
        head = result["heads"][0]
        self.assertEqual(head["status"], "ready")
        self.assertEqual([span["id"] for span in head["spans"]],
                         ["cache", "db", "root", "zero"])
        self.assertEqual(head["analysis"], process({"spans": head["spans"]}))
        self.assertEqual(head["analysis"]["traces"][0]["self_time"]["root"], "4")

        crossing_data = base_data(events=[
            make_event("src-a", 1, "parent", span=make_span(
                "parent", start="0", end="1")),
            make_event("src-a", 2, "crosses", span=make_span(
                "crosses", parent="parent", start="-1.500000", end="11.250000")),
        ])
        crossing_head = compile_trace_history(crossing_data)["heads"][0]
        self.assertEqual(crossing_head["status"], "ready")
        self.assertEqual(
            crossing_head["analysis"]["traces"][0]["self_time"]["parent"], "0"
        )
        self.assertEqual(
            crossing_head["analysis"]["traces"][0]["self_time"]["crosses"],
            "51/4",
        )

        decimal_data = base_data(events=[
            make_event("src-a", 1, "decimal", span=make_span(
                "exact", start="-0.100001", end="0.200000"
            )),
        ])
        decimal_head = compile_trace_history(decimal_data)["heads"][0]
        self.assertEqual(decimal_head["spans"][0]["start"], "-0.100001")
        self.assertEqual(decimal_head["spans"][0]["end"], "0.200000")
        self.assertEqual(
            decimal_head["analysis"]["traces"][0]["wall_time"],
            "300001/1000000",
        )

    def test_empty_mentioned_trace_and_span_limit_are_invalid_without_partial_analysis(self):
        empty_data = base_data(events=[
            make_event("src-a", 2, "future-only", trace="empty",
                       span=make_span("future", trace="empty")),
        ])
        empty_head = compile_trace_history(empty_data)["heads"][0]
        self.assertEqual(empty_head["status"], "invalid")
        self.assertIn("error", empty_head)
        self.assertNotIn("spans", empty_head)
        self.assertNotIn("analysis", empty_head)

        spans = [
            make_span(f"span-{index}", parent=None if index == 0 else f"span-{index - 1}")
            for index in range(2001)
        ]
        limit_data = base_data(events=[
            make_event("src-a", index + 1, f"e{index}", span=span)
            for index, span in enumerate(spans)
        ])
        result = compile_trace_history(limit_data)
        self.assertEqual(result["head_through"]["src-a"], 2001)
        self.assertEqual(result["heads"][0]["status"], "invalid")
        self.assertIn("2000", result["heads"][0]["error"])

    def test_validation_boundaries_and_release_structure(self):
        valid = base_data(events=[make_event("src-a", 1)])

        invalid_cases = [
            {"unknown_top": True, **valid},
            {**valid, "sources": []},
            {**valid, "sources": ["src-a", "src-a"]},
            {**valid, "events": []},
        ]
        for bad in invalid_cases:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    compile_trace_history(bad)

        bool_seq = copy.deepcopy(valid)
        bool_seq["events"][0]["seq"] = True
        with self.assertRaises(ValueError):
            compile_trace_history(bool_seq)

        bad_seq = copy.deepcopy(valid)
        bad_seq["events"][0]["seq"] = 1_000_000_001
        with self.assertRaises(ValueError):
            compile_trace_history(bad_seq)

        unknown_event_field = copy.deepcopy(valid)
        unknown_event_field["events"][0]["extra"] = True
        with self.assertRaises(ValueError):
            compile_trace_history(unknown_event_field)

        bad_time = base_data(events=[
            make_event("src-a", 1, span=make_span("bad", start="2", end="1"))
        ])
        with self.assertRaises(ValueError):
            compile_trace_history(bad_time)

        duplicate_release_id = copy.deepcopy(valid)
        duplicate_release_id["releases"] = [
            make_release("same", "trace-a", 1, zero_through(**{"src-a": 1})),
            make_release("same", "trace-b", 1, zero_through()),
        ]
        with self.assertRaises(ValueError):
            compile_trace_history(duplicate_release_id)

        gap_version = copy.deepcopy(valid)
        gap_version["releases"] = [
            make_release("r1", "trace-a", 1, zero_through(**{"src-a": 1})),
            make_release("r3", "trace-a", 3, zero_through(**{"src-a": 1})),
        ]
        with self.assertRaises(ValueError):
            compile_trace_history(gap_version)

        equal_vector = copy.deepcopy(valid)
        equal_vector["releases"] = [
            make_release("r1", "trace-a", 1, zero_through(**{"src-a": 1})),
            make_release("r2", "trace-a", 2, zero_through(**{"src-a": 1})),
        ]
        with self.assertRaises(ValueError):
            compile_trace_history(equal_vector)

    def test_result_is_json_serializable_deterministic_and_independent_of_input(self):
        data = base_data(
            events=[
                make_event("src-b", 2, "b2", span=make_span("two")),
                make_event("src-a", 1, "a1", span=make_span("one")),
                make_event("src-a", 1, "a1", span=make_span("one")),
            ],
            releases=[
                make_release("r1", "trace-a", 1, zero_through(**{"src-a": 1, "src-b": 2})),
            ],
        )
        original = copy.deepcopy(data)
        result = compile_trace_history(data)

        encoded = json.dumps(result, allow_nan=False, sort_keys=True)
        self.assertIn("trace-history-v1", encoded)
        self.assertEqual(data, original)

        data["events"][0]["span"]["end"] = "999"
        self.assertEqual(result["heads"][0]["spans"][0]["end"], "10")

        reordered = copy.deepcopy(original)
        reordered["sources"] = list(reversed(reordered["sources"]))
        reordered["events"] = list(reversed(reordered["events"]))
        self.assertEqual(
            json.dumps(compile_trace_history(reordered), sort_keys=True),
            json.dumps(result, sort_keys=True),
        )


if __name__ == "__main__":
    unittest.main()
