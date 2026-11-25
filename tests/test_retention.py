import copy
import json
import random
import unittest

from trace_causality import plan_trace_retention


def obj(
    object_id,
    kind="raw",
    trace="t1",
    created="0",
    expires=None,
    subjects=None,
    depends_on=None,
):
    return {
        "id": object_id,
        "trace": trace,
        "kind": kind,
        "created": created,
        "expires": expires,
        "subjects": [] if subjects is None else subjects,
        "depends_on": [] if depends_on is None else depends_on,
    }


def policy(
    policy_id="pol-star",
    trace="*",
    kind="*",
    keep_until="200",
    priority=0,
):
    return {
        "id": policy_id,
        "trace": trace,
        "kind": kind,
        "keep_until": keep_until,
        "priority": priority,
    }


def product(product_id, roots, status="active"):
    return {"id": product_id, "roots": list(roots), "status": status}


def hold(hold_id, roots, mode="evidence", closed=False):
    return {"id": hold_id, "roots": list(roots), "mode": mode, "closed": closed}


def erasure(request_id, subject, scope="direct", submitted="90"):
    return {
        "id": request_id,
        "subject": subject,
        "submitted": submitted,
        "scope": scope,
    }


def base_data(**overrides):
    data = {
        "now": "100",
        "objects": [obj("raw1", subjects=["alice"])],
        "products": [],
        "policies": [policy()],
        "holds": [],
        "requests": [],
    }
    data.update(overrides)
    return data


class RetentionValidationTest(unittest.TestCase):
    def test_top_level_fields_must_be_exact(self):
        data = base_data()
        data["unexpected"] = 1
        with self.assertRaises(ValueError):
            plan_trace_retention(data)
        del data["now"]
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

    def test_type_and_range_violations_are_rejected(self):
        bad_cases = [
            {"now": 100},
            {"objects": []},
            {"policies": []},
            {"requests": [erasure("r1", "alice", submitted="101")]},
        ]
        for override in bad_cases:
            with self.assertRaises(ValueError):
                plan_trace_retention(base_data(**override))

        data = base_data(
            objects=[obj("o1", kind="raw", created="101")],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[obj("o1", expires="-1")],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            policies=[policy(keep_until="99")],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            policies=[policy(priority=-1)],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

    def test_duplicate_identities_and_bad_references_are_rejected(self):
        data = base_data(objects=[obj("o1"), obj("o1")])
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[
                obj("o1", kind="derived", depends_on=["missing"]),
            ]
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[obj("o1"), obj("o2", subjects=["alice", "alice"])]
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[obj("o1"), obj("o2", depends_on=["o1", "o1"], kind="derived")]
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[obj("o1", kind="derived", depends_on=["o1"])]
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[obj("o1")],
            products=[product("p1", ["missing"])],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[obj("o1")],
            holds=[hold("h1", ["missing"])],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[obj("o1")],
            requests=[erasure("r1", "alice"), erasure("r1", "bob")],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

    def test_kind_dependency_rules_and_cycles_are_rejected(self):
        data = base_data(
            objects=[obj("o1"), obj("o2", kind="raw", depends_on=["o1"])]
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(objects=[obj("o1", kind="derived", depends_on=[])])
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[
                obj("a", kind="derived", depends_on=["b"]),
                obj("b", kind="derived", depends_on=["a"]),
            ]
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

    def test_same_priority_mutually_exclusive_policies_reject_whole_document(self):
        # Both wildcard policies can match every object but disagree on term.
        data = base_data(
            objects=[obj("o1")],
            policies=[
                policy("p1", keep_until="200", priority=5),
                policy("p2", keep_until="300", priority=5),
            ],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        # Same priority and identical term is legal even with wildcards.
        data = base_data(
            objects=[obj("o1")],
            policies=[
                policy("p1", keep_until="300", priority=5),
                policy("p2", keep_until="300", priority=5),
            ],
        )
        result = plan_trace_retention(data)
        self.assertEqual(result["objects"][0]["policy_until"], "300")

        # A selector naming a trace no object carries is rejected outright.
        data = base_data(
            objects=[obj("o1", trace="t1")],
            policies=[
                policy("p1", trace="t1", keep_until="200", priority=5),
                policy("p2", trace="other", keep_until="300", priority=5),
            ],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        # Trace-specific versus wildcard on a present trace still conflicts.
        data = base_data(
            objects=[obj("o1", trace="t1")],
            policies=[
                policy("p1", trace="t1", keep_until="200", priority=5),
                policy("p2", trace="*", keep_until="300", priority=5),
            ],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

    def test_every_object_needs_a_matching_policy(self):
        data = base_data(
            objects=[obj("o1", trace="t1"), obj("o2", trace="t2")],
            policies=[policy("p1", trace="t1")],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

    def test_enum_and_boolean_contracts(self):
        data = base_data(objects=[dict(obj("o1"), kind="weird")])
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[obj("o1")],
            holds=[{"id": "h1", "roots": ["o1"], "mode": "evidence", "closed": "yes"}],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[obj("o1")],
            requests=[dict(erasure("r1", "alice"), scope="global")],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

    def test_floats_and_loose_booleans_are_rejected(self):
        data = base_data()
        data["now"] = 1.5
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(policies=[dict(policy(), priority=1.0)])
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[obj("o1")],
            holds=[hold("h1", ["o1"], closed=1)],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

    def test_kind_rules_cover_catalog_and_derived(self):
        # A catalog object may carry dependencies; empty catalog is legal.
        data = base_data(
            objects=[
                obj("r1"),
                obj("c1", kind="catalog", depends_on=["r1"]),
                obj("c2", kind="catalog"),
            ]
        )
        result = plan_trace_retention(data)
        self.assertEqual(len(result["objects"]), 3)

        data = base_data(
            objects=[obj("r1"), obj("d1", kind="derived", depends_on=[])]
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

    def test_policy_priority_and_selector_precedence(self):
        data = base_data(
            objects=[
                obj("o1", trace="t1", kind="raw"),
                obj("o2", trace="t1", kind="derived", depends_on=["o1"]),
            ],
            policies=[
                policy("low", keep_until="200", priority=0),
                policy("mid", trace="t1", keep_until="300", priority=5),
                policy("high", trace="t1", kind="raw",
                       keep_until="400", priority=9),
            ],
        )
        result = plan_trace_retention(data)
        by_id = {item["id"]: item for item in result["objects"]}
        self.assertEqual(by_id["o1"]["policy_until"], "400")
        self.assertEqual(by_id["o2"]["policy_until"], "300")

    def test_unknown_fields_are_rejected_everywhere(self):
        data = base_data(
            objects=[dict(obj("o1"), extra=1)],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[obj("o1")],
            policies=[dict(policy(), extra=1)],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        data = base_data(
            objects=[obj("o1")],
            requests=[dict(erasure("r1", "alice"), extra=1)],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)


class RetentionPlanningTest(unittest.TestCase):
    def test_expired_object_kept_by_longer_compliance_policy(self):
        # Own expiry (10) has passed at now=100 but the policy term is 150.
        data = base_data(
            objects=[obj("o1", expires="10", subjects=["alice"])],
            policies=[policy(keep_until="150")],
            requests=[erasure("r1", "alice")],
        )
        result = plan_trace_retention(data)
        record = result["objects"][0]
        self.assertEqual(record["policy_until"], "150")
        self.assertFalse(record["expired"])

        request = result["requests"][0]
        self.assertEqual(request["status"], "blocked")
        self.assertEqual(request["delete"], [])
        self.assertEqual(request["selections"], [])
        self.assertEqual(
            request["blockers"], [{"kind": "policy", "ref": "o1"}]
        )

        # Once the policy term arrives the same object is expired and erasable.
        data["now"] = "150"
        result = plan_trace_retention(data)
        self.assertTrue(result["objects"][0]["expired"])
        request = result["requests"][0]
        self.assertEqual(request["status"], "completed")
        self.assertEqual(request["delete"], ["o1"])

    def test_own_expiry_can_extend_beyond_policy_term(self):
        data = base_data(
            objects=[obj("o1", expires="300")],
            policies=[policy(keep_until="150")],
        )
        result = plan_trace_retention(data)
        self.assertEqual(result["objects"][0]["policy_until"], "300")
        self.assertFalse(result["objects"][0]["expired"])

    def test_evidence_and_rebuild_holds_freeze_different_sets(self):
        objects = [
            obj("raw1", subjects=["alice"]),
            obj("raw2"),
            obj("der1", kind="derived", depends_on=["raw1", "raw2"]),
        ]
        data = base_data(
            objects=objects,
            holds=[
                hold("h-ev", ["der1"], mode="evidence"),
                hold("h-rb", ["der1"], mode="rebuild"),
            ],
            requests=[erasure("r1", "alice", scope="rebuildable")],
        )
        result = plan_trace_retention(data)
        holds = {item["id"]: item for item in result["holds"]}
        self.assertEqual(holds["h-ev"]["objects"], ["der1"])
        self.assertEqual(holds["h-rb"]["objects"], ["der1", "raw1", "raw2"])

        held = {item["id"]: item["held_by"] for item in result["objects"]}
        self.assertEqual(held["der1"], ["h-ev", "h-rb"])
        self.assertEqual(held["raw1"], ["h-rb"])
        self.assertEqual(held["raw2"], ["h-rb"])

        # Every target is frozen by a hold; the unexpired policy deadline is
        # reported as an independent blocker as well.
        request = result["requests"][0]
        self.assertEqual(request["status"], "blocked")
        self.assertEqual(
            request["blockers"],
            [
                {"kind": "hold", "ref": "h-ev"},
                {"kind": "hold", "ref": "h-rb"},
                {"kind": "policy", "ref": "der1"},
                {"kind": "policy", "ref": "raw1"},
            ],
        )

    def test_closed_hold_reports_but_does_not_freeze(self):
        data = base_data(
            objects=[obj("o1", subjects=["alice"], expires="50")],
            policies=[policy(keep_until="100")],
            holds=[hold("h1", ["o1"], closed=True)],
            requests=[erasure("r1", "alice")],
        )
        result = plan_trace_retention(data)
        hold_record = result["holds"][0]
        self.assertTrue(hold_record["closed"])
        self.assertEqual(hold_record["objects"], [])
        self.assertEqual(result["objects"][0]["held_by"], [])
        # Policy term 90 <= now 100, so the closed hold permits erasure.
        self.assertEqual(result["requests"][0]["status"], "completed")
        self.assertEqual(result["requests"][0]["delete"], ["o1"])

    def test_direct_and_rebuildable_target_sets_differ(self):
        objects = [
            obj("raw1", subjects=["alice"]),
            obj("raw2"),
            obj("der1", kind="derived", depends_on=["raw1"]),
            obj("der2", kind="derived", depends_on=["der1", "raw2"]),
        ]
        data = base_data(
            now="200",
            objects=objects,
            policies=[policy(keep_until="200")],
            requests=[
                erasure("direct", "alice", scope="direct"),
                erasure("rebuild", "alice", scope="rebuildable"),
            ],
        )
        result = plan_trace_retention(data)
        by_id = {item["id"]: item for item in result["requests"]}
        self.assertEqual(by_id["direct"]["targets"], ["raw1"])
        self.assertEqual(
            by_id["rebuild"]["targets"], ["der1", "der2", "raw1"]
        )
        for request in result["requests"]:
            self.assertEqual(request["status"], "completed")
            self.assertEqual(request["blockers"], [])

    def test_clean_alternative_root_completes_while_polluted_unique_root_blocks(self):
        objects = [
            obj("raw-a", subjects=["alice"]),
            obj("raw-b", subjects=["bob"]),
            obj("root-a", kind="derived", depends_on=["raw-a"]),
            obj("root-b", kind="derived", depends_on=["raw-b"]),
        ]
        data = base_data(
            now="200",
            objects=objects,
            policies=[policy(keep_until="200")],
            products=[product("p1", ["root-a", "root-b"])],
            requests=[erasure("r1", "alice", scope="rebuildable")],
        )
        result = plan_trace_retention(data)
        request = result["requests"][0]
        self.assertEqual(request["status"], "completed")
        self.assertEqual(
            request["selections"], [{"product": "p1", "root": "root-b"}]
        )
        # Polluted route disappears; the clean route and its own raw stay.
        self.assertEqual(request["delete"], ["raw-a", "root-a"])

        # With the clean root removed the product has no rebuildable route.
        data["products"] = [product("p1", ["root-a"])]
        result = plan_trace_retention(data)
        request = result["requests"][0]
        self.assertEqual(request["status"], "blocked")
        self.assertEqual(request["delete"], [])
        self.assertEqual(request["selections"], [])
        self.assertEqual(
            request["blockers"], [{"kind": "product", "ref": "p1"}]
        )

    def test_global_route_choice_beats_per_product_local_choice(self):
        # a closure = {a, x, y} size 3; b = {b, y} size 2; c = {c, z} size 2.
        # Independent local minima choose b for p1 and c for p2 (union 4).
        # The joint optimum selects shared root a for both (union 3).
        objects = [
            obj("raw-alice", subjects=["alice"]),
            obj("x"), obj("y"), obj("z"),
            obj("a", kind="derived", depends_on=["x", "y"]),
            obj("b", kind="derived", depends_on=["y"]),
            obj("c", kind="derived", depends_on=["z"]),
            obj("polluted", kind="derived", depends_on=["raw-alice"]),
        ]
        data = base_data(
            now="200",
            objects=objects,
            policies=[policy(keep_until="200")],
            products=[
                product("p1", ["polluted", "a", "b"]),
                product("p2", ["a", "c"]),
            ],
            requests=[erasure("r1", "alice", scope="rebuildable")],
        )
        result = plan_trace_retention(data)
        request = result["requests"][0]
        self.assertEqual(request["status"], "completed")
        self.assertEqual(
            request["selections"],
            [
                {"product": "p1", "root": "a"},
                {"product": "p2", "root": "a"},
            ],
        )
        retained = {
            item["id"]
            for item in result["objects"]
            if item["id"] not in request["delete"]
        }
        self.assertEqual(retained, {"a", "x", "y"})
        self.assertEqual(
            request["delete"],
            ["b", "c", "polluted", "raw-alice", "z"],
        )

    def test_lexicographic_tie_break_is_stable(self):
        # Two roots with identical closure size; the lex-smaller list wins.
        objects = [
            obj("raw-alice", subjects=["alice"]),
            obj("x"),
            obj("ra", kind="derived", depends_on=["x"]),
            obj("rb", kind="derived", depends_on=["x"]),
            obj("polluted", kind="derived", depends_on=["raw-alice"]),
        ]
        data = base_data(
            now="200",
            objects=objects,
            policies=[policy(keep_until="200")],
            products=[product("p1", ["polluted", "rb", "ra"])],
            requests=[erasure("r1", "alice", scope="rebuildable")],
        )
        result = plan_trace_retention(data)
        self.assertEqual(
            result["requests"][0]["selections"],
            [{"product": "p1", "root": "ra"}],
        )

    def test_cascade_deletes_only_unused_and_expired_objects(self):
        objects = [
            obj("raw-alice", subjects=["alice"], expires="50"),
            obj("der-alice", kind="derived", depends_on=["raw-alice"], expires="60"),
            obj("raw-kept", expires="50"),
            obj("der-kept", kind="derived", depends_on=["raw-kept"], expires="60"),
            obj("raw-fresh", expires="500"),
        ]
        data = base_data(
            now="200",
            objects=objects,
            policies=[policy(keep_until="200")],
            products=[product("p1", ["der-kept"])],
            requests=[erasure("r1", "alice", scope="rebuildable")],
        )
        result = plan_trace_retention(data)
        request = result["requests"][0]
        self.assertEqual(request["status"], "completed")
        # Targets plus the expired derived object losing its only consumer.
        self.assertEqual(request["delete"], ["der-alice", "raw-alice"])
        # The product route retains der-kept and its AND-dependency raw-kept
        # even though both are past their own terms; raw-fresh is unexpired.
        self.assertEqual(
            request["selections"], [{"product": "p1", "root": "der-kept"}]
        )

    def test_blocked_request_does_not_affect_other_requests(self):
        # bob is frozen by a hold and retained by a trace-specific long
        # policy; alice lives on another trace and can still be erased.
        objects = [
            obj("raw-alice", trace="ta", subjects=["alice"]),
            obj("raw-bob", trace="tb", subjects=["bob"]),
        ]
        data = base_data(
            now="200",
            objects=objects,
            policies=[
                policy("p-base", keep_until="200", priority=0),
                policy("p-bob", trace="tb", keep_until="500", priority=1),
            ],
            holds=[hold("h1", ["raw-bob"])],
            requests=[
                erasure("r-block", "bob"),
                erasure("r-ok", "alice"),
            ],
        )
        result = plan_trace_retention(data)
        by_id = {item["id"]: item for item in result["requests"]}
        self.assertEqual(by_id["r-block"]["status"], "blocked")
        self.assertEqual(by_id["r-block"]["delete"], [])
        self.assertEqual(
            by_id["r-block"]["blockers"],
            [
                {"kind": "hold", "ref": "h1"},
                {"kind": "policy", "ref": "raw-bob"},
            ],
        )
        self.assertEqual(by_id["r-ok"]["status"], "completed")
        self.assertEqual(by_id["r-ok"]["delete"], ["raw-alice"])

    def test_retired_product_imposes_no_retention(self):
        objects = [
            obj("raw-alice", subjects=["alice"]),
            obj("der1", kind="derived", depends_on=["raw-alice"]),
        ]
        data = base_data(
            now="200",
            objects=objects,
            policies=[policy(keep_until="200")],
            products=[product("p-old", ["der1"], status="retired")],
            requests=[erasure("r1", "alice", scope="rebuildable")],
        )
        result = plan_trace_retention(data)
        self.assertEqual(result["objects"][0]["active_products"], [])
        request = result["requests"][0]
        self.assertEqual(request["status"], "completed")
        self.assertEqual(request["selections"], [])
        self.assertEqual(request["delete"], ["der1", "raw-alice"])

    def test_direct_scope_keeps_rebuildable_ancestor_out_of_targets(self):
        objects = [
            obj("raw1", subjects=["alice"]),
            obj("der1", kind="derived", depends_on=["raw1"]),
        ]
        data = base_data(
            now="200",
            objects=objects,
            policies=[policy(keep_until="200")],
            requests=[erasure("r1", "alice", scope="direct")],
        )
        result = plan_trace_retention(data)
        request = result["requests"][0]
        self.assertEqual(request["targets"], ["raw1"])
        # der1 is not a target, but its AND-dependency raw1 is erased: der1
        # itself is past term and loses its only consumer, so it is collected.
        self.assertEqual(request["delete"], ["der1", "raw1"])

    def test_and_dependency_keeps_expired_dependency_of_retained_object(self):
        objects = [
            obj("raw1", expires="10"),
            obj("der1", kind="derived", expires=None, depends_on=["raw1"]),
        ]
        data = base_data(
            now="200",
            objects=objects,
            policies=[policy(keep_until="200")],
            products=[product("p1", ["der1"])],
            requests=[erasure("r1", "nobody")],
        )
        result = plan_trace_retention(data)
        request = result["requests"][0]
        # der1 is retained by the active product, so expired raw1 cannot be
        # cascade-collected even though its own term elapsed.
        self.assertEqual(request["delete"], [])
        self.assertEqual(
            request["selections"], [{"product": "p1", "root": "der1"}]
        )

    def test_blockers_are_sorted_by_kind_then_ref_without_duplicates(self):
        objects = [obj("o1", subjects=["alice"])]
        # Two holds freeze the same target; the hold is reported only once.
        data = base_data(
            now="200",
            objects=objects,
            policies=[
                policy("base", keep_until="200", priority=0),
                policy("long", keep_until="500", priority=1),
            ],
            holds=[
                hold("h2", ["o1"]),
                hold("h1", ["o1"], mode="rebuild"),
            ],
            requests=[erasure("r1", "alice")],
        )
        result = plan_trace_retention(data)
        blockers = result["requests"][0]["blockers"]
        self.assertEqual(
            blockers,
            [
                {"kind": "hold", "ref": "h1"},
                {"kind": "hold", "ref": "h2"},
                {"kind": "policy", "ref": "o1"},
            ],
        )
        refs = [(b["kind"], b["ref"]) for b in blockers]
        self.assertEqual(len(refs), len(set(refs)))

    def test_negative_time_six_decimals_and_boundary_equality(self):
        data = base_data(
            now="-1.500000",
            objects=[
                obj(
                    "o1",
                    created="-2.000001",
                    expires="-1.500000",
                    subjects=["alice"],
                )
            ],
            policies=[policy(keep_until="-1.500000")],
            requests=[erasure("r1", "alice", submitted="-1.999999")],
        )
        result = plan_trace_retention(data)
        record = result["objects"][0]
        # now == policy_until means expired (>=), so erasure can complete.
        self.assertEqual(record["policy_until"], "-1.5")
        self.assertTrue(record["expired"])
        self.assertEqual(result["requests"][0]["status"], "completed")
        self.assertEqual(result["requests"][0]["delete"], ["o1"])

    def test_output_order_shape_and_json_serializability(self):
        objects = [
            obj("zebra", subjects=["alice"]),
            obj("alpha"),
            obj("mid", kind="derived", depends_on=["alpha", "zebra"]),
        ]
        data = base_data(
            now="200",
            objects=objects,
            policies=[policy(keep_until="200")],
            products=[product("p9", ["mid"]), product("p1", ["mid"])],
            holds=[hold("h2", ["alpha"]), hold("h1", ["zebra"], mode="rebuild")],
            requests=[erasure("r2", "alice", scope="rebuildable"),
                      erasure("r1", "nobody")],
        )
        result = plan_trace_retention(data)
        # Round trip proves JSON serializability and no Fraction leakage.
        cloned = json.loads(json.dumps(result))
        self.assertEqual(cloned, result)
        self.assertEqual(result["schema"], "trace-retention-plan-v1")
        self.assertEqual(set(result), {"schema", "objects", "holds", "requests"})
        self.assertEqual([item["id"] for item in result["objects"]],
                         ["alpha", "mid", "zebra"])
        self.assertEqual([item["id"] for item in result["holds"]], ["h1", "h2"])
        self.assertEqual([item["id"] for item in result["requests"]], ["r1", "r2"])

        mid = next(item for item in result["objects"] if item["id"] == "mid")
        self.assertEqual(mid["active_products"], ["p1", "p9"])
        self.assertEqual(set(mid), {
            "id", "trace", "kind", "policy_until", "expired",
            "held_by", "active_products",
        })
        hold_record = result["holds"][0]
        self.assertEqual(set(hold_record), {"id", "mode", "closed", "objects"})
        request_record = result["requests"][0]
        self.assertEqual(set(request_record), {
            "id", "subject", "scope", "status", "targets", "delete",
            "blockers", "selections",
        })
        # A subject present in no object yields an empty, completed request;
        # the active products still name their required surviving routes.
        nobody = next(item for item in result["requests"] if item["id"] == "r1")
        self.assertEqual(nobody["status"], "completed")
        self.assertEqual(nobody["targets"], [])
        self.assertEqual(nobody["delete"], [])
        self.assertEqual(
            nobody["selections"],
            [
                {"product": "p1", "root": "mid"},
                {"product": "p9", "root": "mid"},
            ],
        )

        # The rebuildable erasure is blocked by the rebuild hold and because
        # no clean product route remains; blockers are sorted by kind, ref.
        blocked = next(item for item in result["requests"] if item["id"] == "r2")
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["delete"], [])
        self.assertEqual(blocked["selections"], [])
        self.assertEqual(
            blocked["blockers"],
            [
                {"kind": "hold", "ref": "h1"},
                {"kind": "product", "ref": "p1"},
                {"kind": "product", "ref": "p9"},
            ],
        )

    def test_input_reorder_does_not_change_output(self):
        objects = [
            obj("raw1", subjects=["alice"]),
            obj("raw2"),
            obj("der1", kind="derived", depends_on=["raw1", "raw2"]),
        ]
        data = base_data(
            now="200",
            objects=objects,
            policies=[
                policy("p2", trace="t1", kind="raw", keep_until="200", priority=1),
                policy("p1", keep_until="300", priority=0),
            ],
            products=[product("p1", ["der1"])],
            holds=[hold("h1", ["raw2"], mode="rebuild")],
            requests=[erasure("r1", "alice", scope="rebuildable")],
        )
        baseline = plan_trace_retention(copy.deepcopy(data))

        shuffled = copy.deepcopy(data)
        rng = random.Random(42)
        rng.shuffle(shuffled["objects"])
        rng.shuffle(shuffled["policies"])
        rng.shuffle(shuffled["holds"])
        rng.shuffle(shuffled["requests"])
        for item in shuffled["objects"]:
            rng.shuffle(item["depends_on"])
        self.assertEqual(plan_trace_retention(shuffled), baseline)

    def test_input_is_not_mutated(self):
        data = base_data(
            now="200",
            objects=[
                obj("raw1", subjects=["alice"]),
                obj("der1", kind="derived", depends_on=["raw1"]),
            ],
            policies=[policy(keep_until="200")],
            requests=[erasure("r1", "alice", scope="rebuildable")],
        )
        snapshot = copy.deepcopy(data)
        plan_trace_retention(data)
        self.assertEqual(data, snapshot)

    def test_parallel_route_bound_breach_rejects_document(self):
        objects = [obj("raw-alice", subjects=["alice"])]
        products = []
        for index in range(19):
            objects.append(
                obj(f"poll{index}", kind="derived",
                    depends_on=["raw-alice"])
            )
            objects.append(obj(f"x{index}"))
            objects.append(
                obj(f"ra{index}", kind="derived", depends_on=[f"x{index}"])
            )
            objects.append(
                obj(f"rb{index}", kind="derived", depends_on=[f"x{index}"])
            )
            products.append(
                product(f"p{index}", [f"poll{index}", f"ra{index}", f"rb{index}"])
            )

        data = base_data(
            now="200",
            objects=objects,
            policies=[policy(keep_until="200")],
            products=products,
            requests=[erasure("r1", "alice", scope="rebuildable")],
        )
        with self.assertRaises(ValueError):
            plan_trace_retention(data)

        # Exactly 18 flexible products (36 flexible roots) is still accepted.
        data["products"] = products[:18]
        result = plan_trace_retention(data)
        self.assertEqual(result["requests"][0]["status"], "completed")
        self.assertEqual(
            len(result["requests"][0]["selections"]), 18
        )


if __name__ == "__main__":
    unittest.main()
