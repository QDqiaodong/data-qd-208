"""Offline data retention and compliant erasure planning.

The planner reasons exclusively about object *lifecycle*: which objects may be
destroyed now, which minimal set must remain, and why each erasure request can
be completed or is blocked.  It never connects to an external platform and
never mutates the input document.

Time follows the repository's bounded exact decimal string contract and every
operation uses :class:`fractions.Fraction`; binary floating point is never
used.  The complete document is validated before any planning takes place;
any violation raises :class:`ValueError` and no partial result is produced.
"""

from fractions import Fraction

from .checks import array, fields, ident, integer, number


SCHEMA = "trace-retention-plan-v1"
_DECIMAL_SCALE = 1_000_000

_TOP_FIELDS = {"now", "objects", "products", "policies", "holds", "requests"}
_OBJECT_FIELDS = (
    "id", "trace", "kind", "created", "expires", "subjects", "depends_on",
)
_PRODUCT_FIELDS = ("id", "roots", "status")
_POLICY_FIELDS = ("id", "trace", "kind", "keep_until", "priority")
_HOLD_FIELDS = ("id", "roots", "mode", "closed")
_REQUEST_FIELDS = ("id", "subject", "submitted", "scope")

_KINDS = ("raw", "derived", "catalog")
_MODES = ("evidence", "rebuild")
_SCOPES = ("direct", "rebuildable")

# Operational bound on simultaneous parallel recovery routes.
_MAX_FLEXIBLE_PRODUCTS = 18
_MAX_FLEXIBLE_ROOTS = 36


def plan_trace_retention(data):
    """Validate ``data`` and return the retention/erasure plan."""
    model = _validate(data)
    return _plan(model)


def _canonical_decimal(value):
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
        raise ValueError(
            "retention data must contain exactly now, objects, products, "
            "policies, holds and requests"
        )

    now = number(data["now"])
    pool = {}
    objects, index_by_id, order_by_id, sorted_nodes = _validate_objects(
        data["objects"], now, pool
    )
    reverse_edges = _build_reverse_edges(objects, index_by_id)
    topo_order = _topological_order(objects, index_by_id)
    products = _validate_products(
        data["products"], index_by_id, pool
    )
    policies = _validate_policies(data["policies"], now, objects, pool)
    holds = _validate_holds(data["holds"], index_by_id, pool)
    requests = _validate_requests(data["requests"], now, pool)

    policy_until = _resolve_policy_terms(objects, policies)
    closure_masks = _closure_masks(objects, index_by_id, topo_order)
    subject_index = _build_subject_index(objects)

    return {
        "now": now,
        "objects": objects,
        "index_by_id": index_by_id,
        "order_by_id": order_by_id,
        "sorted_nodes": sorted_nodes,
        "reverse_edges": reverse_edges,
        "topo_order": topo_order,
        "products": products,
        "policies": policies,
        "holds": holds,
        "requests": requests,
        "policy_until": policy_until,
        "closure_masks": closure_masks,
        "subject_index": subject_index,
    }


def _intern(pool, value):
    """Return one canonical string object per validated identity."""
    canonical = pool.get(value)
    if canonical is None:
        pool[value] = value
        return value
    return canonical


def _validate_objects(raw_objects, now, pool):
    objects = []
    index_by_id = {}
    object_ids = set()

    for raw in array(raw_objects, 1, 100_000):
        fields(raw, _OBJECT_FIELDS)
        object_id = _intern(pool, ident(raw["id"]))
        if object_id in object_ids:
            raise ValueError("duplicate object id")
        object_ids.add(object_id)

        trace = _intern(pool, ident(raw["trace"]))
        kind = raw["kind"]
        if kind not in _KINDS:
            raise ValueError("object kind must be raw, derived or catalog")

        created = number(raw["created"])
        if created > now:
            raise ValueError("object created must not be later than now")

        if raw["expires"] is None:
            expires = None
        else:
            expires = number(raw["expires"])
            if expires < created:
                raise ValueError("object expires must not precede created")

        subjects = []
        subject_set = set()
        for subject in array(raw["subjects"], 0, 64):
            subject_id = _intern(pool, ident(subject))
            if subject_id in subject_set:
                raise ValueError("object subjects must be distinct")
            subject_set.add(subject_id)
            subjects.append(subject_id)

        depends_on = []
        dependency_set = set()
        for dependency in array(raw["depends_on"], 0, 256):
            dependency_id = _intern(pool, ident(dependency))
            if dependency_id == object_id:
                raise ValueError("object must not depend on itself")
            if dependency_id in dependency_set:
                raise ValueError("object dependencies must be distinct")
            dependency_set.add(dependency_id)
            depends_on.append(dependency_id)

        if kind == "raw" and depends_on:
            raise ValueError("raw objects must not depend on other objects")
        if kind == "derived" and not depends_on:
            raise ValueError("derived objects must depend on at least one object")

        objects.append(
            {
                "id": object_id,
                "trace": trace,
                "kind": kind,
                "created": created,
                "expires": expires,
                "subjects": subjects,
                "subject_set": subject_set,
                "depends_on": depends_on,
            }
        )

    # All references are checked only after the complete id table exists so
    # validation never depends on input ordering.
    for obj in objects:
        for dependency_id in obj["depends_on"]:
            if dependency_id not in object_ids:
                raise ValueError("object dependency must reference an existing object")
        index_by_id[obj["id"]] = len(index_by_id)

    order_by_id = sorted(index_by_id)
    sorted_nodes = [index_by_id[object_id] for object_id in order_by_id]
    return objects, index_by_id, order_by_id, sorted_nodes


def _build_reverse_edges(objects, index_by_id):
    reverse_edges = [[] for _ in objects]
    for obj in objects:
        child = index_by_id[obj["id"]]
        for dependency_id in obj["depends_on"]:
            reverse_edges[index_by_id[dependency_id]].append(child)
    return reverse_edges


def _topological_order(objects, index_by_id):
    indegree = [0] * len(objects)
    dependents = [[] for _ in objects]
    for obj in objects:
        child = index_by_id[obj["id"]]
        for dependency_id in obj["depends_on"]:
            parent = index_by_id[dependency_id]
            indegree[child] += 1
            dependents[parent].append(child)

    ready = [node for node in range(len(objects)) if indegree[node] == 0]
    order = []
    while ready:
        node = ready.pop()
        order.append(node)
        for child in dependents[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)

    if len(order) != len(objects):
        raise ValueError("object dependency graph must not contain a cycle")
    return order


def _validate_products(raw_products, index_by_id, pool):
    products = []
    product_ids = set()
    for raw in array(raw_products, 0, 10_000):
        fields(raw, _PRODUCT_FIELDS)
        product_id = _intern(pool, ident(raw["id"]))
        if product_id in product_ids:
            raise ValueError("duplicate product id")
        product_ids.add(product_id)

        status = raw["status"]
        if status not in ("active", "retired"):
            raise ValueError("product status must be active or retired")

        roots = []
        root_set = set()
        for root in array(raw["roots"], 1, 256):
            root_id = _intern(pool, ident(root))
            if root_id not in index_by_id:
                raise ValueError("product root must reference an existing object")
            if root_id in root_set:
                raise ValueError("product roots must be distinct")
            root_set.add(root_id)
            roots.append(root_id)

        products.append(
            {"id": product_id, "roots": roots, "status": status}
        )
    return products


def _validate_policies(raw_policies, now, objects, pool):
    policies = []
    policy_ids = set()
    trace_ids = {obj["trace"] for obj in objects}

    for raw in array(raw_policies, 1, 4096):
        fields(raw, _POLICY_FIELDS)
        policy_id = _intern(pool, ident(raw["id"]))
        if policy_id in policy_ids:
            raise ValueError("duplicate policy id")
        policy_ids.add(policy_id)

        trace = raw["trace"]
        if trace != "*":
            trace = _intern(pool, ident(trace))
        if trace != "*" and trace not in trace_ids:
            raise ValueError("policy trace must be an object trace or *")

        kind = raw["kind"]
        if kind != "*":
            if kind not in _KINDS:
                raise ValueError("policy kind must be an object kind or *")

        keep_until = number(raw["keep_until"])
        if keep_until < now:
            raise ValueError("policy keep_until must not be earlier than now")

        priority = integer(raw["priority"], 0, 1_000_000_000)
        policies.append(
            {
                "id": policy_id,
                "trace": trace,
                "kind": kind,
                "keep_until": keep_until,
                "priority": priority,
            }
        )

    present_pairs = {(obj["trace"], obj["kind"]) for obj in objects}
    present_traces = {pair[0] for pair in present_pairs}
    present_kinds = {pair[1] for pair in present_pairs}

    def shared_object_exists(left, right):
        # Expand each selector over values actually present in the data and
        # look for any (trace, kind) both policies can match.
        traces_left = present_traces if left["trace"] == "*" else {left["trace"]}
        traces_right = present_traces if right["trace"] == "*" else {right["trace"]}
        kinds_left = present_kinds if left["kind"] == "*" else {left["kind"]}
        kinds_right = present_kinds if right["kind"] == "*" else {right["kind"]}
        return any(
            (trace, kind) in present_pairs
            for trace in traces_left & traces_right
            for kind in kinds_left & kinds_right
        )

    # Same-priority policies are mutually exclusive for any object they both
    # match unless their terms are identical.  Grouping by priority keeps the
    # pairwise check independent of object count.
    by_priority = {}
    for policy in policies:
        by_priority.setdefault(policy["priority"], []).append(policy)
    for group in by_priority.values():
        for left_position in range(len(group)):
            left = group[left_position]
            for right in group[left_position + 1:]:
                if (
                    left["keep_until"] != right["keep_until"]
                    and shared_object_exists(left, right)
                ):
                    raise ValueError(
                        "same-priority policies with different keep_until are "
                        "mutually exclusive"
                    )
    return policies


def _validate_holds(raw_holds, index_by_id, pool):
    holds = []
    hold_ids = set()
    for raw in array(raw_holds, 0, 4096):
        fields(raw, _HOLD_FIELDS)
        hold_id = _intern(pool, ident(raw["id"]))
        if hold_id in hold_ids:
            raise ValueError("duplicate hold id")
        hold_ids.add(hold_id)

        mode = raw["mode"]
        if mode not in _MODES:
            raise ValueError("hold mode must be evidence or rebuild")

        if type(raw["closed"]) is not bool:
            raise ValueError("hold closed must be a boolean")

        roots = []
        root_set = set()
        for root in array(raw["roots"], 1, 256):
            root_id = _intern(pool, ident(root))
            if root_id not in index_by_id:
                raise ValueError("hold root must reference an existing object")
            if root_id in root_set:
                raise ValueError("hold roots must be distinct")
            root_set.add(root_id)
            roots.append(root_id)

        holds.append(
            {
                "id": hold_id,
                "roots": roots,
                "mode": mode,
                "closed": raw["closed"],
            }
        )
    return holds


def _validate_requests(raw_requests, now, pool):
    requests = []
    request_ids = set()
    for raw in array(raw_requests, 0, 4096):
        fields(raw, _REQUEST_FIELDS)
        request_id = _intern(pool, ident(raw["id"]))
        if request_id in request_ids:
            raise ValueError("duplicate request id")
        request_ids.add(request_id)

        subject = _intern(pool, ident(raw["subject"]))
        submitted = number(raw["submitted"])
        if submitted > now:
            raise ValueError("request submitted must not be later than now")

        scope = raw["scope"]
        if scope not in _SCOPES:
            raise ValueError("request scope must be direct or rebuildable")

        requests.append(
            {
                "id": request_id,
                "subject": subject,
                "submitted": submitted,
                "scope": scope,
            }
        )
    return requests


def _resolve_policy_terms(objects, policies):
    """Return the exact policy deadline for every object."""
    # Four (trace, kind) buckets per priority make winner lookup constant
    # time per object regardless of the policy list order.
    buckets = {}
    for policy in policies:
        bucket = buckets.setdefault(policy["priority"], {})
        key = (policy["trace"], policy["kind"])
        # Same-priority duplicates with an identical term were accepted by
        # validation; either policy yields the same deadline.
        bucket.setdefault(key, policy)

    priorities = sorted(buckets, reverse=True)
    policy_until = []
    for obj in objects:
        winner = None
        for priority in priorities:
            bucket = buckets[priority]
            for key in (
                (obj["trace"], obj["kind"]),
                (obj["trace"], "*"),
                ("*", obj["kind"]),
                ("*", "*"),
            ):
                candidate = bucket.get(key)
                if candidate is not None:
                    winner = candidate
                    break
            if winner is not None:
                break
        if winner is None:
            raise ValueError("every object must match at least one policy")

        term = winner["keep_until"]
        if obj["expires"] is not None and obj["expires"] > term:
            term = obj["expires"]
        policy_until.append(term)
    return policy_until


def _closure_masks(objects, index_by_id, topo_order):
    """Exact recursive depends_on closure bitmasks, roots included."""
    masks = [0] * len(objects)
    for node in topo_order:
        mask = 1 << node
        for dependency_id in objects[node]["depends_on"]:
            mask |= masks[index_by_id[dependency_id]]
        masks[node] = mask
    return masks


def _close_under_dependencies(mask, closure_masks, topo_order):
    """Add every AND-dependency required to keep a derived/catalog object.

    On an acyclic dependency graph OR-ing the already-computed recursive
    closure mask of each retained node is exactly the downward closure; the
    topological scan only decides which nodes are active.
    """
    for node in reversed(topo_order):
        if mask & (1 << node):
            mask |= closure_masks[node]
    return mask


def _plan(model):
    now = model["now"]
    objects = model["objects"]
    closure_masks = model["closure_masks"]

    frozen_mask, hold_objects, held_by = _freeze_sets(model)
    expired_mask = 0
    for node, deadline in enumerate(model["policy_until"]):
        if now >= deadline:
            expired_mask |= 1 << node
    model["expired_mask"] = expired_mask

    all_bits = (1 << len(objects)) - 1
    # Obligations shared by every request: open holds and objects whose
    # compliance term has not yet arrived, closed under AND-dependencies.
    # Computing this once keeps per-request work proportional to targets and
    # product routes rather than the full object graph.
    base_retain_mask = _close_under_dependencies(
        frozen_mask | (~expired_mask & all_bits),
        model["closure_masks"],
        model["topo_order"],
    )

    active_products = _active_product_membership(model)
    active_products_sorted = [
        product
        for product in sorted(model["products"], key=lambda item: item["id"])
        if product["status"] == "active"
    ]

    request_results = [
        _plan_request(
            request,
            model,
            base_retain_mask,
            held_by,
            active_products_sorted,
        )
        for request in sorted(model["requests"], key=lambda item: item["id"])
    ]

    objects_output = []
    for node in model["sorted_nodes"]:
        object_id = objects[node]["id"]
        obj = objects[node]
        objects_output.append(
            {
                "id": object_id,
                "trace": obj["trace"],
                "kind": obj["kind"],
                "policy_until": _canonical_decimal(model["policy_until"][node]),
                "expired": bool(expired_mask & (1 << node)),
                "held_by": sorted(held_by.get(node, ())),
                "active_products": sorted(active_products.get(node, ())),
            }
        )

    holds_output = []
    for hold in sorted(model["holds"], key=lambda item: item["id"]):
        holds_output.append(
            {
                "id": hold["id"],
                "mode": hold["mode"],
                "closed": hold["closed"],
                "objects": hold_objects.get(hold["id"], []),
            }
        )

    return {
        "schema": SCHEMA,
        "objects": objects_output,
        "holds": holds_output,
        "requests": request_results,
    }


def _freeze_sets(model):
    index_by_id = model["index_by_id"]
    closure_masks = model["closure_masks"]
    held_by = {}
    hold_objects = {}
    frozen_mask = 0

    for hold in model["holds"]:
        if hold["closed"]:
            hold_objects[hold["id"]] = []
            continue

        mask = 0
        for root_id in hold["roots"]:
            root_node = index_by_id[root_id]
            if hold["mode"] == "rebuild":
                mask |= closure_masks[root_node]
            else:
                mask |= 1 << root_node

        hold_objects[hold["id"]] = _decode_mask(
            mask, model["sorted_nodes"], model["objects"]
        )
        frozen_mask |= mask

        nodes = mask
        while nodes:
            bit = nodes & -nodes
            node = bit.bit_length() - 1
            held_by.setdefault(node, []).append(hold["id"])
            nodes -= bit

    return frozen_mask, hold_objects, held_by


def _active_product_membership(model):
    """Map each object node to the active products whose closure contains it."""
    membership = {}
    closure_masks = model["closure_masks"]
    index_by_id = model["index_by_id"]
    for product in model["products"]:
        if product["status"] != "active":
            continue
        mask = 0
        for root_id in product["roots"]:
            mask |= closure_masks[index_by_id[root_id]]
        nodes = mask
        while nodes:
            bit = nodes & -nodes
            node = bit.bit_length() - 1
            membership.setdefault(node, []).append(product["id"])
            nodes -= bit
    return membership


def _build_subject_index(objects):
    """Map each subject id to the nodes carrying it directly."""
    index = {}
    for node, obj in enumerate(objects):
        for subject in obj["subject_set"]:
            index.setdefault(subject, []).append(node)
    return index


def _request_targets(request, model):
    """Canonical target set: direct holders, plus ancestors when rebuildable."""
    direct_nodes = model["subject_index"].get(request["subject"], [])
    if not direct_nodes:
        return set()
    if request["scope"] == "direct":
        return set(direct_nodes)

    # Reverse reachability: every retained ancestor of a directly affected
    # object could rebuild the subject's evidence and must also be erased.
    targets = set(direct_nodes)
    stack = list(direct_nodes)
    reverse_edges = model["reverse_edges"]
    while stack:
        node = stack.pop()
        for dependent in reverse_edges[node]:
            if dependent not in targets:
                targets.add(dependent)
                stack.append(dependent)
    return targets


def _plan_request(
    request, model, base_retain_mask, held_by, active_products
):
    index_by_id = model["index_by_id"]
    sorted_nodes = model["sorted_nodes"]
    closure_masks = model["closure_masks"]
    objects = model["objects"]
    targets = _request_targets(request, model)
    target_mask = _nodes_to_mask(targets)

    blocked_holds = set()
    blocked_policy = set()
    nodes = target_mask
    while nodes:
        bit = nodes & -nodes
        node = bit.bit_length() - 1
        # A frozen object cannot be destroyed regardless of its term.
        blocked_holds.update(held_by.get(node, ()))
        # A strictly unexpired policy deadline blocks even when a hold does.
        if not (model["expired_mask"] & bit):
            blocked_policy.add(node)
        nodes -= bit

    blockers = [
        {"kind": "hold", "ref": hold_id}
        for hold_id in sorted(blocked_holds)
    ]
    blockers.extend(
        {"kind": "policy", "ref": objects[node]["id"]}
        for node in sorted_nodes
        if node in blocked_policy
    )

    candidates_by_product = []
    blocked_products = []
    flexible_count = 0
    flexible_roots = 0
    for product in active_products:
        eligible = []
        for root_id in product["roots"]:
            root_node = index_by_id[root_id]
            if not (closure_masks[root_node] & target_mask):
                eligible.append(root_id)
        if not eligible:
            blocked_products.append(product["id"])
        else:
            candidates_by_product.append((product["id"], eligible))
            if len(eligible) >= 2:
                flexible_count += 1
                flexible_roots += len(eligible)

    # The operational parallel-route bound is part of the validated plan:
    # exceeding it for any request rejects the whole document rather than
    # degrading to an approximate plan.
    if flexible_count > _MAX_FLEXIBLE_PRODUCTS or flexible_roots > _MAX_FLEXIBLE_ROOTS:
        raise ValueError(
            "too many active products retain parallel clean recovery routes"
        )

    blockers.extend(
        {"kind": "product", "ref": product_id}
        for product_id in blocked_products
    )

    target_ids = _decode_mask(target_mask, sorted_nodes, objects)

    if blockers:
        blockers.sort(key=lambda item: (item["kind"], item["ref"]))
        return {
            "id": request["id"],
            "subject": request["subject"],
            "scope": request["scope"],
            "status": "blocked",
            "targets": target_ids,
            "delete": [],
            "blockers": blockers,
            "selections": [],
        }

    selections, route_mask = _choose_routes(
        candidates_by_product, index_by_id, closure_masks
    )

    # The base retain mask already covers open holds, unexpired compliance
    # terms and their AND-dependencies for every request; only the selected
    # product routes add request-specific obligations.
    all_bits = (1 << len(objects)) - 1
    retain_mask = base_retain_mask | _close_under_dependencies(
        route_mask, model["closure_masks"], model["topo_order"]
    )

    # Targets are erased whenever no obligation retains them; anything else
    # that loses every consumer and is past its term is cascade-collected.
    delete_mask = ~retain_mask & all_bits

    return {
        "id": request["id"],
        "subject": request["subject"],
        "scope": request["scope"],
        "status": "completed",
        "targets": target_ids,
        "delete": _decode_mask(delete_mask, sorted_nodes, objects),
        "blockers": [],
        "selections": [
            {"product": product_id, "root": root_id}
            for product_id, root_id in selections
        ],
    }


def _choose_routes(candidates_by_product, index_by_id, closure_masks):
    """Globally minimize retained union size, then the lex selection list."""
    # Every unblocked product must select exactly one root even when it has
    # no alternative; the parallel-route bound keeps the remaining
    # enumeration tiny.
    forced = []
    forced_mask = 0
    flexible = []
    for product_id, eligible in candidates_by_product:
        if len(eligible) == 1:
            root_id = eligible[0]
            forced.append((product_id, root_id))
            forced_mask |= closure_masks[index_by_id[root_id]]
        else:
            flexible.append((product_id, sorted(eligible)))

    best = None  # (score, sorted (product, root) list, retain mask)

    def search(position, mask, chosen):
        nonlocal best
        if position == len(flexible):
            ordered = sorted(forced + chosen)
            # Union size first; the root count is the fixed product count and
            # only the deterministic lex tie-break remains.
            score = (mask.bit_count(), tuple(ordered))
            if best is None or score < best[0]:
                best = (score, ordered, mask)
            return
        product_id, roots = flexible[position]
        for root_id in roots:
            chosen.append((product_id, root_id))
            search(
                position + 1,
                mask | closure_masks[index_by_id[root_id]],
                chosen,
            )
            chosen.pop()

    search(0, forced_mask, [])
    _score, ordered, retain_mask = best
    return ordered, retain_mask


def _nodes_to_mask(nodes):
    mask = 0
    for node in nodes:
        mask |= 1 << node
    return mask


def _decode_mask(mask, sorted_nodes, objects):
    """Return ids inside ``mask`` sorted by object id.

    The returned lists reference the canonical id strings from the validated
    model rather than copying them, so repeated mentions across requests do
    not multiply string memory.  The strings themselves are immutable and the
    caller only reads them.
    """
    # Dense masks are cheaper to emit via one ordered scan; sparse masks are
    # cheaper via bit extraction followed by a small sort.
    if mask.bit_count() * 2 >= len(sorted_nodes):
        return [
            objects[node]["id"]
            for node in sorted_nodes
            if mask & (1 << node)
        ]
    ids = []
    remaining = mask
    while remaining:
        bit = remaining & -remaining
        ids.append(objects[bit.bit_length() - 1]["id"])
        remaining -= bit
    ids.sort()
    return ids
