"""Staged, un-applied edits.

Nothing the UI does touches the dataset until Save. Edits accumulate in
`_editor/session.json` inside the dataset directory, keyed by the *original*
episode index, so the browser can be closed and reopened mid-review.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

EDITOR_DIR = "_editor"
SESSION_FILE = "session.json"


@dataclass
class EpisodeEdit:
    start: int = 0            # first kept frame, relative to the original episode
    end: int | None = None    # one past the last kept frame; None = to the end
    deleted: bool = False
    task: str | None = None   # None = leave the recorded task alone

    def is_noop(self, length: int) -> bool:
        return (
            not self.deleted
            and self.start == 0
            and (self.end is None or self.end >= length)
            and self.task is None
        )

    def kept_range(self, length: int) -> tuple[int, int]:
        start = max(0, min(int(self.start), length))
        end = length if self.end is None else max(0, min(int(self.end), length))
        return start, max(start, end)


@dataclass
class Session:
    dataset: Path
    edits: dict[int, EpisodeEdit] = field(default_factory=dict)
    updated: float = 0.0

    # ------------------------------------------------------------------- disk

    @property
    def file(self) -> Path:
        return self.dataset / EDITOR_DIR / SESSION_FILE

    @classmethod
    def load(cls, dataset: Path) -> "Session":
        self = cls(dataset=Path(dataset))
        if not self.file.is_file():
            return self
        try:
            raw = json.loads(self.file.read_text())
        except (json.JSONDecodeError, OSError):
            return self
        for key, value in (raw.get("edits") or {}).items():
            self.edits[int(key)] = EpisodeEdit(
                start=int(value.get("start", 0)),
                end=None if value.get("end") is None else int(value["end"]),
                deleted=bool(value.get("deleted", False)),
                task=value.get("task"),
            )
        self.updated = float(raw.get("updated", 0.0))
        return self

    def save(self) -> None:
        self.updated = time.time()
        self.file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated": self.updated,
            "edits": {str(k): asdict(v) for k, v in sorted(self.edits.items())},
        }
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.file)

    def clear(self) -> None:
        self.edits.clear()
        if self.file.is_file():
            self.file.unlink()

    # ------------------------------------------------------------------ edits

    def get(self, episode_index: int) -> EpisodeEdit:
        return self.edits.get(int(episode_index), EpisodeEdit())

    def set(self, episode_index: int, edit: EpisodeEdit, length: int) -> None:
        if edit.is_noop(length):
            self.edits.pop(int(episode_index), None)
        else:
            self.edits[int(episode_index)] = edit

    def as_json(self) -> dict:
        return {str(k): asdict(v) for k, v in sorted(self.edits.items())}

    @property
    def dirty(self) -> bool:
        return bool(self.edits)
