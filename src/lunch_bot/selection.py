"""Diversity + vote-weighted restaurant selection.

This module is **pure logic**: it operates on plain ``Restaurant`` objects and a
caller-supplied ``random.Random`` instance. It performs no I/O (no DB, no
network), which makes it fully deterministic and unit-testable.

The goal of :func:`select_candidates` is to pick ``n`` restaurants that are

1. **diverse** across cuisines, and
2. **weighted by past votes** so that restaurants which repeatedly get no votes
   fade out, while restaurants that have never been offered are still explored.

The scoring uses a UCB-style exploration/exploitation blend. See
:func:`score_restaurant` for the exact formula.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Optional

from .models import Restaurant

# Default exploration weight. Larger values push more exploration of
# rarely/never-offered restaurants relative to their observed vote average.
DEFAULT_EXPLORATION_C = 1.0

# Optimistic prior average-vote value assigned to restaurants that have never
# been offered (``times_selected == 0``). This must be high enough that a
# brand-new restaurant is preferred over one that has been offered and got zero
# votes, so that new options get explored rather than being penalised like a
# known-bad option.
DEFAULT_OPTIMISTIC_PRIOR = 1.0


def score_restaurant(
    restaurant: Restaurant,
    *,
    exploration_c: float = DEFAULT_EXPLORATION_C,
    optimistic_prior: float = DEFAULT_OPTIMISTIC_PRIOR,
) -> float:
    """Return a positive sampling weight for ``restaurant``.

    The score blends exploitation (observed average votes) with exploration
    (a bonus that decays as a restaurant is offered more often):

    - ``avg_votes = total_votes / times_selected`` when ``times_selected > 0``.
    - A restaurant that has never been offered (``times_selected == 0``) gets an
      ``optimistic_prior`` instead of ``0``, so it is *not* penalised the same
      as a restaurant that has been offered but earned zero votes.
    - ``exploration_bonus = exploration_c / sqrt(times_selected + 1)``.
    - ``score = avg_or_prior + exploration_bonus``.

    The returned value is always strictly positive so it can be used directly as
    a sampling weight.
    """
    if restaurant.times_selected > 0:
        avg_or_prior = restaurant.total_votes / restaurant.times_selected
    else:
        avg_or_prior = optimistic_prior

    exploration_bonus = exploration_c / math.sqrt(restaurant.times_selected + 1)
    score = avg_or_prior + exploration_bonus
    # Guard against a degenerate zero/negative weight (shouldn't happen given the
    # bonus is always > 0, but keeps sampling robust to weird inputs).
    return max(score, 1e-9)


def _weighted_sample_without_replacement(
    items: list,
    weights: list[float],
    k: int,
    rng: random.Random,
) -> list:
    """Sample ``k`` distinct items proportional to ``weights`` (no replacement).

    Implemented as ``k`` successive weighted draws, removing the chosen item each
    time. ``k`` is clamped to ``len(items)``.
    """
    pool = list(items)
    pool_weights = list(weights)
    chosen = []
    k = min(k, len(pool))
    for _ in range(k):
        total = sum(pool_weights)
        if total <= 0:
            # All remaining weights are zero; fall back to a uniform pick.
            idx = rng.randrange(len(pool))
        else:
            target = rng.random() * total
            cumulative = 0.0
            idx = len(pool) - 1
            for i, w in enumerate(pool_weights):
                cumulative += w
                if target <= cumulative:
                    idx = i
                    break
        chosen.append(pool.pop(idx))
        pool_weights.pop(idx)
    return chosen


def select_candidates(
    restaurants: list[Restaurant],
    n: int = 5,
    *,
    rng: Optional[random.Random] = None,
    exploration_c: float = DEFAULT_EXPLORATION_C,
    optimistic_prior: float = DEFAULT_OPTIMISTIC_PRIOR,
) -> list[Restaurant]:
    """Pick ``n`` diverse, vote-weighted restaurants from the pool.

    Algorithm:

    1. Consider only ``active`` restaurants and compute each one's score.
    2. Group restaurants by cuisine (a ``None`` cuisine is treated as its own
       ``"__unknown__"`` bucket). Each cuisine's aggregate weight is the sum of
       its restaurants' scores.
    3. Iteratively pick distinct cuisines weighted by aggregate score, and within
       each chosen cuisine sample one restaurant weighted by score (without
       replacement), until ``n`` are chosen.
    4. If distinct cuisines run out before ``n`` picks, continue sampling from
       the remaining restaurants by score (again without replacement).

    Args:
        restaurants: The candidate pool (typically all active restaurants).
        n: How many candidates to return (caller should keep this in 4..6).
        rng: A seedable ``random.Random`` for deterministic behaviour. A fresh
            ``random.Random()`` is created if omitted.
        exploration_c: Exploration weight ``C`` in the UCB-style bonus.
        optimistic_prior: Average-vote value assigned to never-offered options.

    Returns:
        A list of up to ``n`` distinct ``Restaurant`` objects. Fewer than ``n``
        are returned only when the active pool is smaller than ``n``.
    """
    if rng is None:
        rng = random.Random()

    active = [r for r in restaurants if r.active]
    if not active:
        return []

    scores: dict[int, float] = {}
    for r in active:
        scores[id(r)] = score_restaurant(
            r, exploration_c=exploration_c, optimistic_prior=optimistic_prior
        )

    # Bucket by cuisine.
    by_cuisine: dict[str, list[Restaurant]] = defaultdict(list)
    for r in active:
        key = r.cuisine if r.cuisine else "__unknown__"
        by_cuisine[key].append(r)

    chosen: list[Restaurant] = []
    n = min(n, len(active))

    # Phase 1: one restaurant per distinct cuisine, cuisines weighted by their
    # aggregate score.
    remaining_cuisines = list(by_cuisine.keys())
    while len(chosen) < n and remaining_cuisines:
        cuisine_weights = [
            sum(scores[id(r)] for r in by_cuisine[c]) for c in remaining_cuisines
        ]
        picked_cuisine = _weighted_sample_without_replacement(
            remaining_cuisines, cuisine_weights, 1, rng
        )[0]
        remaining_cuisines.remove(picked_cuisine)

        bucket = by_cuisine[picked_cuisine]
        bucket_weights = [scores[id(r)] for r in bucket]
        pick = _weighted_sample_without_replacement(bucket, bucket_weights, 1, rng)[0]
        chosen.append(pick)

    # Phase 2: ran out of distinct cuisines but still need more; sample the
    # remaining restaurants by score without replacement.
    if len(chosen) < n:
        chosen_ids = {id(r) for r in chosen}
        leftovers = [r for r in active if id(r) not in chosen_ids]
        leftover_weights = [scores[id(r)] for r in leftovers]
        extra = _weighted_sample_without_replacement(
            leftovers, leftover_weights, n - len(chosen), rng
        )
        chosen.extend(extra)

    return chosen
