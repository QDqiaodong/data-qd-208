import copy
from fractions import Fraction
import json
import re
import unittest
from unittest import mock

from trace_causality import process, project_for_sharing
from trace_causality.sharing import _pseudonym


KEY = b"collaboration-key"
PSEUDONYM_RE = re.compile(r"^x[0-9a-f]{63}$")
DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]{0,8})(?:[.][0-9]{1,6})?$")


def span(ident, trace="trace-a", parent=None, service="api", start="0", end="1"):
    return {
        "id": ident,
        "trace": trace,
        "parent": parent,
        "service": service,
        "start": start,
        "end": end,
    }


def sharing_dataset():
    return {
        "spans": [
            # Negative origin, overlap, and a child extending past its parent.
            span("root-a", start="-2", end="8"),
            span("extend", parent="root-a", service="db", start="0", end="9"),
            span("overlap", parent="root-a", service="cache", start="6", end="11"),
            span("zero-a", parent="root-a", service="cache", start="1", end="1"),
            # Six-place decimals and an independent negative trace origin.
            span(
                "root-b",
                trace="trace-b",
                service="worker",
                start="-0.100001",
                end="0.200000",
            ),
            span(
                "zero-b",
                trace="trace-b",
                parent="root-b",
                service="calc",
                start="0.100000",
                end="0.100000",
            ),
        ]
    }


class ProjectionTest(unittest.TestCase):
    def setUp(self):
        self.data = sharing_dataset()
        self.original = copy.deepcopy(self.data)
        self.projected = project_for_sharing(self.data, KEY)

    def test_contract_identity_format_and_input_immutability(self):
        self.assertEqual(self.data, self.original)
        self.assertEqual(set(self.projected), {"spans"})
        self.assertEqual(len(self.projected["spans"]), len(self.data["spans"]))

        for original, projected in zip(self.data["spans"], self.projected["spans"]):
            self.assertEqual(
                set(projected),
                {"id", "trace", "parent", "service", "start", "end"},
            )
            for field in ("id", "trace", "service"):
                self.assertRegex(projected[field], PSEUDONYM_RE)
            if projected["parent"] is not None:
                self.assertRegex(projected["parent"], PSEUDONYM_RE)
            self.assertRegex(projected["start"], DECIMAL_RE)
            self.assertRegex(projected["end"], DECIMAL_RE)
            self.assertIsInstance(projected["start"], str)
            self.assertIsInstance(projected["end"], str)

        # The result is plain JSON data, with no Fraction or float values.
        json.dumps(self.projected, allow_nan=False)

    def test_no_original_identity_values_remain(self):
        raw_values = set()
        for item in self.data["spans"]:
            raw_values.update((item["id"], item["trace"], item["service"]))
            if item["parent"] is not None:
                raw_values.add(item["parent"])

        projected_values = set()
        for item in self.projected["spans"]:
            projected_values.update(
                (item["id"], item["trace"], item["service"], item["parent"])
            )
        projected_values.discard(None)

        self.assertTrue(raw_values)
        self.assertTrue(projected_values)
        self.assertTrue(raw_values.isdisjoint(projected_values))

    def test_parent_references_and_graph_remain_intact(self):
        by_id = {item["id"]: item for item in self.projected["spans"]}
        for item in self.projected["spans"]:
            if item["parent"] is not None:
                parent = by_id[item["parent"]]
                self.assertEqual(item["trace"], parent["trace"])

        # The unchanged processor must accept the complete projection.
        result = process(self.projected)
        self.assertEqual(result["summary"], {"spans": 6, "traces": 2, "roots": 2})

    def test_relative_times_are_exact_and_origin_is_per_trace(self):
        offsets = {}
        for item in self.data["spans"]:
            trace = item["trace"]
            start = Fraction(item["start"])
            offsets[trace] = min(start, offsets.get(trace, start))

        for original, projected in zip(self.data["spans"], self.projected["spans"]):
            offset = offsets[original["trace"]]
            self.assertEqual(
                Fraction(projected["start"]), Fraction(original["start"]) - offset
            )
            self.assertEqual(
                Fraction(projected["end"]), Fraction(original["end"]) - offset
            )
            self.assertEqual(
                Fraction(projected["end"]) - Fraction(projected["start"]),
                Fraction(original["end"]) - Fraction(original["start"]),
            )

        by_raw_id = {
            original["id"]: projected
            for original, projected in zip(self.data["spans"], self.projected["spans"])
        }
        self.assertEqual(by_raw_id["root-a"]["start"], "0")
        self.assertEqual(by_raw_id["root-a"]["end"], "10")
        self.assertEqual(by_raw_id["root-b"]["start"], "0")
        self.assertEqual(by_raw_id["root-b"]["end"], "0.300001")
        self.assertEqual(by_raw_id["zero-b"]["start"], "0.200001")
        self.assertEqual(by_raw_id["zero-b"]["end"], "0.200001")
        self.assertEqual(by_raw_id["zero-a"]["start"], "3")
        self.assertEqual(by_raw_id["zero-a"]["end"], "3")

    def test_processing_projection_preserves_causal_results(self):
        original_result = process(self.data)
        projected_result = process(self.projected)

        id_map = {
            raw["id"]: hidden["id"]
            for raw, hidden in zip(self.data["spans"], self.projected["spans"])
        }
        trace_map = {
            raw["trace"]: hidden["trace"]
            for raw, hidden in zip(self.data["spans"], self.projected["spans"])
        }
        service_map = {
            raw["service"]: hidden["service"]
            for raw, hidden in zip(self.data["spans"], self.projected["spans"])
        }

        for original_trace in original_result["traces"]:
            projected_trace = next(
                item
                for item in projected_result["traces"]
                if item["trace"] == trace_map[original_trace["trace"]]
            )
            self.assertEqual(
                projected_trace["wall_time"], original_trace["wall_time"]
            )
            self.assertEqual(
                projected_trace["self_time"],
                {
                    id_map[span_id]: value
                    for span_id, value in original_trace["self_time"].items()
                },
            )
            self.assertEqual(
                sorted(projected_trace["roots"]),
                sorted(id_map[root] for root in original_trace["roots"]),
            )
            self.assertEqual(
                projected_trace["services"],
                sorted(service_map[name] for name in original_trace["services"]),
            )
            expected_children = {
                id_map[parent]: sorted(id_map[child] for child in children)
                for parent, children in original_trace["children"].items()
            }
            self.assertEqual(projected_trace["children"], expected_children)

        # Uniform clipping and union overlap preserve this parent's self time.
        hidden_root = id_map["root-a"]
        trace_a = next(
            item for item in projected_result["traces"]
            if item["trace"] == trace_map["trace-a"]
        )
        self.assertEqual(trace_a["self_time"][hidden_root], "2")

    def test_input_order_does_not_change_identity_mapping(self):
        reordered = copy.deepcopy(self.data)
        reordered["spans"] = list(reversed(reordered["spans"]))

        first = project_for_sharing(self.data, KEY)
        second = project_for_sharing(reordered, KEY)

        self.assertEqual(
            {item["id"]: item for item in first["spans"]},
            {item["id"]: item for item in second["spans"]},
        )
        self.assertEqual(process(first)["summary"], process(second)["summary"])

    def test_stable_across_calls_and_sensitive_to_key(self):
        again = project_for_sharing(copy.deepcopy(self.data), KEY)
        self.assertEqual(self.projected, again)

        other_key = project_for_sharing(self.data, b"other-key")
        self.assertNotEqual(
            [item["id"] for item in self.projected["spans"]],
            [item["id"] for item in other_key["spans"]],
        )

    def test_field_category_namespaces_are_isolated(self):
        data = {
            "spans": [
                span("same", trace="same", service="same"),
                span("child", trace="same", parent="same"),
            ]
        }
        projected = project_for_sharing(data, KEY)
        same = projected["spans"][0]

        self.assertNotEqual(same["id"], same["trace"])
        self.assertNotEqual(same["id"], same["service"])
        self.assertNotEqual(same["trace"], same["service"])
        # Parent is an id foreign key and must match the child's id token.
        self.assertEqual(projected["spans"][1]["parent"], same["id"])

        expected_id = _pseudonym(
            KEY, b"trace-causality.sharing/v1/span-id", "same"
        )
        self.assertEqual(same["id"], expected_id)

    def test_truncated_pseudonym_collision_is_rejected(self):
        data = {
            "spans": [
                span("first"),
                span("second", parent="first"),
            ]
        }
        before = copy.deepcopy(data)
        colliding_token = "x" + "0" * 63
        real_pseudonym = _pseudonym
        span_namespace = b"trace-causality.sharing/v1/span-id"

        def collision_after_truncation(key, namespace, value):
            if namespace == span_namespace and value in {"first", "second"}:
                return colliding_token
            return real_pseudonym(key, namespace, value)

        with mock.patch(
            "trace_causality.sharing._pseudonym",
            side_effect=collision_after_truncation,
        ):
            with self.assertRaises(ValueError):
                project_for_sharing(data, KEY)

        self.assertEqual(data, before)

    def test_invalid_keys_are_rejected_without_mutation(self):
        valid = sharing_dataset()
        for bad_key in (b"", "", "non-bytes", None, 123, bytearray(b"key")):
            with self.subTest(bad_key=bad_key):
                before = copy.deepcopy(valid)
                with self.assertRaises(ValueError):
                    project_for_sharing(valid, bad_key)
                self.assertEqual(valid, before)

    def test_invalid_inputs_are_rejected_without_mutation(self):
        valid_span = span("x")

        def cycle_data():
            return {
                "spans": [
                    span("a", parent="b"),
                    span("b", parent="a"),
                ]
            }

        bad_cases = {
            "not_object": [],
            "missing_spans": {},
            "extra_top_level_field": {"spans": [valid_span], "extra": True},
            "empty_spans": {"spans": []},
            "too_many_spans": {
                "spans": [span(f"s{i}") for i in range(2001)]
            },
            "missing_field": {"spans": [{k: v for k, v in valid_span.items() if k != "id"}]},
            "extra_span_field": {"spans": [{**valid_span, "extra": True}]},
            "invalid_identity": {"spans": [span("bad id")]},
            "duplicate_identity": {
                "spans": [span("dup"), span("dup", start="2", end="3")]
            },
            "invalid_trace": {"spans": [span("x", trace="bad trace")]},
            "invalid_service": {"spans": [span("x", service=123)]},
            "invalid_parent_type": {"spans": [span("x", parent=123)]},
            "missing_parent": {"spans": [span("x", parent="ghost")]},
            "cross_trace_parent": {
                "spans": [
                    span("parent", trace="t1"),
                    span("child", trace="t2", parent="parent"),
                ]
            },
            "cycle": cycle_data(),
            "float_timestamp": {"spans": [{**valid_span, "start": 0.1}]},
            "too_many_decimals": {"spans": [span("x", start="0.1234567")]},
            "end_before_start": {"spans": [span("x", start="2", end="1")]},
        }

        for name, bad_data in bad_cases.items():
            with self.subTest(case=name):
                before = copy.deepcopy(bad_data)
                with self.assertRaises(ValueError):
                    project_for_sharing(bad_data, KEY)
                self.assertEqual(bad_data, before)


if __name__ == "__main__":
    unittest.main()
