"""Audit trace-snapshot attestations and propagate lifecycle state to bundles.

The auditor checks whether analysis attestations issued over trace snapshots
still match the underlying evidence, and propagates correction (``replaces``)
and withdrawal state into the evidence bundles that reference those
attestations.  It delivers a traceable current-trust judgement only: it never
recompiles captured events, never selects archive candidates, and never
rewrites frozen bundle seals.

Every digest is SHA-256 over canonical JSON (UTF-8, sorted object keys, no
insignificant whitespace).  Decimal strings are hashed verbatim and never
converted to binary floating point.  The complete input is validated before
any auditing takes place; any violation raises :class:`ValueError` and no
partial result is produced.  The input is never mutated.
"""

import hashlib
import json
import re

from .checks import array, fields, ident
from .service import process


SCHEMA = "trace-attestation-audit-v1"

_TOP_FIELDS = {"snapshots", "attestations", "withdrawals", "bundles"}
_SNAPSHOT_FIELDS = ("id", "spans")
_ATTESTATION_FIELDS = ("id", "snapshot", "source_digest", "analysis_digest", "replaces")
_WITHDRAWAL_FIELDS = ("id", "attestation", "reason")
_BUNDLE_FIELDS = ("id", "attestations", "depends_on", "seal")
_SPAN_FIELDS = ("id", "trace", "parent", "service", "start", "end")

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_MAX_TOTAL_SPANS = 100_000


def audit_trace_attestations(data):
    """Validate the complete audit input and return the current trust state."""
    return _audit(_validate(data))


def _validate(data):
    if not isinstance(data, dict) or set(data) != _TOP_FIELDS:
        raise ValueError(
            "audit input must contain exactly snapshots, attestations, "
            "withdrawals and bundles"
        )
    snapshots = _validate_snapshots(data["snapshots"])
    attestations = _validate_attestations(data["attestations"], snapshots)
    withdrawals = _validate_withdrawals(data["withdrawals"], attestations)
    bundles, bundle_order = _validate_bundles(data["bundles"], attestations)
    return {
        "snapshots": snapshots,
        "attestations": attestations,
        "withdrawals": withdrawals,
        "bundles": bundles,
        "bundle_order": bundle_order,
    }


def _validate_snapshots(raw_snapshots):
    snapshots = {}
    total_spans = 0
    for raw in array(raw_snapshots, 1, 256):
        fields(raw, _SNAPSHOT_FIELDS)
        snapshot_id = ident(raw["id"])
        if snapshot_id in snapshots:
            raise ValueError("duplicate snapshot id")
        # The unchanged processing API applies the complete span input rules;
        # a bad parent, cross-trace parent, cycle or illegal time fails the
        # whole batch instead of marking one snapshot untrusted.
        analysis = process({"spans": raw["spans"]})
        total_spans += len(raw["spans"])
        if total_spans > _MAX_TOTAL_SPANS:
            raise ValueError("total span count exceeds 100000")
        snapshots[snapshot_id] = {
            "id": snapshot_id,
            "spans": raw["spans"],
            "analysis": analysis,
        }
    return snapshots


def _validate_attestations(raw_attestations, snapshots):
    attestations = {}
    for raw in array(raw_attestations, 1, 4096):
        fields(raw, _ATTESTATION_FIELDS)
        attestation_id = ident(raw["id"])
        if attestation_id in attestations:
            raise ValueError("duplicate attestation id")
        snapshot_id = ident(raw["snapshot"])
        if snapshot_id not in snapshots:
            raise ValueError("attestation snapshot must exist")
        replaces = raw["replaces"]
        if replaces is not None:
            replaces = ident(replaces)
        attestations[attestation_id] = {
            "id": attestation_id,
            "snapshot": snapshot_id,
            "source_digest": _hex_digest(raw["source_digest"]),
            "analysis_digest": _hex_digest(raw["analysis_digest"]),
            "replaces": replaces,
        }

    replaced = set()
    for attestation in attestations.values():
        target = attestation["replaces"]
        if target is None:
            continue
        if target not in attestations:
            raise ValueError("replaces must reference an existing attestation")
        if attestations[target]["snapshot"] != attestation["snapshot"]:
            raise ValueError("replaces must stay within the same snapshot")
        if target in replaced:
            raise ValueError("attestation must not have two direct successors")
        replaced.add(target)

    # Each attestation replaces at most one other, so the relation is a set
    # of disjoint chains once forks are rejected; only cycles remain possible.
    checked = set()
    for attestation_id in attestations:
        path = set()
        current = attestation_id
        while current is not None and current not in checked:
            if current in path:
                raise ValueError("replaces relation must not contain a cycle")
            path.add(current)
            current = attestations[current]["replaces"]
        checked.update(path)
    return attestations


def _validate_withdrawals(raw_withdrawals, attestations):
    withdrawals = {}
    withdrawn_targets = set()
    for raw in array(raw_withdrawals, 0, 4096):
        fields(raw, _WITHDRAWAL_FIELDS)
        withdrawal_id = ident(raw["id"])
        if withdrawal_id in withdrawals:
            raise ValueError("duplicate withdrawal id")
        target = ident(raw["attestation"])
        if target not in attestations:
            raise ValueError("withdrawal attestation must exist")
        if target in withdrawn_targets:
            raise ValueError("attestation must not be withdrawn twice")
        reason = raw["reason"]
        if (
            not isinstance(reason, str)
            or not 1 <= len(reason) <= 256
            or reason.strip() != reason
        ):
            raise ValueError(
                "withdrawal reason must be 1..256 characters without "
                "leading or trailing whitespace"
            )
        withdrawn_targets.add(target)
        withdrawals[withdrawal_id] = {
            "id": withdrawal_id,
            "attestation": target,
            "reason": reason,
        }
    return withdrawals


def _validate_bundles(raw_bundles, attestations):
    bundles = {}
    for raw in array(raw_bundles, 0, 4096):
        fields(raw, _BUNDLE_FIELDS)
        bundle_id = ident(raw["id"])
        if bundle_id in bundles:
            raise ValueError("duplicate bundle id")
        attestation_ids = [ident(value) for value in array(raw["attestations"], 1, 256)]
        if len(set(attestation_ids)) != len(attestation_ids):
            raise ValueError("duplicate attestation reference in bundle")
        depends_on = [ident(value) for value in array(raw["depends_on"], 0, 256)]
        if len(set(depends_on)) != len(depends_on):
            raise ValueError("duplicate dependency reference in bundle")
        bundles[bundle_id] = {
            "id": bundle_id,
            "attestations": attestation_ids,
            "depends_on": depends_on,
            "seal": _hex_digest(raw["seal"]),
        }

    for bundle in bundles.values():
        for attestation_id in bundle["attestations"]:
            if attestation_id not in attestations:
                raise ValueError("bundle attestation must exist")
        for dependency in bundle["depends_on"]:
            if dependency not in bundles:
                raise ValueError("bundle dependency must exist")
            if dependency == bundle["id"]:
                raise ValueError("bundle must not depend on itself")

    # Kahn topological order; a non-empty residue means a dependency cycle.
    indegree = {bundle_id: 0 for bundle_id in bundles}
    dependents = {bundle_id: [] for bundle_id in bundles}
    for bundle in bundles.values():
        for dependency in bundle["depends_on"]:
            dependents[dependency].append(bundle["id"])
            indegree[bundle["id"]] += 1
    ready = sorted(bundle_id for bundle_id, degree in indegree.items() if degree == 0)
    order = []
    while ready:
        bundle_id = ready.pop()
        order.append(bundle_id)
        for dependent in dependents[bundle_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if len(order) != len(bundles):
        raise ValueError("bundle dependencies must not contain a cycle")
    return bundles, order


def _audit(model):
    snapshots = model["snapshots"]
    attestations = model["attestations"]
    withdrawals = model["withdrawals"]
    bundles = model["bundles"]

    actual_digests = {}
    snapshot_output = []
    for snapshot_id in sorted(snapshots):
        snapshot = snapshots[snapshot_id]
        ordered_spans = sorted(snapshot["spans"], key=lambda span: span["id"])
        source_document = {
            "spans": [
                {field: span[field] for field in _SPAN_FIELDS}
                for span in ordered_spans
            ]
        }
        source_digest = _digest_canonical(source_document)
        analysis_digest = _digest_canonical(snapshot["analysis"])
        actual_digests[snapshot_id] = (source_digest, analysis_digest)
        snapshot_output.append({
            "id": snapshot_id,
            "source_digest": source_digest,
            "analysis_digest": analysis_digest,
        })

    content_status = {}
    mismatches = {}
    for attestation_id, attestation in attestations.items():
        actual_source, actual_analysis = actual_digests[attestation["snapshot"]]
        differing = []
        if attestation["source_digest"] != actual_source:
            differing.append("source_digest")
        if attestation["analysis_digest"] != actual_analysis:
            differing.append("analysis_digest")
        mismatches[attestation_id] = sorted(differing)
        content_status[attestation_id] = "mismatch" if differing else "verified"

    superseded_by = _superseded_by(attestations, content_status)

    withdrawal_by_attestation = {}
    for withdrawal in withdrawals.values():
        withdrawal_by_attestation[withdrawal["attestation"]] = withdrawal["id"]

    attestation_output = []
    for attestation_id in sorted(attestations):
        attestation = attestations[attestation_id]
        withdrawn = attestation_id in withdrawal_by_attestation
        successor = superseded_by[attestation_id]
        usable = (
            content_status[attestation_id] == "verified"
            and not withdrawn
            and successor is None
        )
        attestation_output.append({
            "id": attestation_id,
            "snapshot": attestation["snapshot"],
            "content_status": content_status[attestation_id],
            "mismatches": mismatches[attestation_id],
            "withdrawn": withdrawn,
            "withdrawal_id": withdrawal_by_attestation.get(attestation_id),
            "superseded_by": successor,
            "usable": usable,
        })

    expected_seals = {}
    bundle_status = {}
    bundle_issues = {}
    for bundle_id in model["bundle_order"]:
        bundle = bundles[bundle_id]
        # The seal freezes the evidence identities referenced at issuance:
        # declared attestation digests and recursively computed dependency
        # seals, never a child's self-reported seal and never current state.
        seal_document = {
            "id": bundle_id,
            "attestations": [
                {
                    "id": attestation_id,
                    "source_digest": attestations[attestation_id]["source_digest"],
                    "analysis_digest": attestations[attestation_id]["analysis_digest"],
                }
                for attestation_id in sorted(bundle["attestations"])
            ],
            "depends_on": [
                {"id": dependency, "expected_seal": expected_seals[dependency]}
                for dependency in sorted(bundle["depends_on"])
            ],
        }
        expected_seals[bundle_id] = _digest_canonical(seal_document)

        issues = set()
        direct_withdrawn = False
        direct_superseded = False
        direct_mismatch = False
        for attestation_id in bundle["attestations"]:
            if attestation_id in withdrawal_by_attestation:
                direct_withdrawn = True
                issues.add(("attestation_withdrawn", attestation_id))
            if superseded_by[attestation_id] is not None:
                direct_superseded = True
                issues.add(("attestation_superseded", attestation_id))
            if content_status[attestation_id] == "mismatch":
                direct_mismatch = True
                issues.add(("attestation_mismatch", attestation_id))

        dependency_withdrawn = False
        dependency_superseded = False
        dependency_unreliable = False
        for dependency in bundle["depends_on"]:
            status = bundle_status[dependency]
            if status == "withdrawn":
                dependency_withdrawn = True
            elif status == "superseded":
                dependency_superseded = True
            elif status in ("invalid_seal", "blocked"):
                dependency_unreliable = True
            if status != "usable":
                issues.add(("dependency_" + status, dependency))

        if bundle["seal"] != expected_seals[bundle_id]:
            status = "invalid_seal"
        elif direct_withdrawn or dependency_withdrawn:
            status = "withdrawn"
        elif direct_superseded or dependency_superseded:
            status = "superseded"
        elif direct_mismatch or dependency_unreliable:
            status = "blocked"
        else:
            status = "usable"
        bundle_status[bundle_id] = status
        bundle_issues[bundle_id] = [
            {"kind": kind, "ref": ref} for kind, ref in sorted(issues)
        ]

    bundle_output = []
    for bundle_id in sorted(bundles):
        bundle_output.append({
            "id": bundle_id,
            "seal": bundles[bundle_id]["seal"],
            "expected_seal": expected_seals[bundle_id],
            "status": bundle_status[bundle_id],
            "issues": bundle_issues[bundle_id],
        })

    return {
        "schema": SCHEMA,
        "snapshots": snapshot_output,
        "attestations": attestation_output,
        "bundles": bundle_output,
    }


def _superseded_by(attestations, content_status):
    successor = {}
    for attestation in attestations.values():
        if attestation["replaces"] is not None:
            successor[attestation["replaces"]] = attestation["id"]

    # Chains are disjoint because each attestation has at most one successor.
    # Walk every unvisited chain once and backfill from its tip so each entry
    # is the nearest verified successor; input order and id order never
    # decide seniority, and withdrawal never revives an ancestor.
    nearest = {}
    for attestation_id in attestations:
        if attestation_id in nearest:
            continue
        chain = []
        current = attestation_id
        while current is not None and current not in nearest:
            chain.append(current)
            current = successor.get(current)
        if current is None:
            carry = None
        else:
            carry = current if content_status[current] == "verified" else nearest[current]
        for node in reversed(chain):
            nearest[node] = carry
            if content_status[node] == "verified":
                carry = node
    return nearest


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest_canonical(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _hex_digest(value):
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError("digest must be a 64 character lowercase hex string")
    return value
