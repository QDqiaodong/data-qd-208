import copy
import hashlib
import json
import unittest

from trace_causality import audit_trace_attestations, process


def canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def span(span_id, trace="t1", parent=None, service="api", start="0", end="10"):
    return {
        "id": span_id,
        "trace": trace,
        "parent": parent,
        "service": service,
        "start": start,
        "end": end,
    }


def source_digest(spans):
    ordered = sorted(spans, key=lambda item: item["id"])
    kept = [
        {key: item[key] for key in ("id", "trace", "parent", "service", "start", "end")}
        for item in ordered
    ]
    return digest({"spans": kept})


def analysis_digest(spans):
    return digest(process({"spans": spans}))


def snapshot(snapshot_id, spans):
    return {"id": snapshot_id, "spans": spans}


def attestation(attestation_id, snapshot_id, spans, replaces=None, source=None, analysis=None):
    return {
        "id": attestation_id,
        "snapshot": snapshot_id,
        "source_digest": source_digest(spans) if source is None else source,
        "analysis_digest": analysis_digest(spans) if analysis is None else analysis,
        "replaces": replaces,
    }


def withdrawal(withdrawal_id, attestation_id, reason="retracted"):
    return {"id": withdrawal_id, "attestation": attestation_id, "reason": reason}


def seal_for(bundle_id, attestation_ids, attestations_by_id, dependency_ids=(), expected_seals=None):
    return digest({
        "id": bundle_id,
        "attestations": [
            {
                "id": attestation_id,
                "source_digest": attestations_by_id[attestation_id]["source_digest"],
                "analysis_digest": attestations_by_id[attestation_id]["analysis_digest"],
            }
            for attestation_id in sorted(attestation_ids)
        ],
        "depends_on": [
            {"id": dependency, "expected_seal": (expected_seals or {})[dependency]}
            for dependency in sorted(dependency_ids)
        ],
    })


def wrong_seal(*args, **kwargs):
    seal = seal_for(*args, **kwargs)
    return ("1" if seal[0] != "1" else "2") + seal[1:]


def bundle(bundle_id, attestation_ids, attestations_by_id, dependency_ids=(), expected_seals=None, seal=None):
    if seal is None:
        seal = seal_for(bundle_id, attestation_ids, attestations_by_id, dependency_ids, expected_seals)
    return {
        "id": bundle_id,
        "attestations": list(attestation_ids),
        "depends_on": list(dependency_ids),
        "seal": seal,
    }


def audit_data(snapshots, attestations, withdrawals=(), bundles=()):
    return {
        "snapshots": list(snapshots),
        "attestations": list(attestations),
        "withdrawals": list(withdrawals),
        "bundles": list(bundles),
    }


def by_id(entries):
    return {entry["id"]: entry for entry in entries}


def scramble(value):
    """Deep-copy with every list reversed and every dict key order flipped."""
    if isinstance(value, dict):
        return {key: scramble(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [scramble(item) for item in reversed(value)]
    return value


def base_spans():
    return [
        span("root", start="0", end="10"),
        span("db", parent="root", service="db", start="2", end="8"),
        span("cache", parent="root", service="cache", start="3", end="5"),
    ]


def rich_spans():
    # Multiple roots, a zero-duration span, overlapping siblings, and a child
    # crossing its parent's boundaries in a second trace.
    return [
        span("root-a", trace="t1", start="0", end="10"),
        span("db", trace="t1", parent="root-a", service="db", start="2", end="8"),
        span("cache", trace="t1", parent="root-a", service="cache", start="3", end="5"),
        span("zero", trace="t1", parent="root-a", service="cache", start="4", end="4"),
        span("root-b", trace="t1", service="worker", start="1", end="3"),
        span("pt2", trace="t2", service="svc", start="0", end="1"),
        span("cross", trace="t2", parent="pt2", service="svc", start="-1.500000", end="11.250000"),
    ]


class SnapshotDigestTest(unittest.TestCase):
    def test_source_digest_matches_hand_built_canonical_json(self):
        spans = [span("a", trace="t", service="svc", start="0", end="1")]
        data = audit_data([snapshot("s1", spans)], [attestation("a1", "s1", spans)])
        result = audit_trace_attestations(data)
        hand = '{"spans":[{"end":"1","id":"a","parent":null,"service":"svc","start":"0","trace":"t"}]}'
        self.assertEqual(
            result["snapshots"][0]["source_digest"],
            hashlib.sha256(hand.encode("utf-8")).hexdigest(),
        )

    def test_analysis_digest_matches_hand_built_canonical_json(self):
        spans = [span("a", trace="t", service="svc", start="0", end="1")]
        data = audit_data([snapshot("s1", spans)], [attestation("a1", "s1", spans)])
        result = audit_trace_attestations(data)
        hand = (
            '{"summary":{"roots":1,"spans":1,"traces":1},"traces":['
            '{"children":{"a":[]},"roots":["a"],"self_time":{"a":"1"},'
            '"services":["svc"],"trace":"t","wall_time":"1"}]}'
        )
        self.assertEqual(
            result["snapshots"][0]["analysis_digest"],
            hashlib.sha256(hand.encode("utf-8")).hexdigest(),
        )

    def test_digests_ignore_span_list_and_key_order(self):
        spans = rich_spans()
        forward = audit_data([snapshot("s1", spans)], [attestation("a1", "s1", spans)])
        shuffled = scramble(forward)
        result = audit_trace_attestations(forward)
        again = audit_trace_attestations(shuffled)
        self.assertEqual(result["snapshots"], again["snapshots"])
        self.assertEqual(result["attestations"][0]["content_status"], "verified")
        self.assertEqual(again["attestations"][0]["content_status"], "verified")

    def test_analysis_semantics_multiple_roots_zero_overlap_crossing(self):
        spans = rich_spans()
        result = audit_trace_attestations(
            audit_data([snapshot("s1", spans)], [attestation("a1", "s1", spans)])
        )
        analysis = process({"spans": spans})
        self.assertEqual(result["snapshots"][0]["analysis_digest"], digest(analysis))
        traces = {trace["trace"]: trace for trace in analysis["traces"]}
        self.assertEqual(traces["t1"]["roots"], ["root-a", "root-b"])
        self.assertEqual(traces["t1"]["self_time"]["root-a"], "4")
        self.assertEqual(traces["t1"]["self_time"]["zero"], "0")
        self.assertEqual(traces["t2"]["self_time"]["pt2"], "0")
        self.assertEqual(traces["t2"]["self_time"]["cross"], "51/4")
        self.assertEqual(traces["t2"]["wall_time"], "51/4")

    def test_same_span_id_may_appear_in_distinct_snapshots(self):
        spans_a = [span("root", start="0", end="5")]
        spans_b = [span("root", start="0", end="9")]
        data = audit_data(
            [snapshot("s1", spans_a), snapshot("s2", spans_b)],
            [attestation("a1", "s1", spans_a), attestation("a2", "s2", spans_b)],
        )
        result = audit_trace_attestations(data)
        self.assertNotEqual(
            result["snapshots"][0]["source_digest"],
            result["snapshots"][1]["source_digest"],
        )
        self.assertTrue(
            all(entry["content_status"] == "verified" for entry in result["attestations"])
        )


class AttestationContentTest(unittest.TestCase):
    def setUp(self):
        self.spans = base_spans()

    def audit(self, attestations, **kwargs):
        data = audit_data([snapshot("s1", self.spans)], attestations, **kwargs)
        return audit_trace_attestations(data)

    def test_verified_attestation_is_usable(self):
        result = self.audit([attestation("a1", "s1", self.spans)])
        entry = result["attestations"][0]
        self.assertEqual(entry["content_status"], "verified")
        self.assertEqual(entry["mismatches"], [])
        self.assertFalse(entry["withdrawn"])
        self.assertIsNone(entry["withdrawal_id"])
        self.assertIsNone(entry["superseded_by"])
        self.assertTrue(entry["usable"])

    def test_forged_analysis_digest_fails_despite_correct_source_digest(self):
        forged = attestation("a1", "s1", self.spans, analysis="0" * 64)
        sealed = bundle("b1", ["a1"], {"a1": forged})
        result = self.audit([forged], bundles=[sealed])
        entry = result["attestations"][0]
        self.assertEqual(entry["content_status"], "mismatch")
        self.assertEqual(entry["mismatches"], ["analysis_digest"])
        self.assertFalse(entry["usable"])
        bundle_entry = result["bundles"][0]
        # The seal froze the declared digests, so the bundle is blocked by
        # content, never waved through by the correct source digest.
        self.assertEqual(bundle_entry["status"], "blocked")
        self.assertEqual(bundle_entry["issues"], [{"kind": "attestation_mismatch", "ref": "a1"}])

    def test_forged_source_digest_only_and_both(self):
        only_source = attestation("a1", "s1", self.spans, source="1" * 64)
        result = self.audit([only_source])
        self.assertEqual(result["attestations"][0]["mismatches"], ["source_digest"])

        both = attestation("a1", "s1", self.spans, source="1" * 64, analysis="0" * 64)
        result = self.audit([both])
        self.assertEqual(
            result["attestations"][0]["mismatches"],
            ["analysis_digest", "source_digest"],
        )


class SupersessionTest(unittest.TestCase):
    def setUp(self):
        self.spans = base_spans()

    def run_audit(self, attestations, withdrawals=()):
        data = audit_data([snapshot("s1", self.spans)], attestations, withdrawals)
        return audit_trace_attestations(data)

    def test_faulty_replacement_does_not_supersede_verified_original(self):
        a1 = attestation("a1", "s1", self.spans)
        a2 = attestation("a2", "s1", self.spans, replaces="a1", analysis="0" * 64)
        entries = by_id(self.run_audit([a1, a2])["attestations"])
        self.assertIsNone(entries["a1"]["superseded_by"])
        self.assertTrue(entries["a1"]["usable"])
        self.assertEqual(entries["a2"]["content_status"], "mismatch")
        self.assertFalse(entries["a2"]["usable"])

    def test_verified_successor_supersedes_across_faulty_intermediate(self):
        a1 = attestation("a1", "s1", self.spans)
        a2 = attestation("a2", "s1", self.spans, replaces="a1", source="1" * 64)
        a3 = attestation("a3", "s1", self.spans, replaces="a2")
        # Input order is not seniority; the chain alone decides.
        entries = by_id(self.run_audit([a3, a1, a2])["attestations"])
        self.assertEqual(entries["a1"]["superseded_by"], "a3")
        self.assertEqual(entries["a2"]["superseded_by"], "a3")
        self.assertFalse(entries["a1"]["usable"])
        self.assertFalse(entries["a2"]["usable"])
        self.assertIsNone(entries["a3"]["superseded_by"])
        self.assertTrue(entries["a3"]["usable"])

    def test_nearest_verified_successor_is_reported(self):
        a1 = attestation("a1", "s1", self.spans)
        a2 = attestation("a2", "s1", self.spans, replaces="a1")
        a3 = attestation("a3", "s1", self.spans, replaces="a2")
        entries = by_id(self.run_audit([a1, a2, a3])["attestations"])
        self.assertEqual(entries["a1"]["superseded_by"], "a2")
        self.assertEqual(entries["a2"]["superseded_by"], "a3")
        self.assertIsNone(entries["a3"]["superseded_by"])

    def test_withdrawn_verified_successor_keeps_ancestors_superseded(self):
        a1 = attestation("a1", "s1", self.spans)
        a2 = attestation("a2", "s1", self.spans, replaces="a1")
        entries = by_id(self.run_audit([a1, a2], [withdrawal("w1", "a2")])["attestations"])
        # The withdrawn successor still blocks itself and the old attestation
        # must not be revived.
        self.assertEqual(entries["a1"]["superseded_by"], "a2")
        self.assertFalse(entries["a1"]["usable"])
        self.assertTrue(entries["a2"]["withdrawn"])
        self.assertEqual(entries["a2"]["withdrawal_id"], "w1")
        self.assertFalse(entries["a2"]["usable"])

    def test_mismatched_attestation_can_be_withdrawn(self):
        a1 = attestation("a1", "s1", self.spans, source="1" * 64)
        result = self.run_audit([a1], [withdrawal("w9", "a1", reason="signed from stale evidence")])
        entry = result["attestations"][0]
        self.assertEqual(entry["content_status"], "mismatch")
        self.assertTrue(entry["withdrawn"])
        self.assertEqual(entry["withdrawal_id"], "w9")
        self.assertFalse(entry["usable"])


class BundleSealTest(unittest.TestCase):
    def setUp(self):
        self.spans = base_spans()
        self.att = attestation("a1", "s1", self.spans)
        self.atts = {"a1": self.att}

    def run_audit(self, bundles, attestations=None, withdrawals=()):
        data = audit_data(
            [snapshot("s1", self.spans)],
            attestations if attestations is not None else [self.att],
            withdrawals,
            bundles,
        )
        return audit_trace_attestations(data)

    def test_seal_matches_hand_built_canonical_document(self):
        hand = (
            '{"attestations":[{"analysis_digest":"%s","id":"a1","source_digest":"%s"}],'
            '"depends_on":[],"id":"b1"}' % (self.att["analysis_digest"], self.att["source_digest"])
        )
        seal = hashlib.sha256(hand.encode("utf-8")).hexdigest()
        result = self.run_audit([bundle("b1", ["a1"], self.atts, seal=seal)])
        entry = result["bundles"][0]
        self.assertEqual(entry["expected_seal"], seal)
        self.assertEqual(entry["seal"], seal)
        self.assertEqual(entry["status"], "usable")
        self.assertEqual(entry["issues"], [])

    def test_invalid_seal_keeps_frozen_and_expected_seal(self):
        wrong = wrong_seal("b1", ["a1"], self.atts)
        result = self.run_audit([bundle("b1", ["a1"], self.atts, seal=wrong)])
        entry = result["bundles"][0]
        self.assertEqual(entry["status"], "invalid_seal")
        self.assertEqual(entry["seal"], wrong)
        self.assertEqual(entry["expected_seal"], seal_for("b1", ["a1"], self.atts))

    def test_parent_must_use_child_expected_seal_not_reported_seal(self):
        child_expected = seal_for("child", ["a1"], self.atts)
        child_wrong = wrong_seal("child", ["a1"], self.atts)
        child = bundle("child", ["a1"], self.atts, seal=child_wrong)

        # Parent sealed over the child's recursively computed expected seal:
        # its own seal is valid, so the child failure only blocks it.
        parent = bundle(
            "parent", ["a1"], self.atts, ["child"], {"child": child_expected}
        )
        # A parent sealed over the child's reported wrong seal must not look
        # valid; its own seal no longer matches its expected seal.
        parent_bad = bundle(
            "parent-bad", ["a1"], self.atts, ["child"], {"child": child_wrong}
        )

        entries = by_id(self.run_audit([child, parent, parent_bad])["bundles"])
        self.assertEqual(entries["child"]["status"], "invalid_seal")

        self.assertEqual(entries["parent"]["status"], "blocked")
        self.assertEqual(entries["parent"]["seal"], entries["parent"]["expected_seal"])
        self.assertEqual(
            entries["parent"]["issues"],
            [{"kind": "dependency_invalid_seal", "ref": "child"}],
        )

        self.assertEqual(entries["parent-bad"]["status"], "invalid_seal")
        self.assertNotEqual(
            entries["parent-bad"]["seal"], entries["parent-bad"]["expected_seal"]
        )

    def test_seal_freezes_declared_digests_not_current_truth(self):
        forged = attestation("a1", "s1", self.spans, analysis="0" * 64)
        atts = {"a1": forged}
        # A seal over the declared (forged) digests is a valid frozen seal;
        # the bundle is blocked by content, not marked invalid_seal.
        result = self.run_audit([bundle("b1", ["a1"], atts)], attestations=[forged])
        entry = result["bundles"][0]
        self.assertEqual(entry["status"], "blocked")
        self.assertEqual(entry["seal"], entry["expected_seal"])


class BundleStatusTest(unittest.TestCase):
    def setUp(self):
        self.spans = base_spans()
        self.att_ok = attestation("ok", "s1", self.spans)
        self.att_bad = attestation("bad", "s1", self.spans, analysis="0" * 64)
        self.att_old = attestation("old", "s1", self.spans)
        self.att_new = attestation("new", "s1", self.spans, replaces="old")
        self.att_gone = attestation("gone", "s1", self.spans)
        self.atts = {
            item["id"]: item
            for item in (self.att_ok, self.att_bad, self.att_old, self.att_new, self.att_gone)
        }
        self.withdrawals = [withdrawal("w1", "gone")]

    def sealed(self, bundle_id, attestation_ids=("ok",), dependency_ids=(), expected_seals=None, seal=None):
        return bundle(
            bundle_id, attestation_ids, self.atts, dependency_ids, expected_seals, seal
        )

    def run_audit(self, bundles):
        data = audit_data(
            [snapshot("s1", self.spans)],
            list(self.atts.values()),
            self.withdrawals,
            bundles,
        )
        return audit_trace_attestations(data)

    def test_withdrawal_and_superseded_propagate_through_multiple_levels(self):
        b_w1 = self.sealed("w1b", ["gone"])
        b_w2 = self.sealed("w2b", ["ok"], ["w1b"], {"w1b": b_w1["seal"]})
        b_w3 = self.sealed("w3b", ["ok"], ["w2b"], {"w2b": b_w2["seal"]})
        b_s1 = self.sealed("s1b", ["old"])
        b_s2 = self.sealed("s2b", ["ok"], ["s1b"], {"s1b": b_s1["seal"]})
        b_s3 = self.sealed("s3b", ["ok"], ["s2b"], {"s2b": b_s2["seal"]})

        entries = by_id(self.run_audit([b_w3, b_w1, b_w2, b_s2, b_s3, b_s1])["bundles"])
        for bundle_id in ("w1b", "w2b", "w3b"):
            self.assertEqual(entries[bundle_id]["status"], "withdrawn")
        for bundle_id in ("s1b", "s2b", "s3b"):
            self.assertEqual(entries[bundle_id]["status"], "superseded")
        self.assertEqual(
            entries["w1b"]["issues"], [{"kind": "attestation_withdrawn", "ref": "gone"}]
        )
        self.assertEqual(
            entries["w2b"]["issues"], [{"kind": "dependency_withdrawn", "ref": "w1b"}]
        )
        self.assertEqual(
            entries["w3b"]["issues"], [{"kind": "dependency_withdrawn", "ref": "w2b"}]
        )
        self.assertEqual(
            entries["s1b"]["issues"], [{"kind": "attestation_superseded", "ref": "old"}]
        )
        self.assertEqual(
            entries["s3b"]["issues"], [{"kind": "dependency_superseded", "ref": "s2b"}]
        )

    def test_status_priority_and_issue_completeness(self):
        c_invalid = self.sealed("c-invalid", ["ok"], seal=wrong_seal("c-invalid", ["ok"], self.atts))
        c_withdrawn = self.sealed("c-withdrawn", ["gone"])
        c_superseded = self.sealed("c-superseded", ["old"])
        c_blocked = self.sealed("c-blocked", ["bad"])

        p1 = self.sealed("p1", ["gone", "bad"])
        p2 = self.sealed("p2", ["old", "bad"])
        p3 = self.sealed(
            "p3", ["bad"], ["c-invalid"], {"c-invalid": seal_for("c-invalid", ["ok"], self.atts)}
        )
        p4 = self.sealed("p4", ["bad"], ["c-withdrawn"], {"c-withdrawn": c_withdrawn["seal"]})
        p5 = self.sealed("p5", ["bad"], ["c-superseded"], {"c-superseded": c_superseded["seal"]})
        p6 = self.sealed("p6", ["gone"], seal=wrong_seal("p6", ["gone"], self.atts))
        p7 = self.sealed("p7", ["ok"], ["c-blocked"], {"c-blocked": c_blocked["seal"]})

        entries = by_id(self.run_audit([
            c_invalid, c_withdrawn, c_superseded, c_blocked, p1, p2, p3, p4, p5, p6, p7,
        ])["bundles"])

        self.assertEqual(entries["p1"]["status"], "withdrawn")
        self.assertEqual(entries["p1"]["issues"], [
            {"kind": "attestation_mismatch", "ref": "bad"},
            {"kind": "attestation_withdrawn", "ref": "gone"},
        ])
        self.assertEqual(entries["p2"]["status"], "superseded")
        self.assertEqual(entries["p2"]["issues"], [
            {"kind": "attestation_mismatch", "ref": "bad"},
            {"kind": "attestation_superseded", "ref": "old"},
        ])
        self.assertEqual(entries["p3"]["status"], "blocked")
        self.assertEqual(entries["p3"]["issues"], [
            {"kind": "attestation_mismatch", "ref": "bad"},
            {"kind": "dependency_invalid_seal", "ref": "c-invalid"},
        ])
        self.assertEqual(entries["p4"]["status"], "withdrawn")
        self.assertEqual(entries["p5"]["status"], "superseded")
        self.assertEqual(entries["p6"]["status"], "invalid_seal")
        self.assertEqual(
            entries["p6"]["issues"], [{"kind": "attestation_withdrawn", "ref": "gone"}]
        )
        self.assertEqual(entries["p7"]["status"], "blocked")
        self.assertEqual(
            entries["p7"]["issues"], [{"kind": "dependency_blocked", "ref": "c-blocked"}]
        )

    def test_usable_bundle_has_empty_issues(self):
        result = self.run_audit([self.sealed("fine", ["ok", "new"])])
        entry = result["bundles"][0]
        self.assertEqual(entry["status"], "usable")
        self.assertEqual(entry["issues"], [])


class SnapshotChangeTest(unittest.TestCase):
    def test_snapshot_change_only_reruns_content_check_and_dependent_status(self):
        spans_v1 = base_spans()
        spans_v2 = [
            dict(item, end="9") if item["id"] == "root" else dict(item)
            for item in spans_v1
        ]
        att = attestation("a1", "s1", spans_v1)  # digests frozen at issuance
        atts = {"a1": att}
        sealed = bundle("b1", ["a1"], atts)

        data_v1 = audit_data([snapshot("s1", spans_v1)], [att], bundles=[sealed])
        data_v2 = audit_data(
            [snapshot("s1", spans_v2)], [copy.deepcopy(att)], bundles=[copy.deepcopy(sealed)]
        )

        result_v1 = audit_trace_attestations(data_v1)
        result_v2 = audit_trace_attestations(data_v2)

        self.assertEqual(result_v1["attestations"][0]["content_status"], "verified")
        self.assertEqual(result_v1["bundles"][0]["status"], "usable")

        changed = result_v2["attestations"][0]
        self.assertEqual(changed["content_status"], "mismatch")
        self.assertEqual(changed["mismatches"], ["analysis_digest", "source_digest"])
        # No replacement or withdrawal facts appear out of nowhere.
        self.assertIsNone(changed["superseded_by"])
        self.assertFalse(changed["withdrawn"])
        self.assertIsNone(changed["withdrawal_id"])

        bundle_entry = result_v2["bundles"][0]
        self.assertEqual(bundle_entry["status"], "blocked")
        self.assertEqual(bundle_entry["issues"], [{"kind": "attestation_mismatch", "ref": "a1"}])
        # The frozen seal and the expected seal are not rewritten by drift.
        self.assertEqual(bundle_entry["seal"], result_v1["bundles"][0]["seal"])
        self.assertEqual(bundle_entry["expected_seal"], result_v1["bundles"][0]["expected_seal"])
        self.assertNotEqual(
            result_v1["snapshots"][0]["source_digest"],
            result_v2["snapshots"][0]["source_digest"],
        )


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.spans = base_spans()
        self.att = attestation("a1", "s1", self.spans)
        self.atts = {"a1": self.att}

    def base_data(self):
        return audit_data(
            [snapshot("s1", self.spans)],
            [copy.deepcopy(self.att)],
            [],
            [bundle("b1", ["a1"], self.atts)],
        )

    def assert_rejected(self, data):
        before = copy.deepcopy(data)
        with self.assertRaises(ValueError):
            audit_trace_attestations(data)
        self.assertEqual(data, before)

    def test_top_level_must_have_exactly_the_four_fields(self):
        self.assert_rejected([])
        self.assert_rejected("not-a-dict")
        data = self.base_data()
        data["extra"] = True
        self.assert_rejected(data)
        data = self.base_data()
        del data["withdrawals"]
        self.assert_rejected(data)

    def test_snapshot_structure_and_identity(self):
        data = self.base_data()
        data["snapshots"] = []
        self.assert_rejected(data)

        data = self.base_data()
        data["snapshots"].append(snapshot("s1", [span("other", end="1")]))
        self.assert_rejected(data)

        data = self.base_data()
        data["snapshots"][0]["future"] = True
        self.assert_rejected(data)

        data = self.base_data()
        data["snapshots"][0]["id"] = "bad id"
        self.assert_rejected(data)

    def test_snapshot_span_rule_violations_fail_the_whole_batch(self):
        bad_span_sets = [
            [],
            [span("a"), span("a")],
            [span("a", parent="ghost")],
            [span("a", trace="t1", parent="b"), span("b", trace="t2")],
            [span("a", parent="b"), span("b", parent="a")],
            [span("a", start="2", end="1")],
            [span("a", start="0.1234567")],
            [dict(span("a"), unknown=1)],
        ]
        for bad_spans in bad_span_sets:
            with self.subTest(bad_spans=bad_spans):
                data = self.base_data()
                data["snapshots"].append(snapshot("s2", bad_spans))
                self.assert_rejected(data)

    def test_snapshot_count_and_total_span_bounds(self):
        data = self.base_data()
        data["snapshots"] = [snapshot(f"s{i}", [span("a", end="1")]) for i in range(257)]
        self.assert_rejected(data)

        many = [
            snapshot(f"s{i}", [span(f"sp{j}", start="0", end="1") for j in range(2000)])
            for i in range(50)
        ]
        data = audit_data(many, [attestation("a1", "s0", many[0]["spans"])])
        result = audit_trace_attestations(data)  # exactly 100000 spans is allowed
        self.assertEqual(len(result["snapshots"]), 50)

        data["snapshots"].append(snapshot("s50", [span("one-more", end="1")]))
        self.assert_rejected(data)

    def test_attestation_validation(self):
        data = self.base_data()
        data["attestations"][0]["note"] = "x"
        self.assert_rejected(data)

        data = self.base_data()
        data["attestations"].append(attestation("a1", "s1", self.spans))
        self.assert_rejected(data)

        data = self.base_data()
        data["attestations"][0]["snapshot"] = "ghost"
        self.assert_rejected(data)

        for bad in ("0" * 63, "0" * 65, "A" * 64, "g" * 64, 7, None):
            with self.subTest(bad=bad):
                data = self.base_data()
                data["attestations"][0]["source_digest"] = bad
                self.assert_rejected(data)
                data = self.base_data()
                data["attestations"][0]["analysis_digest"] = bad
                self.assert_rejected(data)

        data = self.base_data()
        data["attestations"] = []
        self.assert_rejected(data)

        data = self.base_data()
        data["attestations"] = [
            attestation(f"a{i}", "s1", self.spans, source="0" * 64, analysis="0" * 64)
            for i in range(4097)
        ]
        self.assert_rejected(data)

    def test_replaces_relation_validation(self):
        data = self.base_data()
        data["attestations"][0]["replaces"] = "ghost"
        self.assert_rejected(data)

        data = self.base_data()
        data["attestations"][0]["replaces"] = "a1"
        self.assert_rejected(data)

        data = self.base_data()
        data["attestations"].append(attestation("a2", "s1", self.spans, replaces="a1"))
        data["attestations"].append(attestation("a3", "s1", self.spans, replaces="a1"))
        self.assert_rejected(data)

        data = self.base_data()
        data["attestations"][0]["replaces"] = "a2"
        data["attestations"].append(attestation("a2", "s1", self.spans, replaces="a1"))
        self.assert_rejected(data)

        data = self.base_data()
        other_spans = [span("other", end="3")]
        data["snapshots"].append(snapshot("s2", other_spans))
        data["attestations"].append(attestation("a2", "s2", other_spans, replaces="a1"))
        self.assert_rejected(data)

    def test_withdrawal_validation(self):
        data = self.base_data()
        data["withdrawals"] = [dict(withdrawal("w1", "a1"), extra=1)]
        self.assert_rejected(data)

        data = self.base_data()
        data["withdrawals"] = [withdrawal("w1", "a1"), withdrawal("w1", "a1")]
        self.assert_rejected(data)

        data = self.base_data()
        data["withdrawals"] = [withdrawal("w1", "a1"), withdrawal("w2", "a1")]
        self.assert_rejected(data)

        data = self.base_data()
        data["withdrawals"] = [withdrawal("w1", "ghost")]
        self.assert_rejected(data)

        for bad in ("", "x" * 257, " lead", "trail ", "\ttab", 5, None):
            with self.subTest(bad=bad):
                data = self.base_data()
                data["withdrawals"] = [withdrawal("w1", "a1", reason=bad)]
                self.assert_rejected(data)

        data = self.base_data()
        data["withdrawals"] = [withdrawal("w1", "a1", reason="撤" * 256)]
        result = audit_trace_attestations(data)
        self.assertTrue(result["attestations"][0]["withdrawn"])
        self.assertEqual(result["attestations"][0]["withdrawal_id"], "w1")

        data = self.base_data()
        data["withdrawals"] = [withdrawal(f"w{i}", "a1") for i in range(4097)]
        self.assert_rejected(data)

    def test_bundle_validation(self):
        data = self.base_data()
        data["bundles"][0]["extra"] = 1
        self.assert_rejected(data)

        data = self.base_data()
        data["bundles"].append(bundle("b1", ["a1"], self.atts))
        self.assert_rejected(data)

        data = self.base_data()
        data["bundles"][0]["attestations"] = ["ghost"]
        self.assert_rejected(data)

        data = self.base_data()
        data["bundles"][0]["attestations"] = ["a1", "a1"]
        self.assert_rejected(data)

        data = self.base_data()
        data["bundles"][0]["attestations"] = []
        self.assert_rejected(data)

        data = self.base_data()
        data["bundles"][0]["attestations"] = [f"a{i}" for i in range(257)]
        self.assert_rejected(data)

        data = self.base_data()
        data["bundles"][0]["depends_on"] = ["ghost"]
        self.assert_rejected(data)

        data = self.base_data()
        data["bundles"][0]["depends_on"] = ["b1"]
        self.assert_rejected(data)

        data = self.base_data()
        data["bundles"].append(bundle("b2", ["a1"], self.atts))
        data["bundles"][0]["depends_on"] = ["b2", "b2"]
        self.assert_rejected(data)

        data = self.base_data()
        data["bundles"].append(
            bundle("b2", ["a1"], self.atts, ["b1"], {"b1": seal_for("b1", ["a1"], self.atts)})
        )
        data["bundles"][0]["depends_on"] = ["b2"]
        self.assert_rejected(data)

        data = self.base_data()
        data["bundles"][0]["depends_on"] = [f"b{i}" for i in range(257)]
        self.assert_rejected(data)

        for bad in ("0" * 63, "F" * 64, 12, None):
            with self.subTest(bad=bad):
                data = self.base_data()
                data["bundles"][0]["seal"] = bad
                self.assert_rejected(data)

        data = self.base_data()
        data["bundles"] = [bundle(f"b{i}", ["a1"], self.atts) for i in range(4097)]
        self.assert_rejected(data)

    def test_attestation_count_boundary(self):
        data = audit_data(
            [snapshot("s1", self.spans)],
            [
                attestation(f"a{i}", "s1", self.spans, source="0" * 64, analysis="0" * 64)
                for i in range(4096)
            ],
        )
        result = audit_trace_attestations(data)
        self.assertEqual(len(result["attestations"]), 4096)
        self.assertTrue(
            all(entry["content_status"] == "mismatch" for entry in result["attestations"])
        )


class DeterminismTest(unittest.TestCase):
    def complex_data(self):
        s1 = rich_spans()
        s2 = [span("solo", trace="tx", service="svc", start="-0.100001", end="0.200000")]
        a1 = attestation("a1", "s1", s1)
        a2 = attestation("a2", "s1", s1, replaces="a1", analysis="0" * 64)
        a3 = attestation("a3", "s1", s1, replaces="a2")
        a4 = attestation("a4", "s2", s2)
        a5 = attestation("a5", "s2", s2, source="1" * 64)
        atts = {item["id"]: item for item in (a1, a2, a3, a4, a5)}
        w1 = withdrawal("w1", "a5", "issued against stale evidence")
        b1 = bundle("b1", ["a3", "a4"], atts)
        b2 = bundle("b2", ["a5"], atts)
        b3 = bundle("b3", ["a1"], atts, ["b1", "b2"], {"b1": b1["seal"], "b2": b2["seal"]})
        return audit_data(
            [snapshot("s1", s1), snapshot("s2", s2)],
            [a1, a2, a3, a4, a5],
            [w1],
            [b1, b2, b3],
        )

    def test_input_reordering_does_not_change_result_and_input_is_untouched(self):
        data = self.complex_data()
        before = copy.deepcopy(data)
        result = audit_trace_attestations(data)
        self.assertEqual(data, before)

        scrambled = scramble(data)
        again = audit_trace_attestations(scrambled)
        self.assertEqual(
            json.dumps(result, sort_keys=True, allow_nan=False),
            json.dumps(again, sort_keys=True, allow_nan=False),
        )
        self.assertEqual(scrambled, scramble(before))

    def test_complex_result_shape_and_contents(self):
        result = audit_trace_attestations(self.complex_data())
        self.assertEqual(set(result), {"schema", "snapshots", "attestations", "bundles"})
        self.assertEqual(result["schema"], "trace-attestation-audit-v1")

        self.assertEqual([entry["id"] for entry in result["snapshots"]], ["s1", "s2"])
        for entry in result["snapshots"]:
            self.assertEqual(set(entry), {"id", "source_digest", "analysis_digest"})

        self.assertEqual(
            [entry["id"] for entry in result["attestations"]],
            ["a1", "a2", "a3", "a4", "a5"],
        )
        for entry in result["attestations"]:
            self.assertEqual(set(entry), {
                "id", "snapshot", "content_status", "mismatches", "withdrawn",
                "withdrawal_id", "superseded_by", "usable",
            })
        attestations = by_id(result["attestations"])
        self.assertEqual(attestations["a1"]["superseded_by"], "a3")
        self.assertEqual(attestations["a2"]["superseded_by"], "a3")
        self.assertEqual(attestations["a2"]["mismatches"], ["analysis_digest"])
        self.assertTrue(attestations["a3"]["usable"])
        self.assertTrue(attestations["a4"]["usable"])
        self.assertEqual(attestations["a5"]["mismatches"], ["source_digest"])
        self.assertTrue(attestations["a5"]["withdrawn"])
        self.assertEqual(attestations["a5"]["withdrawal_id"], "w1")
        self.assertFalse(attestations["a5"]["usable"])

        self.assertEqual([entry["id"] for entry in result["bundles"]], ["b1", "b2", "b3"])
        for entry in result["bundles"]:
            self.assertEqual(set(entry), {"id", "seal", "expected_seal", "status", "issues"})
            for issue in entry["issues"]:
                self.assertEqual(set(issue), {"kind", "ref"})
        bundles = by_id(result["bundles"])
        self.assertEqual(bundles["b1"]["status"], "usable")
        self.assertEqual(bundles["b2"]["status"], "withdrawn")
        self.assertEqual(bundles["b2"]["issues"], [
            {"kind": "attestation_mismatch", "ref": "a5"},
            {"kind": "attestation_withdrawn", "ref": "a5"},
        ])
        self.assertEqual(bundles["b3"]["status"], "withdrawn")
        self.assertEqual(bundles["b3"]["issues"], [
            {"kind": "attestation_superseded", "ref": "a1"},
            {"kind": "dependency_withdrawn", "ref": "b2"},
        ])

        json.dumps(result, allow_nan=False)

    def test_empty_withdrawals_and_bundles_are_allowed(self):
        spans = base_spans()
        result = audit_trace_attestations(
            audit_data([snapshot("s1", spans)], [attestation("a1", "s1", spans)])
        )
        self.assertEqual(result["bundles"], [])
        self.assertTrue(result["attestations"][0]["usable"])


if __name__ == "__main__":
    unittest.main()
