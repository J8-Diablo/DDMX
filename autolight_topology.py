#!/usr/bin/env python3
"""Spatial topology for AutoLight.

Given the engine's registered devices (each with optional ``x``/``y`` world
coordinates and pre-computed ``capabilities``), compute once per rig change:

* mirror pairs across the rig's vertical center line
* coarse spatial clusters (x- and y-tertiles)
* a deterministic x-ordered index sequence for chaser effects

The result is an immutable-ish snapshot the renderer reads per frame without
recomputing anything. No numpy; math is trivial at the rig sizes we see.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


log = logging.getLogger(__name__)


@dataclass
class FixtureTopo:
    device_id: str
    x: Optional[float]
    y: Optional[float]
    mirror_pair_id: Optional[int] = None
    mirror_side: Optional[str] = None  # "left" | "right" | None
    cluster_x: int = 1   # 0 left / 1 center / 2 right
    cluster_y: int = 1   # 0 top  / 1 middle / 2 bottom
    order_index: int = 0


@dataclass
class TopologySnapshot:
    fixtures: Dict[str, FixtureTopo] = field(default_factory=dict)
    order_by_x: List[str] = field(default_factory=list)
    mirror_pair_count: int = 0
    center_x: Optional[float] = None
    cluster_counts_x: Tuple[int, int, int] = (0, 0, 0)
    cluster_counts_y: Tuple[int, int, int] = (0, 0, 0)
    has_positions: bool = False

    def cluster_summary(self) -> str:
        cx = self.cluster_counts_x
        return f"L{cx[0]}·C{cx[1]}·R{cx[2]}"


def compute_topology(devices: Dict[str, Any]) -> TopologySnapshot:
    """Compute mirror pairs + spatial clusters for a set of registered devices.

    ``devices`` is ``engine._devices`` — a mapping of device_id to a
    DeviceState-like object with ``.x`` and ``.y`` attributes (possibly None).
    Missing coordinates degrade gracefully to a flat "everyone is center"
    layout.
    """
    snap = TopologySnapshot()
    if not devices:
        return snap

    # Collect fixtures with valid coordinates.
    coord_ids: List[str] = []
    xs: List[float] = []
    ys: List[float] = []
    for dev_id, dev in devices.items():
        snap.fixtures[str(dev_id)] = FixtureTopo(
            device_id=str(dev_id),
            x=getattr(dev, "x", None),
            y=getattr(dev, "y", None),
        )
        x = getattr(dev, "x", None)
        y = getattr(dev, "y", None)
        if x is not None and y is not None:
            coord_ids.append(str(dev_id))
            xs.append(float(x))
            ys.append(float(y))

    snap.has_positions = len(coord_ids) >= 3

    if not snap.has_positions:
        order = sorted(snap.fixtures.keys())
        for i, dev_id in enumerate(order):
            snap.fixtures[dev_id].order_index = i
        snap.order_by_x = order
        return snap

    # Midpoint of the extremes is more robust than the median for
    # asymmetric rigs: the "axis of reflection" sits between the leftmost
    # and rightmost fixture, whereas the median drifts when one side has
    # more fixtures.
    snap.center_x = (float(min(xs)) + float(max(xs))) / 2.0
    _assign_clusters(snap, coord_ids, xs, ys)
    _assign_mirror_pairs(snap, coord_ids, xs, ys)
    _assign_chaser_order(snap, coord_ids, xs)
    snap.cluster_counts_x = _count_buckets([snap.fixtures[i].cluster_x for i in coord_ids], 3)
    snap.cluster_counts_y = _count_buckets([snap.fixtures[i].cluster_y for i in coord_ids], 3)
    return snap


def _assign_clusters(snap: TopologySnapshot, ids: List[str], xs: List[float], ys: List[float]) -> None:
    # x-tertiles: sort, split at 1/3 and 2/3.
    for axis_values, is_x in ((xs, True), (ys, False)):
        sorted_vals = sorted(axis_values)
        n = len(sorted_vals)
        if n == 0:
            continue
        low = sorted_vals[max(0, n // 3 - 1)] if n >= 3 else sorted_vals[0]
        high = sorted_vals[min(n - 1, (2 * n) // 3)] if n >= 3 else sorted_vals[-1]
        for dev_id, v in zip(ids, axis_values):
            cluster = 1
            if v <= low:
                cluster = 0
            elif v >= high:
                cluster = 2
            topo = snap.fixtures[dev_id]
            if is_x:
                topo.cluster_x = cluster
            else:
                topo.cluster_y = cluster


def _assign_mirror_pairs(snap: TopologySnapshot, ids: List[str], xs: List[float], ys: List[float]) -> None:
    """Greedy pairing: for each left-side device, pick best unmatched right-side partner."""
    cx = snap.center_x or 0.0
    span = (max(xs) - min(xs)) if len(xs) > 1 else 0.0
    if span <= 0:
        return
    tol = max(200.0, span * 0.15)  # 15 % of x-span, floor at 200 world units

    left: List[Tuple[str, float, float]] = []   # (id, x, y)
    right: List[Tuple[str, float, float]] = []
    for dev_id, x, y in zip(ids, xs, ys):
        if x < cx - 1.0:
            left.append((dev_id, x, y))
        elif x > cx + 1.0:
            right.append((dev_id, x, y))

    # Pair greedily: iterate left sorted by distance-from-center descending.
    # The farthest fixtures have the least ambiguous partner, so lock them
    # first — otherwise a center-ish fixture can greedy-grab a far partner
    # and leave the actually-far fixture orphaned on an asymmetric rig.
    left.sort(key=lambda t: abs(t[1] - cx), reverse=True)
    used_right: set = set()
    pair_id = 0
    for (li, lx, ly) in left:
        best_j = -1
        best_cost = float("inf")
        for j, (ri, rx, ry) in enumerate(right):
            if j in used_right:
                continue
            cost = 0.7 * abs(abs(lx - cx) - abs(rx - cx)) + 0.3 * abs(ly - ry)
            if cost < best_cost:
                best_cost = cost
                best_j = j
        if best_j >= 0 and best_cost <= tol:
            pair_id += 1
            ri = right[best_j][0]
            used_right.add(best_j)
            snap.fixtures[li].mirror_pair_id = pair_id
            snap.fixtures[li].mirror_side = "left"
            snap.fixtures[ri].mirror_pair_id = pair_id
            snap.fixtures[ri].mirror_side = "right"

    snap.mirror_pair_count = pair_id


def _assign_chaser_order(snap: TopologySnapshot, ids: List[str], xs: List[float]) -> None:
    """Left-to-right then top-to-bottom (by y)."""
    decorated = sorted(
        ((x, snap.fixtures[d].y if snap.fixtures[d].y is not None else 0.0, d) for d, x in zip(ids, xs)),
        key=lambda t: (t[0], t[1]),
    )
    ordered = [t[2] for t in decorated]
    # Append fixtures without coords at the end, stable by device_id.
    leftover = sorted(dev_id for dev_id in snap.fixtures if dev_id not in set(ordered))
    ordered.extend(leftover)
    for i, dev_id in enumerate(ordered):
        snap.fixtures[dev_id].order_index = i
    snap.order_by_x = ordered


def _count_buckets(values: List[int], n: int) -> Tuple[int, ...]:
    out = [0] * n
    for v in values:
        if 0 <= v < n:
            out[v] += 1
    return tuple(out)
