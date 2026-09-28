"""Browser-friendly proxies for episode video.

The source mp4s are AV1 and hold many episodes each, so they are wrong for the
browser twice over: AV1 playback is unreliable outside Chrome, and scrubbing
episode 200 would mean seeking minutes into a shared file. Instead ffmpeg cuts
each episode into its own small H.264 clip, plus a filmstrip sprite for the
timeline. `-ss` before `-i` plus `-frames:v n` is frame-exact here (verified
against episode lengths), so clip time maps to episode frame as `t * fps`.

Cache keys are content hashes of (video file, offset, length), so they survive
the episode renumbering that a delete causes - nothing needs invalidating.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

CACHE_DIR = "_editor/cache"
CLIP_WIDTH = 384
TILE_WIDTH = 80
MAX_TILES = 700  # 700 * 80px = 56000px, inside the 65535px JPEG limit
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


@dataclass(frozen=True)
class Slice:
    """One episode's window into one shared video file."""

    video: Path
    from_timestamp: float
    frames: int
    fps: int

    def key(self, *extra) -> str:
        raw = "|".join(
            [str(self.video), f"{self.from_timestamp:.6f}", str(self.frames), str(self.fps)]
            + [str(e) for e in extra]
        )
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


class ProxyCache:
    def __init__(self, dataset_path: Path):
        self.root = Path(dataset_path) / CACHE_DIR
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ clips

    def clip(self, sl: Slice) -> Path:
        key = sl.key()
        path = self.root / f"clip_{key}.mp4"
        if path.is_file() and path.stat().st_size > 0:
            return path
        with _lock_for(f"clip_{key}"):
            if path.is_file() and path.stat().st_size > 0:
                return path
            tmp = path.with_name(path.stem + ".tmp.mp4")  # ffmpeg picks the muxer by extension
            _run(
                [
                    FFMPEG, "-y", "-v", "error",
                    "-ss", f"{sl.from_timestamp:.6f}",
                    "-i", str(sl.video),
                    "-frames:v", str(sl.frames),
                    "-vf", f"scale={CLIP_WIDTH}:-2",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
                    "-pix_fmt", "yuv420p", "-profile:v", "baseline",
                    # constant frame rate so the browser's currentTime maps to
                    # episode frame as exactly t * fps
                    "-fps_mode", "cfr", "-r", str(sl.fps),
                    "-movflags", "+faststart", "-an",
                    str(tmp),
                ]
            )
            tmp.replace(path)
        return path

    # ------------------------------------------------------------- filmstrips

    def filmstrip(self, sl: Slice, step: int = 20) -> tuple[Path, dict]:
        """One JPEG holding every `step`-th frame side by side.

        `step` follows the timeline zoom so a tile is drawn at roughly its own
        aspect ratio: at k px/frame a tile spans step*k px on screen, and step
        is chosen to land near TILE_WIDTH."""
        step = max(1, int(step))
        step = max(step, -(-sl.frames // MAX_TILES))
        key = sl.key(step)
        image = self.root / f"strip_{key}.jpg"
        meta_path = self.root / f"strip_{key}.json"
        if image.is_file() and meta_path.is_file():
            return image, json.loads(meta_path.read_text())

        with _lock_for(f"strip_{key}"):
            if image.is_file() and meta_path.is_file():
                return image, json.loads(meta_path.read_text())
            tiles = max(1, -(-sl.frames // step))
            tile_h = 60
            tmp = image.with_name(image.stem + ".tmp.jpg")
            _run(
                [
                    FFMPEG, "-y", "-v", "error",
                    "-ss", f"{sl.from_timestamp:.6f}",
                    "-i", str(sl.video),
                    "-vf",
                    (
                        f"select='lt(n\\,{sl.frames})*not(mod(n\\,{step}))',"
                        f"scale={TILE_WIDTH}:{tile_h},tile={tiles}x1"
                    ),
                    "-frames:v", "1", "-update", "1", "-q:v", "5",
                    str(tmp),
                ]
            )
            tmp.replace(image)
            meta = {
                "tiles": tiles,
                "step": step,
                "tile_width": TILE_WIDTH,
                "tile_height": tile_h,
                "frames": sl.frames,
            }
            meta_path.write_text(json.dumps(meta))
        return image, meta

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.glob("*") if p.is_file())

    def clear(self) -> int:
        freed = self.size_bytes()
        for p in self.root.glob("*"):
            if p.is_file():
                p.unlink()
        return freed


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {result.stderr.strip()[:500]}")
