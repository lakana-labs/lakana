"""The words an asset header shares with `lakana.yaml`: the model kinds and the write modes.

A leaf module: `layers` needs them and is imported by `project_config`, so it may not import
`asset_model`, which imports `project_config`. `asset_model` imports them from here, so every
existing `from lakana_core.projects.asset_model import MODEL_KINDS` keeps working.
"""

from __future__ import annotations

MODEL_KINDS = ("fact", "dimension", "wide", "staging")
"""What an asset's `kind:` may say. `lakana.assets.MODEL_KINDS` repeats it (a parity test
pins both, added with the decorator's arguments)."""

WRITE_CHOICES = ("replace", "append", "merge", "history")
"""What a run does to an asset's table: replace every row, append the rows past the watermark,
merge the rows on a key, or keep every version of a dimension's rows (`history`).
`lakana.assets.WRITE_CHOICES` is the same (a parity test pins both)."""
