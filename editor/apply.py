"""Write staged edits back to the dataset, in place.

Videos are never touched. In v3.0 an episode's frames are a timestamp window
into a shared mp4, so trimming k frames off the front is just
`from_timestamp += k / fps` and k fewer parquet rows - no re-encode, no quality
loss, and the mp4 stays byte-identical for anything else that reads it (Rerun
included). Deleting an episode leaves its segment sitting unreferenced inside
the mp4: dead bytes, but nothing points at them.

Everything that *is* rewritten - data/, meta/ - is hardlinked into
`_backups/<timestamp>/` first and then replaced atomically, so a backup costs no
disk and the previous state is always one `mv` away.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import stats as stats_mod
from .dataset import Dataset
from .session import Session

MIN_EPISODE_FRAMES = 2  # below this there is nothing to compute quantiles from


class ApplyError(Exception):
    pass


@dataclass
class Plan:
    kept: list[dict] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    trimmed: list[dict] = field(default_factory=list)
    retasked: list[dict] = field(default_factory=list)
    frames_before: int = 0
    frames_after: int = 0
    errors: list[str] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "episodes_before": len(self.kept) + len(self.deleted),
            "episodes_after": len(self.kept),
            "frames_before": self.frames_before,
            "frames_after": self.frames_after,
            "deleted": self.deleted,
            "trimmed": self.trimmed,
            "retasked": self.retasked,
            "errors": self.errors,
        }


def build_plan(ds: Dataset, session: Session) -> Plan:
    plan = Plan()
    for _, row in ds.episodes.iterrows():
        index = int(row["episode_index"])
        length = int(row["length"])
        plan.frames_before += length
        edit = session.get(index)
        if edit.deleted:
            plan.deleted.append(index)
            continue
        start, end = edit.kept_range(length)
        kept_len = end - start
        if kept_len < MIN_EPISODE_FRAMES:
            plan.errors.append(
                f"episode {index}: crop keeps {kept_len} frame(s); at least "
                f"{MIN_EPISODE_FRAMES} are needed. Widen it or delete the episode."
            )
            continue
        task = edit.task if edit.task is not None else ds.episode_task(row)
        if not str(task).strip():
            plan.errors.append(f"episode {index}: task text is empty")
        plan.kept.append(
            {
                "old_index": index,
                "new_index": len(plan.kept),
                "start": start,
                "end": end,
                "length": kept_len,
                "task": task,
            }
        )
        plan.frames_after += kept_len
        if start or end < length:
            plan.trimmed.append(
                {"index": index, "start": start, "end": end, "was": length, "now": kept_len}
            )
        if edit.task is not None and edit.task != ds.episode_task(row):
            plan.retasked.append({"index": index, "task": edit.task})
    if not plan.kept:
        plan.errors.append("that would delete every episode - the dataset must keep at least one")
    return plan


# ------------------------------------------------------------------- backups


def _hardlink_tree(src: Path, dst: Path) -> None:
    """Snapshot a directory for free. Safe because every writer below replaces
    files by rename rather than truncating them in place."""
    for path in src.rglob("*"):
        if path.is_dir():
            continue
        target = dst / path.relative_to(src.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, target)
        except OSError:
            shutil.copy2(path, target)


def make_backup(ds: Dataset) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = ds.path / "_backups" / stamp
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("data", "meta"):
        if (ds.path / name).is_dir():
            _hardlink_tree(ds.path / name, dest)
    return dest


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _write_json(payload: dict, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=4))
    os.replace(tmp, path)


# --------------------------------------------------------------------- apply


def apply_session(ds: Dataset, session: Session, backup: bool = True) -> dict:
    plan = build_plan(ds, session)
    if plan.errors:
        raise ApplyError("; ".join(plan.errors))
    if not session.dirty:
        return {"changed": False, "message": "nothing staged", **plan.as_json()}

    fps = ds.fps
    features = ds.features
    numeric_keys = [k for k in ds.numeric_keys]
    kept_by_old = {k["old_index"]: k for k in plan.kept}

    # --- task table: keep the existing order, append anything newly typed
    order: list[str] = []
    for task in ds.tasks.index:
        if any(k["task"] == str(task) for k in plan.kept):
            order.append(str(task))
    for k in plan.kept:
        if k["task"] not in order:
            order.append(k["task"])
    task_index = {task: i for i, task in enumerate(order)}

    # --- global row numbering follows episode order, not file order
    cursor = 0
    for k in plan.kept:
        k["from_index"] = cursor
        cursor += k["length"]
        k["to_index"] = cursor

    # --- rewrite the data files
    ep_stats: dict[int, dict] = {}
    data_groups: dict[Path, list[dict]] = {}
    for _, row in ds.episodes.iterrows():
        old = int(row["episode_index"])
        if old in kept_by_old:
            data_groups.setdefault(ds.data_file(row), []).append(kept_by_old[old])

    backup_path = make_backup(ds) if backup else None
    written_data: list[str] = []
    removed_data: list[str] = []

    for path, keeps in data_groups.items():
        source = pd.read_parquet(path)
        pieces = []
        for keep in sorted(keeps, key=lambda k: k["new_index"]):
            d = source[source["episode_index"] == keep["old_index"]]
            d = d.sort_values("frame_index").iloc[keep["start"] : keep["end"]].copy()
            n = len(d)
            if n != keep["length"]:
                raise ApplyError(
                    f"episode {keep['old_index']}: metadata says {keep['length']} frames "
                    f"after the crop but the parquet yielded {n}"
                )
            d["frame_index"] = np.arange(n, dtype="int64")
            d["timestamp"] = (np.arange(n) / fps).astype("float32")
            d["index"] = np.arange(keep["from_index"], keep["to_index"], dtype="int64")
            d["episode_index"] = np.int64(keep["new_index"])
            d["task_index"] = np.int64(task_index[keep["task"]])
            pieces.append(d)
            ep_stats[keep["old_index"]] = _episode_stats(d, numeric_keys, features)
        _write_parquet(pd.concat(pieces, ignore_index=True), path)
        written_data.append(str(path.relative_to(ds.path)))

    for path in {ds.data_file(r) for _, r in ds.episodes.iterrows()} - set(data_groups):
        if path.is_file():
            path.unlink()
            removed_data.append(str(path.relative_to(ds.path)))

    # --- rewrite the episode metadata
    meta = ds.episodes.copy()
    meta = meta[meta["episode_index"].isin(kept_by_old)].reset_index(drop=True)
    old_order = [int(v) for v in meta["episode_index"]]
    visual_stat_cols = {
        c
        for c in meta.columns
        if c.startswith("stats/") and c.split("/")[1] in features and features[c.split("/")[1]].is_visual
    }

    for i, row in meta.iterrows():
        keep = kept_by_old[int(row["episode_index"])]
        meta.at[i, "episode_index"] = keep["new_index"]
        meta.at[i, "length"] = keep["length"]
        meta.at[i, "dataset_from_index"] = keep["from_index"]
        meta.at[i, "dataset_to_index"] = keep["to_index"]
        for cam in ds.camera_keys:
            from_col = f"videos/{cam}/from_timestamp"
            to_col = f"videos/{cam}/to_timestamp"
            if from_col in meta.columns:
                new_from = float(row[from_col]) + keep["start"] / fps
                meta.at[i, from_col] = new_from
                meta.at[i, to_col] = new_from + keep["length"] / fps
        for col in meta.columns:
            if not col.startswith("stats/") or col in visual_stat_cols:
                continue
            _, feature, stat = col.split("/", 2)
            computed = ep_stats[keep["old_index"]].get(feature)
            if computed is not None and stat in computed:
                meta.at[i, col] = computed[stat].tolist()

    # assigned as a whole column: pandas refuses to put a list into a single cell
    meta["tasks"] = [np.array([kept_by_old[old]["task"]], dtype=object) for old in old_order]

    written_meta: list[str] = []
    existing_meta = set(ds.episode_meta_files)
    for (chunk, file_index), group in meta.groupby(
        ["meta/episodes/chunk_index", "meta/episodes/file_index"]
    ):
        path = ds.path / f"meta/episodes/chunk-{int(chunk):03d}/file-{int(file_index):03d}.parquet"
        _write_parquet(group.reset_index(drop=True), path)
        written_meta.append(str(path.relative_to(ds.path)))
        existing_meta.discard(path)
    for stale in existing_meta:
        stale.unlink()

    # --- tasks, info, aggregate stats
    tasks_df = pd.DataFrame({"task_index": [task_index[t] for t in order]}, index=pd.Index(order))
    tmp = ds.path / "meta" / "tasks.parquet.tmp"
    tasks_df.to_parquet(tmp)
    os.replace(tmp, ds.path / "meta" / "tasks.parquet")

    info = dict(ds.info)
    info["total_episodes"] = len(plan.kept)
    info["total_frames"] = plan.frames_after
    info["total_tasks"] = len(order)
    info["splits"] = {"train": f"0:{len(plan.kept)}"}
    _write_json(info, ds.path / "meta" / "info.json")

    _write_json(_aggregate(ds, meta, ep_stats, kept_by_old, features), ds.path / "meta" / "stats.json")

    session.clear()

    return {
        "changed": True,
        "backup": str(backup_path) if backup_path else None,
        "data_files_written": written_data,
        "data_files_removed": removed_data,
        "meta_files_written": written_meta,
        **plan.as_json(),
    }


def _episode_stats(d: pd.DataFrame, numeric_keys: list[str], features: dict) -> dict:
    out = {}
    for key in numeric_keys:
        if key not in d.columns:
            continue
        feature = features[key]
        scalar = feature.shape in ([1], [])
        array = d[key].to_numpy() if scalar else np.stack(d[key].to_numpy())
        out[key] = stats_mod.feature_stats(np.asarray(array), keepdims=scalar)
    return out


def _aggregate(ds: Dataset, meta: pd.DataFrame, ep_stats: dict, kept_by_old: dict, features: dict) -> dict:
    """Dataset-level stats.

    Numeric features are aggregated from the freshly computed per-episode stats.
    Image stats are carried over per episode - the frames themselves never
    changed, and a trim moves a channel mean by far less than the sampling noise
    in how lerobot estimated it (it samples ~O(n^0.75) frames) - but the
    aggregate is still recomputed over exactly the surviving episodes, so
    deleting episodes does move it.
    """
    new_to_old = {k["new_index"]: k["old_index"] for k in kept_by_old.values()}
    per_episode = []
    for _, row in meta.iterrows():
        old = new_to_old[int(row["episode_index"])]
        entry = {k: {s: np.asarray(v) for s, v in f.items()} for k, f in ep_stats[old].items()}
        for cam in ds.camera_keys:
            raw = {
                col.split("/", 2)[2]: row[col]
                for col in meta.columns
                if col.startswith(f"stats/{cam}/")
            }
            if raw:
                entry[cam] = stats_mod.normalise(raw, is_visual=True)
        per_episode.append(entry)
    return stats_mod.to_json(stats_mod.aggregate(per_episode))
