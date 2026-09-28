"""Per-episode and dataset-level statistics.

LeRobot's own implementation is used when it can be imported, so the numbers the
editor writes are bit-for-bit what `lerobot_train` would have computed. The
fallbacks exist only so the tool still runs outside the lerobot venv; they use
exact numpy quantiles where lerobot uses a 5000-bin histogram estimate, which is
a slightly *more* accurate answer to the same question.
"""

from __future__ import annotations

import numpy as np

try:  # pragma: no cover - depends on the interpreter the server runs under
    from lerobot.datasets.compute_stats import aggregate_stats as _lerobot_aggregate
    from lerobot.datasets.compute_stats import get_feature_stats as _lerobot_feature_stats

    HAVE_LEROBOT = True
except Exception:  # noqa: BLE001 - any import failure means "use the fallback"
    _lerobot_aggregate = _lerobot_feature_stats = None
    HAVE_LEROBOT = False

QUANTILES = [0.01, 0.10, 0.50, 0.90, 0.99]
QUANTILE_KEYS = [f"q{int(q * 100):02d}" for q in QUANTILES]


def _fallback_feature_stats(array: np.ndarray, axis, keepdims: bool) -> dict[str, np.ndarray]:
    a = array if array.ndim > 1 else array.reshape(-1, 1)
    count = np.array([array.shape[0]])
    stats = {
        "min": np.min(a, axis=0),
        "max": np.max(a, axis=0),
        "mean": np.mean(a, axis=0),
        "std": np.std(a, axis=0),
        "count": count,
    }
    for q, key in zip(QUANTILES, QUANTILE_KEYS, strict=True):
        stats[key] = np.quantile(a, q, axis=0)
    if keepdims and array.ndim == 1:
        for key in list(stats):
            if key != "count":
                stats[key] = np.atleast_1d(stats[key]).reshape(1)
    return stats


def feature_stats(array: np.ndarray, keepdims: bool) -> dict[str, np.ndarray]:
    """Stats for one vector feature over an episode: array is (n_frames, dim)."""
    if HAVE_LEROBOT:
        return _lerobot_feature_stats(array, axis=0, keepdims=keepdims)
    return _fallback_feature_stats(array, axis=0, keepdims=keepdims)


def _fallback_aggregate(stats_list: list[dict[str, dict]]) -> dict[str, dict[str, np.ndarray]]:
    keys = {k for s in stats_list for k in s}
    out: dict[str, dict[str, np.ndarray]] = {}
    for key in keys:
        per = [s[key] for s in stats_list if key in s]
        means = np.stack([s["mean"] for s in per])
        variances = np.stack([np.asarray(s["std"]) ** 2 for s in per])
        counts = np.stack([np.asarray(s["count"]) for s in per])
        total = counts.sum(axis=0)
        while counts.ndim < means.ndim:
            counts = np.expand_dims(counts, axis=-1)
        total_mean = (means * counts).sum(axis=0) / total
        delta = means - total_mean
        total_var = ((variances + delta**2) * counts).sum(axis=0) / total
        agg = {
            "min": np.min(np.stack([s["min"] for s in per]), axis=0),
            "max": np.max(np.stack([s["max"] for s in per]), axis=0),
            "mean": total_mean,
            "std": np.sqrt(total_var),
            "count": total,
        }
        for qk in QUANTILE_KEYS:
            if all(qk in s for s in per):
                agg[qk] = (np.stack([s[qk] for s in per]) * counts).sum(axis=0) / total
        out[key] = agg
    return out


def aggregate(stats_list: list[dict[str, dict]]) -> dict[str, dict[str, np.ndarray]]:
    if HAVE_LEROBOT:
        return _lerobot_aggregate(stats_list)
    return _fallback_aggregate(stats_list)


# --------------------------------------------------------------------- shapes
# Stats round-trip through parquet as nested lists, and pandas hands them back as
# object arrays. lerobot's validator is strict about shape - notably image stats
# must be exactly (3, 1, 1) and count exactly (1,) - so normalise on the way in.


def to_array(value) -> np.ndarray:
    if isinstance(value, np.ndarray) and value.dtype == object:
        return np.array(value.tolist(), dtype="float64")
    return np.asarray(value, dtype="float64")


def normalise(stats: dict[str, np.ndarray], is_visual: bool) -> dict[str, np.ndarray]:
    out = {}
    for key, value in stats.items():
        arr = to_array(value)
        if key == "count":
            arr = arr.reshape(1)
        elif is_visual:
            arr = arr.reshape(3, 1, 1)
        out[key] = arr
    return out


def to_json(stats: dict[str, dict[str, np.ndarray]]) -> dict:
    return {k: {s: np.asarray(v).tolist() for s, v in f.items()} for k, f in stats.items()}
