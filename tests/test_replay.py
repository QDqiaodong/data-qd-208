import copy
import json
import unittest

from trace_causality import plan_trace_replay


def span(
    span_id,
    trace="trace-a",
    parent=None,
    clock="c1",
    start="0",
    end="10",
    service="api",
):
    return {
        "id": span_id,
        "trace": trace,
        "parent": parent,
        "service": service,
        "start": start,
        "end": end,
        "clock": clock,
    }


def segment(segment_id, clock="c1", start="0", end="10", byte_count=10):
    return {
        "id": segment_id,
        "clock": clock,
        "from": start,
        "to": end,
        "bytes": byte_count,
    }


def request(request_id="req-a", trace="trace-a", start="0", end="10"):
    return {"id": request_id, "trace": trace, "from": start, "to": end}


def base_data(**overrides):
    data = {
        "reference": "ref",
        "clocks": ["ref", "c1"],
        "constraints": [
            {"left": "ref", "right": "c1", "min": "0", "max": "0"},
        ],
        "spans": [span("span-a")],
        "segments": [segment("seg-a")],
        "requests": [request()],
    }
    data.update(overrides)
    return data


class ReplayPlanningTest(unittest.TestCase):
    def test_indirect_constraints_tighten_bounds_and_boundary_match(self):
        tightened = base_data(
            clocks=["ref", "c1", "c2"],
            constraints=[
                {"left": "ref", "right": "c1", "min": "0", "max": "10"},
                {"left": "ref", "right": "c2", "min": "2", "max": "4"},
                {"left": "c2", "right": "c1", "min": "1", "max": "3"},
            ],
            spans=[span("point", start="0", end="0")],
            segments=[segment("point-seg", start="0", end="0", byte_count=1)],
            requests=[
                request("at-bound", start="7", end="7"),
                request("past-bound", start="8", end="8"),
            ],
        )

        result = plan_trace_replay(tightened)

        clocks = {item["clock"]: item for item in result["clocks"]}
        self.assertEqual(clocks["c1"], {
            "clock": "c1", "min_offset": "3", "max_offset": "7",
        })
        self.assertEqual(clocks["c2"], {
            "clock": "c2", "min_offset": "2", "max_offset": "4",
        })
        self.assertEqual(clocks["ref"], {
            "clock": "ref", "min_offset": "0", "max_offset": "0",
        })

        by_id = {item["id"]: item for item in result["requests"]}
        self.assertEqual(by_id["at-bound"]["matched_spans"], ["point"])
        self.assertEqual(by_id["at-bound"]["status"], "ready")
        self.assertEqual(by_id["past-bound"]["matched_spans"], [])
        self.assertEqual(by_id["past-bound"]["protected_spans"], [])
        self.assertEqual(by_id["past-bound"]["segments"], [])
        self.assertEqual(by_id["past-bound"]["total_bytes"], 0)

        loose = base_data(
            clocks=["ref", "c1"],
            constraints=[
                {"left": "ref", "right": "c1", "min": "0", "max": "10"},
            ],
            spans=[span("point", start="0", end="0")],
            segments=[segment("point-seg", start="0", end="0", byte_count=1)],
            requests=[request("past-bound", start="8", end="8")],
        )
        loose_result = plan_trace_replay(loose)
        self.assertEqual(
            loose_result["requests"][0]["matched_spans"], ["point"]
        )

    def test_contradictory_cycle_and_unanchored_clock_are_rejected(self):
        contradictory = base_data(constraints=[
            {"left": "ref", "right": "c1", "min": "2", "max": "5"},
            {"left": "c1", "right": "ref", "min": "-10", "max": "-8"},
        ])
        with self.assertRaises(ValueError):
            plan_trace_replay(contradictory)

        unanchored = base_data(
            clocks=["ref", "c1", "c2"],
            constraints=[
                {"left": "c1", "right": "c2", "min": "0", "max": "1"},
            ],
        )
        with self.assertRaises(ValueError):
            plan_trace_replay(unanchored)

    def test_cross_clock_ancestor_closure_excludes_sibling(self):
        data = base_data(
            clocks=["ref", "parent-clock", "child-clock"],
            constraints=[
                {"left": "ref", "right": "parent-clock", "min": "0", "max": "0"},
                {"left": "ref", "right": "child-clock", "min": "0", "max": "0"},
            ],
            spans=[
                span(
                    "root",
                    clock="parent-clock",
                    start="-100",
                    end="-90",
                ),
                span(
                    "child",
                    parent="root",
                    clock="child-clock",
                    start="5",
                    end="6",
                ),
                span(
                    "sibling",
                    parent="root",
                    clock="child-clock",
                    start="20",
                    end="21",
                ),
            ],
            segments=[
                segment("root-seg", clock="parent-clock",
                        start="-100", end="-90", byte_count=1),
                segment("child-seg", clock="child-clock",
                        start="5", end="6", byte_count=1),
            ],
            requests=[request(start="0", end="10")],
        )
        result = plan_trace_replay(data)
        req = result["requests"][0]

        self.assertEqual(req["matched_spans"], ["child"])
        self.assertEqual(req["protected_spans"], ["child", "root"])
        self.assertNotIn("sibling", req["protected_spans"])
        self.assertEqual(req["segments"], ["child-seg", "root-seg"])
        self.assertEqual(req["total_bytes"], 2)

    def test_global_cost_prefers_shared_segment_to_separate_cheap_pieces(self):
        data = base_data(
            spans=[
                span("span-a", start="0", end="1"),
                span("span-b", start="9", end="10"),
            ],
            segments=[
                segment("piece-a", start="0", end="1", byte_count=2),
                segment("piece-b", start="9", end="10", byte_count=2),
                segment("shared", start="0", end="10", byte_count=3),
            ],
        )
        result = plan_trace_replay(data)
        req = result["requests"][0]
        self.assertEqual(req["protected_spans"], ["span-a", "span-b"])
        self.assertEqual(req["segments"], ["shared"])
        self.assertEqual(req["total_bytes"], 3)

    def test_equal_cost_and_count_uses_stable_id_lex_order(self):
        data = base_data(
            segments=[
                segment("seg-b", byte_count=5),
                segment("seg-a", byte_count=5),
            ],
        )
        result = plan_trace_replay(data)
        self.assertEqual(result["requests"][0]["segments"], ["seg-a"])

        reordered = copy.deepcopy(data)
        reordered["segments"].reverse()
        self.assertEqual(
            plan_trace_replay(reordered)["requests"][0]["segments"],
            ["seg-a"],
        )

    def test_touching_segments_zero_span_and_zero_segment_cover(self):
        data = base_data(
            spans=[
                span("long", start="0", end="10"),
                span("point", start="12", end="12"),
            ],
            segments=[
                segment("part-a", start="0", end="5", byte_count=1),
                segment("part-b", start="5", end="10", byte_count=1),
                segment("whole-expensive", start="0", end="10", byte_count=3),
                segment("point-seg", start="12", end="12", byte_count=1),
            ],
            requests=[
                request("req-long", start="0", end="10"),
                request("req-point", start="12", end="12"),
            ],
        )
        by_id = {item["id"]: item for item in plan_trace_replay(data)["requests"]}

        self.assertEqual(by_id["req-long"]["status"], "ready")
        self.assertEqual(by_id["req-long"]["segments"], ["part-a", "part-b"])
        self.assertEqual(by_id["req-long"]["total_bytes"], 2)

        self.assertEqual(by_id["req-point"]["status"], "ready")
        self.assertEqual(by_id["req-point"]["segments"], ["point-seg"])
        self.assertEqual(by_id["req-point"]["total_bytes"], 1)

    def test_missing_ranges_merge_while_another_request_remains_ready(self):
        data = base_data(
            clocks=["ref", "bad-clock", "good-clock"],
            constraints=[
                {"left": "ref", "right": "bad-clock", "min": "0", "max": "0"},
                {"left": "ref", "right": "good-clock", "min": "0", "max": "0"},
            ],
            spans=[
                span("bad-a", trace="trace-bad", clock="bad-clock",
                     start="0", end="6"),
                span("bad-b", trace="trace-bad", clock="bad-clock",
                     start="4", end="10"),
                span("good", trace="trace-good", clock="good-clock"),
            ],
            segments=[
                segment("bad-left", clock="bad-clock",
                        start="0", end="3", byte_count=1),
                segment("bad-right", clock="bad-clock",
                        start="7", end="10", byte_count=1),
                segment("good-seg", clock="good-clock", byte_count=1),
            ],
            requests=[
                request("req-bad", trace="trace-bad"),
                request("req-good", trace="trace-good"),
            ],
        )

        by_id = {item["id"]: item for item in plan_trace_replay(data)["requests"]}
        bad = by_id["req-bad"]
        self.assertEqual(bad["status"], "incomplete")
        self.assertEqual(bad["missing"], [
            {"clock": "bad-clock", "from": "3", "to": "7"},
        ])
        self.assertNotIn("segments", bad)
        self.assertNotIn("total_bytes", bad)

        good = by_id["req-good"]
        self.assertEqual(good["status"], "ready")
        self.assertEqual(good["segments"], ["good-seg"])
        self.assertEqual(good["total_bytes"], 1)
        self.assertNotIn("missing", good)

    def test_negative_six_decimals_reorder_and_no_mutation(self):
        data = base_data(
            constraints=[{
                "left": "ref",
                "right": "c1",
                "min": "-0.123456",
                "max": "-0.123456",
            }],
            spans=[span(
                "decimal-span",
                start="-2.000001",
                end="-1.500000",
            )],
            segments=[segment(
                "decimal-seg",
                start="-2.000001",
                end="-1.500000",
                byte_count=1,
            )],
            requests=[request(
                "decimal-req",
                start="-2.123457",
                end="-1.623456",
            )],
        )

        before = copy.deepcopy(data)
        first = plan_trace_replay(data)
        json.dumps(first, allow_nan=False)
        self.assertEqual(data, before)

        clock = first["clocks"][0]
        self.assertEqual(clock["min_offset"], "-0.123456")
        self.assertEqual(clock["max_offset"], "-0.123456")
        req = first["requests"][0]
        self.assertEqual(req["from"], "-2.123457")
        self.assertEqual(req["to"], "-1.623456")
        self.assertEqual(req["matched_spans"], ["decimal-span"])
        self.assertEqual(req["segments"], ["decimal-seg"])

        reordered = copy.deepcopy(data)
        reordered["clocks"].reverse()
        reordered["constraints"].reverse()
        reordered["spans"].reverse()
        reordered["segments"].reverse()
        reordered["requests"].reverse()
        self.assertEqual(plan_trace_replay(reordered), first)
        self.assertEqual(data, before)

    def test_invalid_documents_are_rejected_without_partial_planning(self):
        def expect_value_error(mutate):
            data = base_data()
            mutate(data)
            with self.assertRaises(ValueError):
                plan_trace_replay(data)

        expect_value_error(lambda d: d.update(extra=True))
        expect_value_error(lambda d: d["spans"][0].update(extra=True))
        expect_value_error(lambda d: d["segments"][0].update(bytes=True))
        expect_value_error(lambda d: d["segments"][0].update(bytes="1"))
        expect_value_error(lambda d: d["segments"][0].update(bytes=1.5))
        expect_value_error(lambda d: d["clocks"].append("c1"))
        expect_value_error(lambda d: d["spans"].append(copy.deepcopy(d["spans"][0])))
        expect_value_error(lambda d: d["requests"].append(copy.deepcopy(d["requests"][0])))
        expect_value_error(lambda d: d["spans"][0].update(end="-1"))
        expect_value_error(lambda d: d["requests"][0].update(trace="missing"))
        expect_value_error(lambda d: d["constraints"][0].update(left="missing"))
        expect_value_error(lambda d: d["constraints"].append({
            "left": "c1", "right": "c1", "min": "0", "max": "0",
        }))


if __name__ == "__main__":
    unittest.main()
