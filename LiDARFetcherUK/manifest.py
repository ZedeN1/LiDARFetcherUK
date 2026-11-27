"""Per-dataset download record, kept as lidar_fetcher.json in the dataset folder.

It records every tile (mapcache path, status, attempts, when, files produced,
errors) and the processing that was run, and decides which tiles a new run
must fetch: new or previously failed tiles, tiles whose path changed or whose
files have gone, tiles fetched before the dataset's last modified date moved
on, and tiles still missing the .ovr overviews when pyramids are wanted.
Saved after every tile, so an interrupted run resumes where it stopped.

Status "nodata": the publisher has no such tile (e.g. sea along the coast);
not retried until the dataset's date moves on.
"""
import json
import os
from datetime import datetime, timezone

FILE_NAME = "lidar_fetcher.json"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Manifest:
    def __init__(self, folder, ds, tiles_dir):
        self.path = os.path.join(folder, FILE_NAME)
        self.tiles_dir = tiles_dir
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {"created": now(), "tiles": {}}
        self.data.update({"dataset": ds.id, "title": ds.title, "resolution": ds.resolution_label,
                          "dataset_last_modified": ds.last_modified})
        self.tiles = self.data.setdefault("tiles", {})

    def check(self, tile_id, path, last_modified, want_ovr=False):
        """(action, reason) for one tile: action is "skip" or "download"."""
        entry = self.tiles.get(tile_id)
        if entry is None:
            return "download", None
        fetched_for = entry.get("dataset_last_modified")
        if last_modified and fetched_for and last_modified > fetched_for:
            return "download", f"dataset updated since ({fetched_for} to {last_modified})"
        if entry.get("status") == "nodata":
            return "skip", "no data at the publisher"
        if entry.get("status") != "ok":
            return "download", "retrying, failed last time"
        if entry.get("path") != path:
            return "download", "download path changed"
        missing = [f for f in entry.get("files", []) if not os.path.exists(os.path.join(self.tiles_dir, f))]
        if missing:
            return "download", f"{len(missing)} file(s) missing"
        if want_ovr and not entry.get("ovr_asked") and not any(
                f.lower().endswith(".ovr") for f in entry.get("files", [])):
            return "download", "fetching its overviews"
        when = (entry.get("downloaded") or "")[:10]
        return "skip", f"already downloaded{' ' + when if when else ''}"

    def old_files(self, tile_id):
        return list(self.tiles.get(tile_id, {}).get("files", []))

    def _entry(self, tile_id, path, attempts):
        entry = self.tiles.setdefault(tile_id, {})
        stamp = now()
        entry.update({"path": path, "last_attempt": stamp, "attempts": entry.get("attempts", 0) + attempts})
        return entry, stamp

    def success(self, tile_id, path, files, last_modified, attempts, ovr_asked=False):
        """ovr_asked: overviews were requested (mapcache may have none: not asked again)."""
        entry, stamp = self._entry(tile_id, path, attempts)
        entry.update({
            "status": "ok", "error": None, "downloaded": stamp, "ovr_asked": ovr_asked,
            "dataset_last_modified": last_modified,
            "files": sorted(files),
            "bytes": sum(os.path.getsize(os.path.join(self.tiles_dir, f)) for f in files
                         if os.path.exists(os.path.join(self.tiles_dir, f))),
        })
        self.save()

    def nodata(self, tile_id, path, last_modified, attempts):
        entry, _ = self._entry(tile_id, path, attempts)
        entry.update({"status": "nodata", "error": None, "files": [],
                      "dataset_last_modified": last_modified})
        self.save()

    def failure(self, tile_id, path, error, attempts):
        entry, _ = self._entry(tile_id, path, attempts)
        entry.update({"status": "failed", "error": error})
        self.save()

    def processed(self, options, outputs):
        self.data["processing"] = {
            "when": now(), "options": options,
            "outputs": [os.path.relpath(p, os.path.dirname(self.path)) for p in outputs],
        }
        self.save()

    def save(self):
        self.data["updated"] = now()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
