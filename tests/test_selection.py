"""Deterministic unit tests for the selection algorithm.

No network, Slack, Google, or Anthropic access. Uses a fixed-seed RNG so results
are reproducible.
"""

import random

from lunch_bot.models import Restaurant
from lunch_bot.selection import score_restaurant, select_candidates


def make_pool():
    """A small pool spanning several cuisines with varied vote history."""
    return [
        # Italian: one popular, one unpopular-but-offered.
        Restaurant(name="Popular Italian", cuisine="italian", times_selected=10, total_votes=40),
        Restaurant(name="Meh Italian", cuisine="italian", times_selected=10, total_votes=0),
        # Japanese.
        Restaurant(name="Sushi Spot", cuisine="japanese", times_selected=5, total_votes=15),
        # Mexican.
        Restaurant(name="Taqueria", cuisine="mexican", times_selected=3, total_votes=6),
        # Thai (never offered).
        Restaurant(name="New Thai", cuisine="thai", times_selected=0, total_votes=0),
        # Indian (never offered).
        Restaurant(name="New Indian", cuisine="indian", times_selected=0, total_votes=0),
        # Vegan.
        Restaurant(name="Green Bowl", cuisine="vegan", times_selected=2, total_votes=3),
    ]


def test_returns_requested_count():
    rng = random.Random(42)
    picks = select_candidates(make_pool(), n=5, rng=rng)
    assert len(picks) == 5


def test_no_duplicates():
    rng = random.Random(7)
    for _ in range(50):
        picks = select_candidates(make_pool(), n=5, rng=rng)
        names = [r.name for r in picks]
        assert len(names) == len(set(names))


def test_respects_pool_smaller_than_n():
    rng = random.Random(1)
    pool = make_pool()[:3]
    picks = select_candidates(pool, n=5, rng=rng)
    assert len(picks) == 3
    assert len({r.name for r in picks}) == 3


def test_favors_diversity_of_cuisines():
    """Most selections of 5 from a 7-cuisine pool should be all-distinct cuisines."""
    all_distinct = 0
    trials = 300
    for seed in range(trials):
        rng = random.Random(seed)
        picks = select_candidates(make_pool(), n=5, rng=rng)
        cuisines = [r.cuisine for r in picks]
        if len(cuisines) == len(set(cuisines)):
            all_distinct += 1
    # With 7 distinct cuisines available and phase-1 picking distinct cuisines
    # first, every pick of 5 should have 5 distinct cuisines.
    assert all_distinct == trials


def test_high_vote_beats_zero_vote_within_cuisine():
    """Within the Italian cuisine, the popular option should be chosen far more
    often than the offered-but-zero-vote option across many trials."""
    popular = 0
    meh = 0
    trials = 2000
    for seed in range(trials):
        rng = random.Random(seed)
        picks = select_candidates(make_pool(), n=5, rng=rng)
        names = {r.name for r in picks}
        if "Popular Italian" in names:
            popular += 1
        if "Meh Italian" in names:
            meh += 1
    assert popular > meh, f"popular={popular} meh={meh}"


def test_never_offered_are_explored():
    """Restaurants never offered (times_selected == 0) must still get picked
    across trials — they should not be starved like offered-but-zero-vote ones."""
    new_seen = 0
    trials = 500
    for seed in range(trials):
        rng = random.Random(seed)
        picks = select_candidates(make_pool(), n=4, rng=rng)
        names = {r.name for r in picks}
        if "New Thai" in names or "New Indian" in names:
            new_seen += 1
    # They belong to unique cuisines, so they should appear very frequently.
    assert new_seen > trials * 0.5, f"new_seen={new_seen}"


def test_optimistic_prior_beats_offered_zero_vote():
    """A never-offered restaurant should score higher than an offered restaurant
    that earned zero votes."""
    never = Restaurant(name="never", cuisine="x", times_selected=0, total_votes=0)
    offered_zero = Restaurant(name="zero", cuisine="x", times_selected=10, total_votes=0)
    assert score_restaurant(never) > score_restaurant(offered_zero)


def test_deterministic_with_same_seed():
    picks_a = select_candidates(make_pool(), n=5, rng=random.Random(123))
    picks_b = select_candidates(make_pool(), n=5, rng=random.Random(123))
    assert [r.name for r in picks_a] == [r.name for r in picks_b]


def test_inactive_excluded():
    pool = make_pool()
    pool[0].active = False
    rng = random.Random(3)
    for _ in range(20):
        picks = select_candidates(pool, n=6, rng=rng)
        assert all(r.name != "Popular Italian" for r in picks)


def test_empty_pool_returns_empty():
    assert select_candidates([], n=5, rng=random.Random(0)) == []


def test_mixed_case_cuisine_treated_as_one_bucket():
    """Mixed-case/whitespace variants of one cuisine collapse into a single
    diversity bucket, so the guarantee still holds.

    The pool has three "Japanese" spellings (``Japanese``/``japanese``/``
    JAPANESE ``) plus one lone Thai. After normalization there are exactly two
    cuisines, so a pick of 2 always spans both — the Thai option is always chosen.
    Without normalization the Japanese variants would be three separate buckets
    and the Thai option could be skipped.
    """
    pool = [
        Restaurant(name="Alpha", cuisine="Japanese", times_selected=1, total_votes=1),
        Restaurant(name="Beta", cuisine="japanese", times_selected=1, total_votes=1),
        Restaurant(name="Gamma", cuisine=" JAPANESE ", times_selected=1, total_votes=1),
        Restaurant(name="Solo Thai", cuisine="thai", times_selected=1, total_votes=1),
    ]
    for seed in range(200):
        picks = select_candidates(pool, n=2, rng=random.Random(seed))
        cuisines = {r.cuisine.strip().casefold() for r in picks}
        assert cuisines == {"japanese", "thai"}, f"seed={seed} picks={[r.name for r in picks]}"
        assert any(r.name == "Solo Thai" for r in picks)
