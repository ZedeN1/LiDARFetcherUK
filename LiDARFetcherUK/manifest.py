"""Per-dataset download record, kept as lidar_fetcher.json in the dataset folder.

It records every tile (URL, status, attempts, when, files produced, errors) and
the processing that was run, and decides which tiles a new run must fetch:
new or previously failed tiles, tiles whose link changed or whose files have
gone, and tiles fetched before the dataset's last modified date moved on.
Saved after every tile, so an interrupted run resumes where it stopped.
"""
import json
import os

from .cache import now

FILE_NAME = "lidar_fetcher.json"
# Tile record written by v0.01, migrated on first use.
LEGACY_DONE_FILE = "_downloaded.txt"


class Manifest:
    def __init__(self, folder, ds, tiles_dir):
        self.path = os.path.join(folder, FILE_NAME)
        self.tiles_dir = tiles_dir
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {"created": now(), "tiles": {}}
        self.data.update({
            "source": ds.source, "product_id": ds.product_id, "product": ds.product_label,
            "year": ds.year, "resolution": ds.resolution_label,
            "dataset_last_modified": ds.last_modified,
        })
        self.tiles = self.data.setdefault("tiles", {})
        self._migrate_legacy()

    def _migrate_legacy(self):
        legacy = os.path.join(self.tiles_dir, LEGACY_DONE_FILE)
        if not os.path.exists(legacy):
            return
        with open(legacy, encoding="utf-8") as f:
            for tile_id in (line.strip() for line in f):
                if tile_id and tile_id not in self.tiles:
                    # No URL, date or file list was kept; trust it as done.
                    self.tiles[tile_id] = {"status": "ok", "migrated_from": LEGACY_DONE_FILE}
        self.save()
        os.remove(legacy)

    def check(self, tile_id, url, last_modified):
        """(action, reason) for one tile.

        action is "skip", "download", or "extract" when only the extracted
        files are gone and the kept zip can supply them again.
        """
        entry = self.tiles.get(tile_id)
        if entry is None:
            return "download", None
        if entry.get("status") != "ok":
            return "download", "retrying, failed last time"
        if entry.get("url") and entry["url"] != url:
            return "download", "download link changed"
        fetched_for = entry.get("dataset_last_modified")
        if last_modified and fetched_for and last_modified > fetched_for:
            return "download", f"dataset updated since ({fetched_for} to {last_modified})"
        missing = [f for f in entry.get("files", [])
                   if not os.path.exists(os.path.join(self.tiles_dir, f))]
        if missing:
            zip_name = entry.get("zip")
            if zip_name and os.path.exists(os.path.join(self.tiles_dir, zip_name)):
                return "extract", f"{len(missing)} file(s) missing, re-extracting from {zip_name}"
            return "download", f"{len(missing)} file(s) missing"
        when = (entry.get("downloaded") or "")[:10]
        return "skip", f"already downloaded{' ' + when if when else ''}"

    def zip_name(self, tile_id):
        return self.tiles.get(tile_id, {}).get("zip")

    def old_files(self, tile_id):
        return list(self.tiles.get(tile_id, {}).get("files", []))

    def _entry(self, tile_id, url):
        entry = self.tiles.setdefault(tile_id, {})
        entry.pop("migrated_from", None)
        entry["url"] = url
        return entry

    def success(self, tile_id, url, files, last_modified, attempts, zip_name=None):
        """Record a download (attempts > 0) or a re-extraction from the kept zip (0)."""
        entry = self._entry(tile_id, url)
        stamp = now()
        if attempts:
            entry.update({"downloaded": stamp, "last_attempt": stamp,
                          "attempts": entry.get("attempts", 0) + attempts,
                          "zip": zip_name if zip_name and os.path.exists(
                              os.path.join(self.tiles_dir, zip_name)) else None})
        else:
            entry["extracted"] = stamp
        entry.update({
            "status": "ok", "error": None,
            "dataset_last_modified": last_modified,
            "files": sorted(files),
            "bytes": sum(os.path.getsize(os.path.join(self.tiles_dir, f)) for f in files
                         if os.path.exists(os.path.join(self.tiles_dir, f))),
        })
        self.save()

    def failure(self, tile_id, url, error, attempts):
        entry = self._entry(tile_id, url)
        entry.update({"status": "failed", "last_attempt": now(), "error": error,
                      "attempts": entry.get("attempts", 0) + attempts})
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
