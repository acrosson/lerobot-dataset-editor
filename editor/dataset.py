"""Read-side model of a LeRobot v3.0 dataset.

v3.0 packs many episodes into a few shared parquet files and a few shared mp4s.
An episode is therefore a *slice*: a contiguous run of rows in one parquet file,
and a (from_timestamp, to_timestamp) window inside one mp4 per camera. All of
that lives in meta/episodes/chunk-*/file-*.parquet, one row per episode.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

STAT_KEYS = ["min", "max", "mean", "std", "count", "q01", "q10", "q50", "q90", "q99"]


def is_dataset(path: Path) -> bool:
    return (path / "meta" / "info.json").is_file()


def find_datasets(roots: list[Path], max_depth: int = 4) -> list[Path]:
    """Walk the given roots for anything that looks like a LeRobot dataset."""
    found: list[Path] = []
    seen: set[Path] = set()

    def walk(d: Path, depth: int) -> None:
        if depth < 0 or not d.is_dir() or d.name.startswith((".", "_")):
            return
        if d in seen:
            return
        seen.add(d)
        if is_dataset(d):
            found.append(d)
            return  # datasets don't nest
        try:
            children = sorted(p for p in d.iterdir() if p.is_dir())
        except PermissionError:
            return
        for child in children:
            walk(child, depth - 1)

    for root in roots:
        walk(Path(root).expanduser().resolve(), max_depth)
    return found


@dataclass
class Feature:
    key: str
    dtype: str
    shape: list[int]
    names: list[str] | None

    @property
    def is_visual(self) -> bool:
        return self.dtype in ("image", "video")

    @property
    def is_numeric(self) -> bool:
        return self.dtype not in ("image", "video", "string")


class Dataset:
    """Everything the editor needs to read; nothing that writes."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        if not is_dataset(self.path):
            raise FileNotFoundError(f"{self.path} has no meta/info.json")
        self.info = json.loads((self.path / "meta" / "info.json").read_text())
        self.fps = int(self.info["fps"])
        self.features = {
            k: Feature(k, v["dtype"], list(v.get("shape") or []), v.get("names"))
            for k, v in self.info["features"].items()
        }
        self.camera_keys = [k for k, f in self.features.items() if f.is_visual]
        self.numeric_keys = [k for k, f in self.features.items() if f.is_numeric]
        self.episodes = self._load_episodes()
        self.tasks = self._load_tasks()

    # ---------------------------------------------------------------- loading

    @property
    def episode_meta_files(self) -> list[Path]:
        return sorted((self.path / "meta" / "episodes").rglob("file-*.parquet"))

    def _load_episodes(self) -> pd.DataFrame:
        files = self.episode_meta_files
        if not files:
            raise FileNotFoundError(f"{self.path}: no meta/episodes/**/file-*.parquet")
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        return df.sort_values("episode_index").reset_index(drop=True)

    def _load_tasks(self) -> pd.DataFrame:
        """meta/tasks.parquet is indexed by the task string, with a task_index column."""
        df = pd.read_parquet(self.path / "meta" / "tasks.parquet")
        return df.sort_values("task_index")

    @property
    def task_by_index(self) -> dict[int, str]:
        return {int(v): str(k) for k, v in self.tasks["task_index"].items()}

    # ------------------------------------------------------------------ paths

    def data_file(self, row) -> Path:
        return self.path / self.info["data_path"].format(
            chunk_index=int(row["data/chunk_index"]), file_index=int(row["data/file_index"])
        )

    def video_file(self, row, cam: str) -> Path:
        return self.path / self.info["video_path"].format(
            video_key=cam,
            chunk_index=int(row[f"videos/{cam}/chunk_index"]),
            file_index=int(row[f"videos/{cam}/file_index"]),
        )

    def video_slice(self, row, cam: str) -> tuple[float, float]:
        return (
            float(row[f"videos/{cam}/from_timestamp"]),
            float(row[f"videos/{cam}/to_timestamp"]),
        )

    # --------------------------------------------------------------- episodes

    def episode_row(self, episode_index: int) -> pd.Series:
        hit = self.episodes[self.episodes["episode_index"] == episode_index]
        if hit.empty:
            raise KeyError(f"no episode {episode_index} in {self.path.name}")
        return hit.iloc[0]

    def episode_task(self, row) -> str:
        tasks = row.get("tasks")
        if tasks is None:
            return ""
        if isinstance(tasks, (list, np.ndarray, pd.Series)):
            return str(tasks[0]) if len(tasks) else ""
        return str(tasks)

    @functools.lru_cache(maxsize=8)
    def _read_data_file(self, path_str: str) -> pd.DataFrame:
        return pd.read_parquet(path_str)

    def episode_frames(self, episode_index: int) -> pd.DataFrame:
        """The rows of one episode, in frame order."""
        row = self.episode_row(episode_index)
        df = self._read_data_file(str(self.data_file(row)))
        d = df[df["episode_index"] == episode_index]
        return d.sort_values("frame_index").reset_index(drop=True)

    def episode_series(self, episode_index: int) -> dict[str, np.ndarray]:
        """Vector features of one episode as (n, dim) float arrays."""
        d = self.episode_frames(episode_index)
        out: dict[str, np.ndarray] = {}
        for key in ("observation.state", "action"):
            if key in d.columns:
                out[key] = np.stack(d[key].to_numpy()).astype("float32")
        return out

    # ------------------------------------------------------------- summarising

    def summary(self) -> dict:
        eps = self.episodes
        return {
            "name": self.path.name,
            "path": str(self.path),
            "codebase_version": self.info.get("codebase_version"),
            "robot_type": self.info.get("robot_type"),
            "fps": self.fps,
            "total_episodes": int(len(eps)),
            "total_frames": int(eps["length"].sum()),
            "cameras": self.camera_keys,
            "joint_names": self.features["observation.state"].names
            if "observation.state" in self.features
            else [],
            "tasks": [str(k) for k in self.tasks.index],
        }

    def episode_list(self) -> list[dict]:
        out = []
        for _, row in self.episodes.iterrows():
            out.append(
                {
                    "index": int(row["episode_index"]),
                    "length": int(row["length"]),
                    "seconds": round(int(row["length"]) / self.fps, 2),
                    "task": self.episode_task(row),
                }
            )
        return out
