"""Per-viewer page layout: how many slots each rail gets and where discovery lands."""
from __future__ import annotations
import logging


from .constants import ACTIVITY_COLD_START_EVENTS, DISCOVERY_HIGH_THRESHOLD, DISCOVERY_LOW_THRESHOLD, PAGE_SIZE, SLOT_LAYOUT, _DISCOVERY_ORDER_COLD, _DISCOVERY_ORDER_MATURE

logger = logging.getLogger(__name__)

# Discovery budget = number of non-followed slots, set from follow count.
# Few follows → the followed feed runs dry, so lean into discovery; many
# follows → there's plenty of followed content, so dial discovery back. (Yes,
# this is the right direction: someone following 2,000 accounts is drowning in
# followed posts and wants fewer interruptions, per ACTIVITY_AND_FEED_AUDIT.md
# item B5.) Budget is clamped to [2, 6] so followed stays in [4, 8] — every
# viewer keeps a real followed feed AND at least a little discovery.
def _discovery_budget(follow_count, discovery_appetite):
    if follow_count < 20:
        budget = 6
    elif follow_count < 100:
        budget = 5
    elif follow_count < 500:
        budget = 4
    elif follow_count < 2000:
        budget = 3
    else:
        budget = 2
    # Tab-usage appetite nudges ±1 within the band (B1 signal, folded in here).
    if discovery_appetite is not None:
        if discovery_appetite >= DISCOVERY_HIGH_THRESHOLD:
            budget += 1
        elif discovery_appetite < DISCOVERY_LOW_THRESHOLD:
            budget -= 1
    return max(2, min(budget, 6))



def _slot_allocation(follow_count, n_events, discovery_appetite):
    """
    Return {rail: slot_count} summing to PAGE_SIZE, personalised per viewer.
    """
    budget = _discovery_budget(follow_count, discovery_appetite)
    counts = {
        "followed": PAGE_SIZE - budget,
        "friend_network": 0, "nearby": 0, "activity": 0, "collaborative": 0,
    }
    mature = n_events >= ACTIVITY_COLD_START_EVENTS
    order = _DISCOVERY_ORDER_MATURE if mature else _DISCOVERY_ORDER_COLD
    for i in range(budget):
        counts[order[i]] += 1
    return counts



def _generate_layout(counts):
    """
    Turn a {rail: count} allocation into a position layout of length PAGE_SIZE.

    Guarantees: slot 0 is always 'followed' (discovery never leads the page),
    and discovery slots are spread as evenly as possible across positions
    1..PAGE_SIZE-1. Discovery labels are emitted round-robin across rail types
    so that, where two discovery slots do land adjacent, they're different
    rails rather than two of the same.
    """
    # Round-robin the discovery labels for type variety.
    remaining = {
        r: counts.get(r, 0)
        for r in ("activity", "collaborative", "friend_network", "nearby")
        if counts.get(r, 0) > 0
    }
    discovery = []
    types = list(remaining.keys())
    while remaining:
        for r in types:
            if remaining.get(r, 0) > 0:
                discovery.append(r)
                remaining[r] -= 1
                if remaining[r] == 0:
                    del remaining[r]

    layout = ["followed"] * PAGE_SIZE
    d = len(discovery)
    if d:
        # Evenly-spaced target positions, then snap each to the nearest still-
        # free slot (never position 0). No loops, fully deterministic.
        avail = list(range(1, PAGE_SIZE))
        chosen = []
        for i in range(d):
            target = int(round((i + 1) * PAGE_SIZE / (d + 1)))
            target = min(max(target, 1), PAGE_SIZE - 1)
            best = min(avail, key=lambda p: (abs(p - target), p))
            avail.remove(best)
            chosen.append(best)
        for label, pos in zip(discovery, sorted(chosen)):
            layout[pos] = label
    return layout



def compute_slot_layout(follow_count, n_events, discovery_appetite):
    """
    The per-viewer page layout (B5). Combines follow count (followed↔discovery
    balance), taste-profile maturity (which discovery rails get the slots), and
    discovery appetite (a ±1 nudge). Falls back to the curated SLOT_LAYOUT on
    any unexpected input so the feed can never be left without a layout.
    """
    try:
        counts = _slot_allocation(follow_count, n_events, discovery_appetite)
        layout = _generate_layout(counts)
        if len(layout) == PAGE_SIZE and layout[0] == "followed":
            return layout
    except Exception as exc:   # pragma: no cover — defensive
        logger.warning(f"[compute_slot_layout] fell back to default: {exc}")
    return SLOT_LAYOUT
