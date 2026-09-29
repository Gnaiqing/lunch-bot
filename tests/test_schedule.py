"""Tests for schedule parsing and per-group scheduling config.

No network or credentials needed — pure config parsing.
"""

import textwrap

import pytest

from lunch_bot.config import (
    DEFAULT_SLACK_CHANNEL_ID,
    DEFAULT_TIMEZONE,
    ScheduleEntry,
    load_config,
)

_PHASES = ("poll_create", "poll_close", "order_reminder", "order_deadline")


def test_schedule_defaults_when_absent(tmp_path):
    cfg = load_config(config_path=str(tmp_path / "nope.yaml"), env={}, load_dotenv=False)
    assert cfg.timezone == DEFAULT_TIMEZONE
    assert set(cfg.schedule) == set(_PHASES)
    assert cfg.schedule["poll_create"] == ScheduleEntry(day="mon", hour=10, minute=0)
    assert cfg.schedule["poll_close"] == ScheduleEntry(day="wed", hour=10, minute=0)
    assert cfg.schedule["order_reminder"] == ScheduleEntry(day="thu", hour=10, minute=0)
    assert cfg.schedule["order_deadline"] == ScheduleEntry(day="thu", hour=11, minute=0)


def test_default_production_channel(tmp_path):
    cfg = load_config(config_path=str(tmp_path / "nope.yaml"), env={}, load_dotenv=False)
    assert cfg.slack_channel_id == DEFAULT_SLACK_CHANNEL_ID  # #dl-time-series-tabular
    assert cfg.restaurants_csv == "data/restaurants_seed.csv"


def _write(tmp_path, body: str):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(textwrap.dedent(body))
    return str(yaml_file)


def test_schedule_from_yaml(tmp_path):
    path = _write(
        tmp_path,
        """
        timezone: America/New_York
        schedule:
          poll_create:    { day: tue, time: "09:30" }
          poll_close:     { day: thu, time: "12:00" }
          order_reminder: { day: fri, time: "08:15" }
          order_deadline: { day: fri, time: "11:45" }
        """,
    )
    cfg = load_config(config_path=path, env={}, load_dotenv=False)
    assert cfg.timezone == "America/New_York"
    assert cfg.schedule["poll_create"] == ScheduleEntry(day="tue", hour=9, minute=30)
    assert cfg.schedule["poll_close"] == ScheduleEntry(day="thu", hour=12, minute=0)
    assert cfg.schedule["order_reminder"] == ScheduleEntry(day="fri", hour=8, minute=15)
    assert cfg.schedule["order_deadline"] == ScheduleEntry(day="fri", hour=11, minute=45)


def test_schedule_partial_yaml_falls_back_per_phase(tmp_path):
    # Only override one phase; the rest (and time within it) keep defaults.
    path = _write(
        tmp_path,
        """
        schedule:
          poll_create: { day: sunday }
        """,
    )
    cfg = load_config(config_path=path, env={}, load_dotenv=False)
    # Full weekday name is normalised; time falls back to the default 10:00.
    assert cfg.schedule["poll_create"] == ScheduleEntry(day="sun", hour=10, minute=0)
    # Untouched phases keep their defaults.
    assert cfg.schedule["poll_close"] == ScheduleEntry(day="wed", hour=10, minute=0)


def test_weekday_full_names_and_case(tmp_path):
    path = _write(
        tmp_path,
        """
        schedule:
          poll_create: { day: MONDAY, time: "10:00" }
          poll_close:  { day: Wed, time: "10:00" }
        """,
    )
    cfg = load_config(config_path=path, env={}, load_dotenv=False)
    assert cfg.schedule["poll_create"].day == "mon"
    assert cfg.schedule["poll_close"].day == "wed"


def test_bad_time_raises_clear_error(tmp_path):
    path = _write(
        tmp_path,
        """
        schedule:
          poll_create: { day: mon, time: "25:00" }
        """,
    )
    with pytest.raises(ValueError) as exc:
        load_config(config_path=path, env={}, load_dotenv=False)
    assert "schedule.poll_create.time" in str(exc.value)


def test_malformed_time_string_raises(tmp_path):
    path = _write(
        tmp_path,
        """
        schedule:
          poll_create: { day: mon, time: "1000" }
        """,
    )
    with pytest.raises(ValueError) as exc:
        load_config(config_path=path, env={}, load_dotenv=False)
    assert "HH:MM" in str(exc.value)


def test_bad_day_raises_clear_error(tmp_path):
    path = _write(
        tmp_path,
        """
        schedule:
          poll_create: { day: funday, time: "10:00" }
        """,
    )
    with pytest.raises(ValueError) as exc:
        load_config(config_path=path, env={}, load_dotenv=False)
    assert "schedule.poll_create.day" in str(exc.value)


def test_schedule_entry_time_str():
    assert ScheduleEntry(day="thu", hour=11, minute=0).time_str == "11:00"
    assert ScheduleEntry(day="thu", hour=9, minute=5).time_str == "09:05"
