"""Background tasks: mapcache search, and tile download plus post-processing."""
import glob
import json
import os
import time
import traceback
from urllib.parse import urlparse

from qgis.core import QgsTask, QgsFeedback
from qgis.PyQt.QtCore import pyqtSignal

from . import catalogue, raster_tools
from .manifest import Manifest

RASTER_PATTERNS = ("*.tif", "*.tiff")
# Seconds to wait before each retry of a tile mapcache could not give: it is
# busy fetching from the publisher (503), the publisher failed (502), or the
# publisher is rate limiting (429). Mapcache retries the publisher itself too.
RETRY_WAITS = (5, 15, 30)
RETRY_STATUSES = {429, 500, 502, 503, 504}


class _Task(QgsTask):
    """QgsTask that logs to the dialog and cancels network requests with it."""

    message = pyqtSignal(str)

    def __init__(self, description, on_finished):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self.feedback = QgsFeedback()
        self.on_finished = on_finished
        self.error = None

    def cancel(self):
        self.feedback.cancel()
        super().cancel()

    def log(self, text):
        self.message.emit(text)

    def run(self):
        try:
            self.work()
            return not self.isCanceled()
        except Exception as e:
            if not self.isCanceled():
                self.error = f"{e}\n{traceback.format_exc()}"
            return False

    def finished(self, result):
        self.on_finished(self, result)


class SearchTask(_Task):
    def __init__(self, area, on_finished):
        super().__init__("EVY LIDAR Downloader: searching mapcache", on_finished)
        self.area = area
        self.datasets = {}

    def work(self):
        self.datasets = catalogue.search(self.area, self.feedback, self.log)


class NoData(Exception):
    """Mapcache answered 404: the publisher has no such tile."""


class Downloader:
    """Download each dataset into its own folder through mapcache, then run the chosen processing.

    Layout: <out>/<dataset name>/tiles/ holds the tiles' files, and the VRT,
    merged raster and converted tiles sit alongside it, with evy_lidar.json
    recording every tile and the processing (see manifest).

    Plain class shared by the dialog's DownloadTask and the Processing
    algorithm: feedback (a QgsFeedback) cancels it, log receives messages
    (with simple HTML) and progress receives 0-100.
    """

    def __init__(self, datasets, out_dir, options, feedback, log, progress):
        self.datasets = datasets
        self.out_dir = out_dir
        self.options = options
        self.feedback = feedback
        self.log = log
        self.setProgress = progress
        self.outputs = []  # (dataset folder name, [paths to add to the map])
        self.total = sum(len(d.tiles) for d in datasets)
        self.done = 0
        self.failed = []  # (dataset folder name, tile id, error)

    def isCanceled(self):
        return self.feedback.isCanceled()

    def cancelled(self):
        return self.feedback.isCanceled()

    def work(self):
        for ds in self.datasets:
            if self.isCanceled():
                return
            folder = os.path.join(self.out_dir, ds.folder_name)
            tiles_dir = os.path.join(folder, "tiles")
            os.makedirs(tiles_dir, exist_ok=True)
            self.log(f"<b>{ds.source}: {ds.title}</b> ({ds.resolution_label}): {len(ds.tiles)} tile(s)")
            manifest = Manifest(folder, ds, tiles_dir)
            self.download(ds, tiles_dir, manifest)
            if self.isCanceled():
                return
            outputs = self.process(ds, folder, tiles_dir)
            if self.isCanceled():
                return
            manifest.processed(self.options, outputs)
            self.outputs.append((ds.folder_name, outputs))

        if self.failed:
            self.log(f"<span style='color:#c00'>{len(self.failed)} tile(s) failed:</span>")
            for folder_name, tile_id, error in self.failed:
                self.log(f"<span style='color:#c00'>  {folder_name} {tile_id}: {error}</span>")
            self.log("Run the download again into the same folder to retry only the failed tiles.")

    def download(self, ds, tiles_dir, manifest):
        want_ovr = self.options["pyramids"] and ds.ovr
        for tile_id, tile in sorted(ds.tiles.items()):
            if self.isCanceled():
                return
            path = tile["path"]
            action, reason = manifest.check(tile_id, path, ds.last_modified, want_ovr)
            if action == "skip":
                self.log(f"  {tile_id}: {reason}, skipped")
            else:
                self.log(f"  {tile_id}: downloading{f' ({reason})' if reason else ''}")
                # A refetch replaces the tile, so clear what it produced last time.
                for name in manifest.old_files(tile_id):
                    p = os.path.join(tiles_dir, name)
                    if os.path.exists(p):
                        os.remove(p)
                attempts = [0]
                try:
                    files = self.fetch_tile(path, tiles_dir, want_ovr, attempts)
                except NoData:
                    manifest.nodata(tile_id, path, ds.last_modified, attempts[0])
                    self.log(f"  {tile_id}: no data at the publisher (e.g. sea), skipped")
                except Exception as e:
                    if self.isCanceled():
                        return
                    # Recorded as failed, so the next run retries it.
                    error = str(e).splitlines()[0][:200]
                    manifest.failure(tile_id, path, error, attempts[0])
                    self.failed.append((ds.folder_name, tile_id, error))
                    self.log(f"  <span style='color:#c00'>{tile_id}: failed ({error}), skipped</span>")
                else:
                    manifest.success(tile_id, path, files, ds.last_modified, attempts[0], want_ovr)
            self.done += 1
            self.setProgress(100.0 * self.done / max(self.total, 1))

    def get(self, url, attempts):
        """Body of url from mapcache, retrying while it is busy or the publisher pushes back."""
        for wait in RETRY_WAITS + (None,):
            attempts[0] += 1
            try:
                return catalogue.request(url, feedback=self.feedback)
            except Exception as e:
                status = catalogue.http_status(e)
                if status == 404:
                    raise NoData() from e
                if self.isCanceled() or wait is None or (status is not None and status not in RETRY_STATUSES):
                    raise
                self.log(f"    attempt {attempts[0]} failed ({str(e).splitlines()[0][:120]}), "
                         f"retrying in {wait} s")
                self.sleep(wait)

    def fetch_tile(self, path, tiles_dir, want_ovr, attempts):
        """Download one tile's files into tiles_dir; returns their names.

        EA tiles are a folder of files at mapcache (tif, tfw, xml, metadata
        gpkg, and the .ovr it built): get the list, then each file. Scottish
        and Welsh tiles are one GeoTIFF (Welsh ones with a .ovr beside it).
        """
        if path.startswith("ea/"):
            listing = json.loads(self.get(catalogue.file_url(path), attempts))
            wanted = [(f["name"], f["url"]) for f in listing["files"]
                      if want_ovr or not f.get("generated")]
        else:
            name = os.path.basename(urlparse(path).path)
            wanted = [(name, path)]
            if want_ovr:
                wanted.append((name + ".ovr", path + ".ovr"))
        names = []
        for name, url in wanted:
            if self.isCanceled():
                raise RuntimeError("cancelled")
            try:
                data = self.get(catalogue.file_url(url), attempts)
            except NoData:
                if name.lower().endswith(".ovr"):
                    continue            # overviews are optional
                raise
            self.write(os.path.join(tiles_dir, name), data)
            names.append(name)
        return names

    @staticmethod
    def write(path, data):
        """Write via a temporary name, so a half-written file never appears under the real one."""
        part = path + ".part"
        with open(part, "wb") as f:
            f.write(data)
        os.replace(part, path)

    def sleep(self, seconds):
        """Wait, but return straight away if the task is cancelled."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.isCanceled():
            time.sleep(0.1)

    def process(self, ds, folder, tiles_dir):
        opts = self.options
        rasters = sorted({p for pat in RASTER_PATTERNS for p in glob.glob(os.path.join(tiles_dir, pat))})
        if not rasters:
            self.log("  No rasters in this dataset, nothing to process")
            return []
        missing = sum(1 for f in self.failed if f[0] == ds.folder_name)
        if missing:
            self.log(f"  <span style='color:#b36b00'>{missing} tile(s) missing, "
                     f"so the VRT / merged file will have gaps</span>")
        formats = opts["formats"]
        if not (opts["vrt"] or opts["merge"] or opts["pyramids"] or formats):
            return rasters

        name = ds.folder_name
        outputs = []
        pyramid_targets = []

        vrt_path = os.path.join(folder, name + ".vrt")
        if opts["vrt"] or opts["merge"]:
            self.log(f"  Building VRT from {len(rasters)} raster(s)")
            raster_tools.build_vrt(rasters, vrt_path, self.cancelled)
            if opts["vrt"]:
                outputs.append(vrt_path)
                pyramid_targets.append(vrt_path)

        if opts["merge"]:
            # One merged file per chosen format; GeoTIFF when none is chosen.
            for fmt in formats or ["GTiff"]:
                if self.isCanceled():
                    return outputs
                merged = os.path.join(folder, name + "_merged" + raster_tools.FORMATS[fmt][1])
                self.log(f"  Merging into {os.path.basename(merged)}")
                self.warn(raster_tools.convert(vrt_path, merged, fmt, self.cancelled))
                outputs.append(merged)
                pyramid_targets.append(merged)
            if not opts["vrt"]:
                os.remove(vrt_path)
        else:
            for fmt in formats:
                ext = raster_tools.FORMATS[fmt][1]
                conv_dir = os.path.join(folder, ext.lstrip("."))
                os.makedirs(conv_dir, exist_ok=True)
                self.log(f"  Converting {len(rasters)} tile(s) to {raster_tools.FORMATS[fmt][0]}")
                for src in rasters:
                    if self.isCanceled():
                        return outputs
                    dst = os.path.join(conv_dir, os.path.splitext(os.path.basename(src))[0] + ext)
                    self.warn(raster_tools.convert(src, dst, fmt, self.cancelled))
                    outputs.append(dst)

        if opts["pyramids"]:
            if pyramid_targets:
                for path in pyramid_targets:
                    if self.isCanceled():
                        return outputs
                    self.log(f"  Building pyramids for {os.path.basename(path)}")
                    raster_tools.build_pyramids(path, self.cancelled)
            else:
                # No mosaic: tiles keep mapcache's .ovr (EA, Wales) or their own
                # internal overviews (Scottish COGs); build only where neither exists.
                todo = [p for p in rasters if not raster_tools.has_overviews(p)]
                if todo:
                    self.log(f"  Building pyramids for {len(todo)} tile(s)")
                for path in todo:
                    if self.isCanceled():
                        return outputs
                    raster_tools.build_pyramids(path, self.cancelled)

        return outputs or rasters

    def warn(self, warning):
        if warning:
            self.log(f"  <span style='color:#b36b00'>Warning: {warning}</span>")


class DownloadTask(_Task):
    """Runs a Downloader in the background for the dialog."""

    def __init__(self, datasets, out_dir, options, on_finished):
        super().__init__("EVY LIDAR Downloader: downloading tiles", on_finished)
        self.downloader = Downloader(datasets, out_dir, options, self.feedback, self.log,
                                     self.setProgress)

    @property
    def outputs(self):
        return self.downloader.outputs

    @property
    def failed(self):
        return self.downloader.failed

    def work(self):
        self.downloader.work()
