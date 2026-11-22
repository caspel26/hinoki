"""The activity app: a record of what happened to tasks, fed by events.

It requires ``tasks`` and ``members`` (hinoki.toml) and is the only app that
listens: ``tasks`` never imports it.
"""
