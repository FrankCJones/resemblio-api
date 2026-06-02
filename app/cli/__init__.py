"""Operational CLI entrypoints for the Resemblio API.

Modules in this package are designed to be invoked as ``python -m app.cli.<name>``
from systemd timers, cron jobs, or one-off operator runs. They share the
application's database session machinery and constants so behavior matches
the running service exactly.
"""
