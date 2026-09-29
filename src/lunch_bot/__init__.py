"""Lunch-decision Slack bot for the #thoughts-on-lunch team channel.

A weekly workflow bot: discovers nearby restaurants, runs a diversity-aware
Block Kit poll, records votes, and preps a (human-placed) Uber Eats group order.

The subpackage is intentionally light at import time. Heavy/optional
dependencies (slack-bolt, googlemaps, anthropic) are imported lazily inside the
modules and functions that need them, so that the pure-logic modules
(``config``, ``models``, ``selection``) and their tests can run without those
packages installed.
"""

__version__ = "0.1.0"
