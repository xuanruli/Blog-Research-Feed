"""Cron-side runner for the Blog-Research-Feed Managed Agent.

This package is HOST-SIDE only — it runs on a GitHub Action runner (or a dev
laptop) to create and supervise Managed Agent sessions. It does **not** ship
into the agent's container.

Separation of concerns (vs the ``brf`` package):

* ``brf`` is a pure tool CLI invoked by the agent inside its session container
  via bash. Holds no orchestration logic, knows nothing about Managed Agents.
* ``cron`` calls the Anthropic API to create sessions, upload Files,
  and stream events. Does NOT import from ``brf``.

Both happen to live in this repo for now because the CLI source needs to be
the same git repo the agent's environment.yaml installs from. Could split
into separate packages later if it stops being convenient.
"""
