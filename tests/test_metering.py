import copy
import json
import unittest

from trace_causality import settle_trace_metering


def span(span_id, start="0", end="10", service="svc", trace="trace-a", parent=None):
    return {
        "id": span_id,
        "trace": trace,
        "parent": parent,
        "service": service,
        "start": start,
        "end": end,
    }


def meter(meter_id="meter-a", service="svc", unit="request", readings=None, modulus=None):
    if readings is None:
        readings = [
            {"at": "0", "value": 0},
            {"at": "10", "value": 10},
        ]
    return {
        "id": meter_id,
        "service": service,
        "unit": unit,
        "modulus": modulus,
        "readings": readings,
    }


def budget(account="acct-a", service="svc", unit="request", limit=1000, committed=0):
    return {
        "account": account,
        "service": service,
        "unit": unit,
        "limit": limit,
        "committed": committed,
    }


def request(request_id="request-a", account="acct-a", items=None):
    if items is None:
        items = [{"service": "svc", "unit": "request", "units": 1}]
    return {"id": request_id, "account": account, "items": items}


def deepest_data():
    spans = [
        span("span-root", "0", "10", "svc"),
        span("span-same-child", "2", "8", "svc", parent="span-root"),
        span("span-same-grandchild", "4", "6", "svc", parent="span-same-child"),
        span("span-cross-db", "3", "7", "db", parent="span-same-child"),
    ]
    return {
        "spans": spans,
        "meters": [meter()],
        "ownership": [{"trace": "trace-a", "account": "acct-a"}],
        "budgets": [budget()],
        "requests": [],
    }


class MeterSettlementTest(unittest.TestCase):
    def test_schema_and_exact_object_shapes(self):
        result = settle_trace_metering(deepest_data())
        self.assertEqual(set(result), {
            "schema", "meters", "spans", "budgets", "requests"
        })
        self.assertEqual(result["schema"], "trace-meter-settlement-v1")

        meter_result = result["meters"][0]
        self.assertEqual(set(meter_result), {
            "id", "service", "unit", "intervals",
            "total_delta", "total_allocated", "total_unassigned",
        })
        interval = meter_result["intervals"][0]
        self.assertEqual(set(interval), {
            "from", "to", "delta", "allocations", "unassigned"
        })
        allocation = interval["allocations"][0]
        self.assertEqual(set(allocation), {"span", "trace", "account", "units"})
        self.assertEqual(set(result["spans"][0]), {
            "id", "trace", "service", "account", "usage"
        })
        self.assertEqual(set(result["budgets"][0]), {
            "account", "service", "unit", "limit", "committed",
            "settled", "used", "remaining", "status",
        })
        json.dumps(result, allow_nan=False)

    def test_deepest_same_service_owner_and_cross_service_descendant(self):
        result = settle_trace_metering(deepest_data())
        interval = result["meters"][0]["intervals"][0]
        units = {item["span"]: item["units"] for item in interval["allocations"]}

        # root owns [0,2) and [8,10): 4
        # same-service child owns [2,4) and [6,8): 4
        # grandchild owns [4,6): 2
        # the db child never captures svc meter ownership.
        self.assertEqual(units, {
            "span-same-child": 4,
            "span-same-grandchild": 2,
            "span-root": 4,
        })
        self.assertEqual(interval["delta"], 10)
        self.assertEqual(interval["unassigned"], 0)
        self.assertEqual(
            sum(item["units"] for item in interval["allocations"]), 10
        )

        db_span = next(s for s in result["spans"] if s["id"] == "span-cross-db")
        self.assertEqual(db_span["usage"], [])

    def test_overlapping_same_service_siblings_share_and_tie_by_id(self):
        data = {
            "spans": [
                span("span-a", "0", "5"),
                span("span-b", "3", "8"),
            ],
            "ownership": [{"trace": "trace-a", "account": "acct-a"}],
            "meters": [meter(readings=[
                {"at": "0", "value": 0},
                {"at": "8", "value": 3},
            ])],
            "budgets": [budget(limit=10)],
            "requests": [],
        }
        result = settle_trace_metering(data)
        allocations = result["meters"][0]["intervals"][0]["allocations"]
        self.assertEqual(
            [(item["span"], item["units"]) for item in allocations],
            [("span-a", 2), ("span-b", 1)],
        )

    def test_distinct_traces_own_concurrently_and_aggregate_separately(self):
        data = {
            "spans": [
                span("span-a", "0", "10", trace="trace-a"),
                span("span-b", "2", "8", trace="trace-b"),
            ],
            "ownership": [
                {"trace": "trace-a", "account": "acct-a"},
                {"trace": "trace-b", "account": "acct-b"},
            ],
            "meters": [meter(readings=[
                {"at": "0", "value": 0},
                {"at": "10", "value": 9},
            ])],
            "budgets": [
                budget(account="acct-a", limit=100),
                budget(account="acct-b", limit=100),
            ],
            "requests": [],
        }
        result = settle_trace_metering(data)
        allocations = result["meters"][0]["intervals"][0]["allocations"]
        self.assertEqual(
            [(a["span"], a["account"], a["units"]) for a in allocations],
            [("span-a", "acct-a", 6), ("span-b", "acct-b", 3)],
        )
        self.assertEqual(
            {b["account"]: b["settled"] for b in result["budgets"]},
            {"acct-a": 6, "acct-b": 3},
        )

    def test_long_parent_meter_boundaries_and_out_of_bounds_child_conserve(self):
        data = {
            "spans": [
                span("span-parent", "0", "100", parent=None),
                span("span-child", "40", "120", parent="span-parent"),
            ],
            "ownership": [{"trace": "trace-a", "account": "acct-a"}],
            "meters": [meter(readings=[
                {"at": "0", "value": 0},
                {"at": "50", "value": 7},
                {"at": "100", "value": 20},
                {"at": "150", "value": 23},
            ])],
            "budgets": [budget(limit=1000)],
            "requests": [],
        }
        result = settle_trace_metering(data)
        intervals = result["meters"][0]["intervals"]
        by_time = {(item["from"], item["to"]): item for item in intervals}

        first = by_time[("0", "50")]
        self.assertEqual(
            {a["span"]: a["units"] for a in first["allocations"]},
            {"span-parent": 6, "span-child": 1},
        )
        second = by_time[("50", "100")]
        self.assertEqual(
            {a["span"]: a["units"] for a in second["allocations"]},
            {"span-child": 13},
        )
        third = by_time[("100", "150")]
        self.assertEqual(
            {a["span"]: a["units"] for a in third["allocations"]},
            {"span-child": 3},
        )

        for interval in intervals:
            allocated = sum(a["units"] for a in interval["allocations"])
            self.assertEqual(
                allocated + interval["unassigned"], interval["delta"]
            )

        usage = {
            item["id"]: item["usage"] for item in result["spans"]
        }
        self.assertEqual(usage["span-parent"], [{"unit": "request", "units": 6}])
        self.assertEqual(usage["span-child"], [{"unit": "request", "units": 17}])

    def test_inactive_zero_delta_and_zero_duration_produce_unassigned_or_empty_usage(self):
        data = {
            "spans": [span("span-zero", "5", "5")],
            "ownership": [{"trace": "trace-a", "account": "acct-a"}],
            "meters": [
                meter("meter-inactive", readings=[
                    {"at": "100", "value": 0},
                    {"at": "200", "value": 5},
                ]),
                meter("meter-zero-delta", readings=[
                    {"at": "100", "value": 7},
                    {"at": "200", "value": 7},
                ]),
                meter("meter-zero-span", readings=[
                    {"at": "4", "value": 0},
                    {"at": "6", "value": 7},
                ]),
            ],
            "budgets": [budget(limit=1000)],
            "requests": [],
        }
        result = settle_trace_metering(data)
        meters = {item["id"]: item for item in result["meters"]}

        inactive = meters["meter-inactive"]["intervals"][0]
        self.assertEqual(inactive["allocations"], [])
        self.assertEqual(inactive["unassigned"], 5)

        zero_delta = meters["meter-zero-delta"]["intervals"][0]
        self.assertEqual(zero_delta["delta"], 0)
        self.assertEqual(zero_delta["allocations"], [])
        self.assertEqual(zero_delta["unassigned"], 0)

        zero_span = meters["meter-zero-span"]["intervals"][0]
        self.assertEqual(zero_span["allocations"], [])
        self.assertEqual(zero_span["unassigned"], 7)

        zero_output = next(s for s in result["spans"] if s["id"] == "span-zero")
        self.assertEqual(zero_output["usage"], [])
        self.assertEqual(result["budgets"][0]["settled"], 0)

    def test_modulus_rollover_and_decrease_without_modulus(self):
        data = deepest_data()
        data["meters"] = [meter(
            modulus=100,
            # Deliberately reverse input chronological order.
            readings=[
                {"at": "2", "value": 10},
                {"at": "1", "value": 90},
            ],
        )]
        result = settle_trace_metering(data)
        interval = result["meters"][0]["intervals"][0]
        self.assertEqual(interval["from"], "1")
        self.assertEqual(interval["to"], "2")
        self.assertEqual(interval["delta"], 20)

        invalid = deepest_data()
        invalid["meters"][0]["modulus"] = None
        invalid["meters"][0]["readings"] = [
            {"at": "0", "value": 9},
            {"at": "10", "value": 1},
        ]
        with self.assertRaises(ValueError):
            settle_trace_metering(invalid)

        bad_reading = deepest_data()
        bad_reading["meters"][0]["modulus"] = 10
        bad_reading["meters"][0]["readings"] = [
            {"at": "0", "value": 0},
            {"at": "10", "value": 10},
        ]
        with self.assertRaises(ValueError):
            settle_trace_metering(bad_reading)

    def test_two_physical_meters_same_service_and_unit_settle_separately(self):
        data = {
            "spans": [span("span-a")],
            "ownership": [{"trace": "trace-a", "account": "acct-a"}],
            "meters": [
                meter("meter-b", readings=[
                    {"at": "0", "value": 0},
                    {"at": "10", "value": 5},
                ]),
                meter("meter-a", readings=[
                    {"at": "0", "value": 0},
                    {"at": "10", "value": 3},
                ]),
            ],
            "budgets": [budget()],
            "requests": [],
        }
        result = settle_trace_metering(data)
        self.assertEqual([m["id"] for m in result["meters"]], ["meter-a", "meter-b"])
        self.assertEqual(
            [(m["id"], m["total_allocated"]) for m in result["meters"]],
            [("meter-a", 3), ("meter-b", 5)],
        )
        self.assertEqual(result["budgets"][0]["settled"], 8)

    def test_account_change_moves_aggregation_and_admission_not_span_units(self):
        def dataset():
            return {
                "spans": [
                    span("span-used", "0", "10", trace="trace-a"),
                    span("span-other-b", "0", "1", service="other", trace="trace-b"),
                    span("span-other-c", "0", "1", service="other", trace="trace-c"),
                ],
                "ownership": [
                    {"trace": "trace-a", "account": "acct-a"},
                    {"trace": "trace-b", "account": "acct-b"},
                    {"trace": "trace-c", "account": "acct-a"},
                ],
                "meters": [meter()],
                "budgets": [
                    budget(account="acct-a", limit=3),
                    budget(account="acct-b", limit=10),
                ],
                "requests": [
                    request("request-a", account="acct-a"),
                    request("request-b", account="acct-b"),
                ],
            }

        before = settle_trace_metering(dataset())
        before_allocation = before["meters"][0]["intervals"][0]["allocations"][0]
        self.assertEqual(before_allocation["account"], "acct-a")
        self.assertEqual(before_allocation["units"], 10)
        self.assertEqual(
            {b["account"]: (b["settled"], b["remaining"], b["status"])
             for b in before["budgets"]},
            {
                "acct-a": (10, -7, "exceeded"),
                "acct-b": (0, 10, "within"),
            },
        )
        self.assertEqual(
            {r["id"]: (r["status"], r["deficits"]) for r in before["requests"]},
            {
                "request-a": ("blocked", [{
                    "service": "svc", "unit": "request", "deficit": 8
                }]),
                "request-b": ("allowed", []),
            },
        )

        moved = dataset()
        moved["ownership"][0]["account"] = "acct-b"
        after = settle_trace_metering(moved)

        before_core = [
            {k: v for k, v in allocation.items() if k != "account"}
            for allocation in before["meters"][0]["intervals"][0]["allocations"]
        ]
        after_core = [
            {k: v for k, v in allocation.items() if k != "account"}
            for allocation in after["meters"][0]["intervals"][0]["allocations"]
        ]
        self.assertEqual(before_core, after_core)
        after_allocation = after["meters"][0]["intervals"][0]["allocations"][0]
        self.assertEqual(after_allocation["account"], "acct-b")

        self.assertEqual(
            {b["account"]: (b["settled"], b["remaining"], b["status"])
             for b in after["budgets"]},
            {
                "acct-a": (0, 3, "within"),
                "acct-b": (10, 0, "within"),
            },
        )
        self.assertEqual(
            {r["id"]: (r["status"], r["deficits"]) for r in after["requests"]},
            {
                "request-a": ("allowed", []),
                "request-b": ("blocked", [{
                    "service": "svc", "unit": "request", "deficit": 1
                }]),
            },
        )

    def test_exceeded_budget_reports_actual_and_every_deficit(self):
        data = {
            "spans": [span("span-a")],
            "ownership": [{"trace": "trace-a", "account": "acct-a"}],
            "meters": [
                meter("meter-token", unit="token", readings=[
                    {"at": "0", "value": 0},
                    {"at": "10", "value": 10},
                ]),
                meter("meter-byte", unit="byte", readings=[
                    {"at": "0", "value": 0},
                    {"at": "10", "value": 20},
                ]),
                meter("meter-disk", unit="disk", readings=[
                    {"at": "0", "value": 4},
                    {"at": "10", "value": 4},
                ]),
            ],
            "budgets": [
                budget(unit="token", limit=5, committed=0),
                budget(unit="byte", limit=10, committed=5),
                budget(unit="disk", limit=100, committed=0),
            ],
            "requests": [request("request-multi", items=[
                {"service": "svc", "unit": "token", "units": 3},
                {"service": "svc", "unit": "byte", "units": 7},
                {"service": "svc", "unit": "disk", "units": 5},
            ])],
        }
        result = settle_trace_metering(data)
        budget_by_unit = {b["unit"]: b for b in result["budgets"]}
        self.assertEqual(budget_by_unit["token"]["remaining"], -5)
        self.assertEqual(budget_by_unit["token"]["status"], "exceeded")
        self.assertEqual(budget_by_unit["byte"]["used"], 25)
        self.assertEqual(budget_by_unit["byte"]["remaining"], -15)

        request_result = result["requests"][0]
        self.assertEqual(request_result["status"], "blocked")
        self.assertEqual(request_result["deficits"], [
            {"service": "svc", "unit": "byte", "deficit": 22},
            {"service": "svc", "unit": "token", "deficit": 8},
        ])

    def test_requests_are_independent_and_reordering_does_not_change_settlement(self):
        data = {
            "spans": [span("span-a")],
            "ownership": [{"trace": "trace-a", "account": "acct-a"}],
            "meters": [meter(readings=[
                {"at": "0", "value": 0},
                {"at": "10", "value": 0},
            ])],
            "budgets": [budget(limit=5)],
            "requests": [
                request("request-big", items=[
                    {"service": "svc", "unit": "request", "units": 10}
                ]),
                request("request-fit", items=[
                    {"service": "svc", "unit": "request", "units": 5}
                ]),
            ],
        }
        first = settle_trace_metering(data)
        statuses = {r["id"]: r["status"] for r in first["requests"]}
        self.assertEqual(statuses, {"request-big": "blocked", "request-fit": "allowed"})

        reordered = copy.deepcopy(data)
        reordered["requests"].reverse()
        second = settle_trace_metering(reordered)
        self.assertEqual(second, first)

        changed = copy.deepcopy(data)
        changed["requests"][0]["items"][0]["units"] = 1
        third = settle_trace_metering(changed)
        self.assertEqual(third["meters"], first["meters"])
        self.assertEqual(third["spans"], first["spans"])
        self.assertEqual(third["budgets"], first["budgets"])
        self.assertTrue(all(r["status"] == "allowed" for r in third["requests"]))

    def test_negative_time_six_decimals_reordering_and_input_detachment(self):
        data = {
            "spans": [
                span("span-b", "0", "1", service="other"),
                span("span-a", "-1.500000", "2.250000"),
            ],
            "ownership": [{"trace": "trace-a", "account": "acct-a"}],
            "meters": [meter(readings=[
                {"at": "1.123456", "value": 3},
                {"at": "-0.250000", "value": 0},
            ])],
            "budgets": [budget()],
            "requests": [],
        }
        original = copy.deepcopy(data)
        result = settle_trace_metering(data)
        self.assertEqual(data, original)

        interval = result["meters"][0]["intervals"][0]
        self.assertEqual(interval["from"], "-0.250000")
        self.assertEqual(interval["to"], "1.123456")
        self.assertEqual(interval["delta"], 3)
        # span-a covers the whole meter interval; span-b does not.
        self.assertEqual(
            [a["span"] for a in interval["allocations"]], ["span-a"]
        )

        reordered = copy.deepcopy(data)
        reordered["spans"].reverse()
        for current_meter in reordered["meters"]:
            current_meter["readings"].reverse()
        self.assertEqual(settle_trace_metering(reordered), result)

        returned_snapshot = copy.deepcopy(result)
        data["spans"][0]["id"] = "changed-after-return"
        data["meters"][0]["readings"].append({"at": "9", "value": 99})
        self.assertEqual(result, returned_snapshot)

    def test_missing_settled_policy_is_rejected(self):
        data = {
            "spans": [
                span("span-a", service="svc-a"),
                span("span-b", service="svc-b"),
            ],
            "ownership": [{"trace": "trace-a", "account": "acct-a"}],
            "meters": [
                meter("meter-a", service="svc-a", unit="u"),
                meter("meter-b", service="svc-b", unit="u"),
            ],
            # Only svc-a/u has a policy; svc-b/u receives positive usage.
            "budgets": [budget(service="svc-a", unit="u")],
            "requests": [],
        }
        with self.assertRaises(ValueError):
            settle_trace_metering(data)

    def test_invalid_documents_raise_value_error(self):
        def assert_invalid(mutate):
            data = deepest_data()
            mutate(data)
            with self.assertRaises(ValueError):
                settle_trace_metering(data)

        assert_invalid(lambda d: d.update(unknown=1))
        assert_invalid(lambda d: d["meters"][0].update(unknown=1))
        assert_invalid(lambda d: d["meters"][0].update(modulus=True))
        assert_invalid(lambda d: d["meters"].append(copy.deepcopy(d["meters"][0])))

        def duplicate_request(d):
            d["requests"].append(request("request-duplicate"))
            d["requests"].append(request("request-duplicate"))

        assert_invalid(duplicate_request)
        assert_invalid(lambda d: d["budgets"].append(copy.deepcopy(d["budgets"][0])))
        assert_invalid(lambda d: d["requests"].append(request(items=[
            {"service": "svc", "unit": "request", "units": 2},
            {"service": "svc", "unit": "request", "units": 3},
        ])))
        assert_invalid(lambda d: d.update(ownership=[]))
        assert_invalid(lambda d: d["ownership"].append(
            {"trace": "trace-extra", "account": "acct-a"}
        ))
        assert_invalid(lambda d: d["meters"][0].update(service="missing"))
        assert_invalid(lambda d: d["budgets"][0].update(account="missing"))
        assert_invalid(lambda d: d["requests"].append(request(account="missing")))
        assert_invalid(lambda d: d["budgets"][0].update(unit="missing-unit"))
        assert_invalid(lambda d: d["requests"].append(request(items=[
            {"service": "svc", "unit": "missing-policy", "units": 1}
        ])))
        assert_invalid(lambda d: d["spans"][0].update(parent="span-does-not-exist"))

        def make_cycle(d):
            d["spans"][1].update(id="cycle-a", parent="cycle-b")
            d["spans"][2].update(id="cycle-b", parent="cycle-a")
            d["spans"][3].update(parent="cycle-a")

        assert_invalid(make_cycle)
        assert_invalid(lambda d: d["spans"][0].update(end="-1"))
        assert_invalid(lambda d: d["meters"][0]["readings"][0].update(value=True))
        assert_invalid(lambda d: d["budgets"][0].update(limit=True))


if __name__ == "__main__":
    unittest.main()
