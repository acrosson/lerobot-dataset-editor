#!/usr/bin/env python
# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas", "numpy", "pyarrow"]
# ///
"""Local web editor for LeRobot v3.0 datasets.

    ./run.sh                                   # discover datasets and open the UI
    ./run.sh --root /path/to/datasets --port 8800

Serves a single-page UI plus a small JSON API over the dataset directories it
finds. Nothing is written until the UI asks it to.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from editor import proxy as proxy_mod  # noqa: E402
from editor.apply import ApplyError, apply_session, build_plan  # noqa: E402
from editor.dataset import Dataset, find_datasets, is_dataset  # noqa: E402
from editor.session import EpisodeEdit, Session  # noqa: E402
from editor.stats import HAVE_LEROBOT  # noqa: E402

WEB = HERE / "web"
ROOTS: list[Path] = []
_ds_cache: dict[str, tuple[float, Dataset]] = {}
_ds_lock = threading.Lock()


# ----------------------------------------------------------------- dataset io


def _stamp(path: Path) -> float:
    meta = path / "meta"
    newest = 0.0
    for p in list(meta.rglob("*.parquet")) + [meta / "info.json"]:
        if p.is_file():
            newest = max(newest, p.stat().st_mtime)
    return newest


def get_dataset(path_str: str) -> Dataset:
    path = Path(path_str).expanduser().resolve()
    if not is_dataset(path):
        raise FileNotFoundError(f"{path} is not a LeRobot dataset")
    if not any(_is_within(path, root) for root in ROOTS):
        raise PermissionError(f"{path} is outside the served roots")
    with _ds_lock:
        stamp = _stamp(path)
        cached = _ds_cache.get(str(path))
        if cached and cached[0] == stamp:
            return cached[1]
        ds = Dataset(path)
        _ds_cache[str(path)] = (stamp, ds)
        return ds


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def invalidate(path: Path) -> None:
    with _ds_lock:
        _ds_cache.pop(str(path), None)


def slice_for(ds: Dataset, episode: int, cam: str) -> proxy_mod.Slice:
    row = ds.episode_row(episode)
    start, _ = ds.video_slice(row, cam)
    return proxy_mod.Slice(
        video=ds.video_file(row, cam),
        from_timestamp=start,
        frames=int(row["length"]),
        fps=ds.fps,
    )


# --------------------------------------------------------------- api handlers


def api_datasets(_query: dict) -> dict:
    out = []
    for path in find_datasets(ROOTS):
        try:
            ds = get_dataset(str(path))
        except Exception:  # noqa: BLE001 - a broken dataset shouldn't hide the rest
            out.append({"path": str(path), "name": path.name, "error": "unreadable"})
            continue
        summary = ds.summary()
        summary["staged_edits"] = len(Session.load(ds.path).edits)
        out.append(summary)
    return {"datasets": out, "roots": [str(r) for r in ROOTS], "lerobot": HAVE_LEROBOT}


def api_dataset(query: dict) -> dict:
    ds = get_dataset(query["path"][0])
    session = Session.load(ds.path)
    episodes = ds.episode_list()
    for ep in episodes:
        edit = session.get(ep["index"])
        start, end = edit.kept_range(ep["length"])
        ep["deleted"] = edit.deleted
        ep["start"] = start
        ep["end"] = end
        ep["edited"] = not edit.is_noop(ep["length"])
        if edit.task is not None:
            ep["task"] = edit.task
    return {
        **ds.summary(),
        "episodes": episodes,
        "staged": session.as_json(),
        "ffmpeg": proxy_mod.ffmpeg_available(),
    }


def api_episode(query: dict) -> dict:
    ds = get_dataset(query["path"][0])
    episode = int(query["ep"][0])
    row = ds.episode_row(episode)
    length = int(row["length"])
    session = Session.load(ds.path)
    edit = session.get(episode)
    start, end = edit.kept_range(length)
    series = ds.episode_series(episode)

    state = series.get("observation.state")
    action = series.get("action")
    speed = None
    if state is not None and len(state) > 1:
        step = np.abs(np.diff(state, axis=0)).sum(axis=1)
        speed = np.concatenate([[step[0]], step])

    cams = []
    for cam in ds.camera_keys:
        from_ts, to_ts = ds.video_slice(row, cam)
        shape = ds.features[cam].shape  # (height, width, channels)
        cams.append(
            {
                "key": cam,
                "label": cam.rsplit(".", 1)[-1],
                # identifies the exact video slice; the client puts it in the clip
                # and filmstrip URLs so a save that re-cuts an episode can't be
                # served from the browser's cache as the pre-edit clip
                "token": slice_for(ds, episode, cam).key(),
                "height": shape[0] if len(shape) > 1 else None,
                "width": shape[1] if len(shape) > 1 else None,
                "video": str(ds.video_file(row, cam).relative_to(ds.path)),
                "from_timestamp": from_ts,
                "to_timestamp": to_ts,
            }
        )

    return {
        "index": episode,
        "length": length,
        "fps": ds.fps,
        "task": edit.task if edit.task is not None else ds.episode_task(row),
        "recorded_task": ds.episode_task(row),
        "deleted": edit.deleted,
        "start": start,
        "end": end,
        "joint_names": ds.features["observation.state"].names or [],
        "state": _round(state),
        "action": _round(action),
        "speed": _round(speed),
        "cameras": cams,
    }


def _round(array) -> list | None:
    if array is None:
        return None
    return np.round(np.asarray(array, dtype="float64"), 5).tolist()


def api_filmstrip_meta(query: dict) -> dict:
    ds = get_dataset(query["path"][0])
    episode = int(query["ep"][0])
    cam = query["cam"][0]
    step = int(query.get("step", [20])[0])
    _, meta = proxy_mod.ProxyCache(ds.path).filmstrip(slice_for(ds, episode, cam), step)
    return meta


def api_plan(query: dict) -> dict:
    ds = get_dataset(query["path"][0])
    return build_plan(ds, Session.load(ds.path)).as_json()


def api_edit(body: dict) -> dict:
    ds = get_dataset(body["path"])
    episode = int(body["episode"])
    length = int(ds.episode_row(episode)["length"])
    session = Session.load(ds.path)
    edit = session.get(episode)
    edit = EpisodeEdit(
        start=int(body.get("start", edit.start)),
        end=None if body.get("end") is None else int(body["end"]),
        deleted=bool(body.get("deleted", edit.deleted)),
        task=body.get("task", edit.task),
    )
    if edit.task is not None and edit.task == ds.episode_task(ds.episode_row(episode)):
        edit.task = None
    session.set(episode, edit, length)
    session.save()
    start, end = edit.kept_range(length)
    return {
        "ok": True,
        "episode": episode,
        "start": start,
        "end": end,
        "deleted": edit.deleted,
        "edited": not edit.is_noop(length),
        "staged_total": len(session.edits),
    }


def api_clear(body: dict) -> dict:
    ds = get_dataset(body["path"])
    Session.load(ds.path).clear()
    return {"ok": True}


def api_apply(body: dict) -> dict:
    ds = get_dataset(body["path"])
    session = Session.load(ds.path)
    report = apply_session(ds, session, backup=bool(body.get("backup", True)))
    invalidate(ds.path)
    return report


def api_cache_clear(body: dict) -> dict:
    ds = get_dataset(body["path"])
    return {"ok": True, "freed_bytes": proxy_mod.ProxyCache(ds.path).clear()}


def api_warm(body: dict) -> dict:
    """Pre-render clips and filmstrips in the background so scrubbing is instant."""
    ds = get_dataset(body["path"])
    episodes = [int(e) for e in body.get("episodes", [])]
    cache = proxy_mod.ProxyCache(ds.path)

    def work() -> None:
        for episode in episodes:
            for cam in ds.camera_keys:
                try:
                    sl = slice_for(ds, episode, cam)
                    cache.clip(sl)
                    cache.filmstrip(sl)
                except Exception:  # noqa: BLE001 - warming is best effort
                    pass

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "queued": len(episodes)}


GET_ROUTES = {
    "/api/datasets": api_datasets,
    "/api/dataset": api_dataset,
    "/api/episode": api_episode,
    "/api/filmstrip.json": api_filmstrip_meta,
    "/api/plan": api_plan,
}

POST_ROUTES = {
    "/api/edit": api_edit,
    "/api/edits/clear": api_clear,
    "/api/apply": api_apply,
    "/api/cache/clear": api_cache_clear,
    "/api/warm": api_warm,
}


# -------------------------------------------------------------------- serving

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lerobot-dataset-editor"

    def log_message(self, fmt: str, *args) -> None:  # quieter than the default
        if "/api/" in str(args[0]) and " 200 " not in str(args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc: Exception) -> None:
        status = {
            FileNotFoundError: HTTPStatus.NOT_FOUND,
            KeyError: HTTPStatus.NOT_FOUND,
            PermissionError: HTTPStatus.FORBIDDEN,
            ApplyError: HTTPStatus.CONFLICT,
        }.get(type(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
        if status == HTTPStatus.INTERNAL_SERVER_ERROR:
            traceback.print_exc()
        self._json({"error": str(exc) or type(exc).__name__}, int(status))

    def _file(self, path: Path, content_type: str | None = None, cache: str = "no-store") -> None:
        if not path.is_file():
            self._json({"error": f"{path.name} not found"}, 404)
            return
        size = path.stat().st_size
        ctype = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        partial = False
        match = RANGE_RE.match(self.headers.get("Range", "") or "")
        if match:
            first, last = match.group(1), match.group(2)
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            elif last:  # suffix range
                start = max(0, size - int(last))
            end = min(end, size - 1)
            partial = start <= end
        if not partial:
            start, end = 0, size - 1
        length = end - start + 1

        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # -- verbs

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        route, query = url.path, parse_qs(url.query)
        try:
            if route in GET_ROUTES:
                self._json(GET_ROUTES[route](query))
                return
            if route in ("/api/clip", "/api/filmstrip"):
                ds = get_dataset(query["path"][0])
                sl = slice_for(ds, int(query["ep"][0]), query["cam"][0])
                cache = proxy_mod.ProxyCache(ds.path)
                if route == "/api/clip":
                    self._file(cache.clip(sl), "video/mp4", cache="private, max-age=3600")
                else:
                    image, _ = cache.filmstrip(sl, int(query.get("step", [20])[0]))
                    self._file(image, "image/jpeg", cache="private, max-age=3600")
                return
            if route == "/" or route == "/index.html":
                self._file(WEB / "index.html", "text/html; charset=utf-8")
                return
            name = route.lstrip("/")
            if name and "/" not in name and (WEB / name).is_file():
                self._file(WEB / name)
                return
            self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001 - every failure becomes JSON
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        handler = POST_ROUTES.get(url.path)
        if handler is None:
            self._json({"error": "not found"}, 404)
            return
        try:
            size = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(size) or b"{}")
            self._json(handler(body))
        except Exception as exc:  # noqa: BLE001
            self._error(exc)


def default_roots() -> list[Path]:
    env = os.environ.get("LRDE_ROOTS")
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p]
    guess = Path.home() / "dev" / "Panthera-HT_lerobot" / "Panthera-HT_lerobot_dataset"
    return [guess] if guess.is_dir() else [Path.cwd()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", action="append", type=Path, help="directory to search for datasets (repeatable)")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true", help="don't launch a browser")
    args = parser.parse_args()

    global ROOTS
    ROOTS = [p.expanduser().resolve() for p in (args.root or default_roots())]
    missing = [r for r in ROOTS if not r.is_dir()]
    if missing:
        print(f"warning: no such directory: {', '.join(map(str, missing))}", file=sys.stderr)
    ROOTS = [r for r in ROOTS if r.is_dir()]
    if not ROOTS:
        print("error: no readable dataset roots; pass --root", file=sys.stderr)
        return 2

    found = find_datasets(ROOTS)
    url = f"http://{args.host}:{args.port}/"
    print(f"lerobot-dataset-editor  ->  {url}")
    print(f"  roots:    {', '.join(str(r) for r in ROOTS)}")
    print(f"  datasets: {len(found)}")
    if not proxy_mod.ffmpeg_available():
        print("  warning:  ffmpeg not on PATH - video previews will fail", file=sys.stderr)
    if not HAVE_LEROBOT:
        print("  note:     lerobot not importable; using built-in stats (numpy quantiles)")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
