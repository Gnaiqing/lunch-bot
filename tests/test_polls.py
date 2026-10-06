"""Poll rendering tests for percentages, voter mentions, and Maps links."""

from lunch_bot import polls
from lunch_bot.models import Restaurant


def test_poll_blocks_show_vote_share_voters_and_map_link():
    restaurants = [
        Restaurant(
            id=1,
            name="Miznon",
            cuisine="Mediterranean",
            maps_url="https://maps.example/miznon",
        ),
        Restaurant(id=2, name="Raku", cuisine="Japanese"),
    ]

    blocks = polls.build_poll_blocks(
        7,
        restaurants,
        tally={1: 2, 2: 1},
        voters={1: ["U_ALEX", "U_JESSE"], 2: ["U_JESSE"]},
    )

    miznon = blocks[1]["text"]["text"]
    raku = blocks[2]["text"]["text"]
    assert "<https://maps.example/miznon|Miznon> [Mediterranean]" in miznon
    assert "67% (2)" in miznon
    assert "<@U_ALEX>, <@U_JESSE>" in miznon
    assert "33% (1)" in raku
    assert "<@U_JESSE>" in raku


def test_empty_poll_renders_zero_percent_without_voters():
    blocks = polls.build_poll_blocks(
        1, [Restaurant(id=1, name="No Votes", cuisine="Other")]
    )
    assert "0% (0)" in blocks[1]["text"]["text"]


def test_percentage_rounds_half_up_like_poll_apps():
    restaurants = [Restaurant(id=index, name=str(index)) for index in range(1, 5)]
    blocks = polls.build_poll_blocks(
        1, restaurants, tally={1: 2, 2: 3, 3: 2, 4: 1}
    )
    assert "25% (2)" in blocks[1]["text"]["text"]
    assert "38% (3)" in blocks[2]["text"]["text"]
    assert "25% (2)" in blocks[3]["text"]["text"]
    assert "13% (1)" in blocks[4]["text"]["text"]
