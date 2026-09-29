"""Background tasks: catalogue search, and tile download plus post-processing."""
import glob
import os
import time
import traceback
import zipfile
from urllib.parse import urlparse

from qgis.core import QgsTask, QgsFeedback
from qgis.PyQt.QtCore import pyqtSignal

from . import catalogue, raster_tools
from .manifest import Manifest

RASTER_PATTERNS = ("*.tif", "*.tiff", "*.asc")
ZIP_SIGNATURE = b"PK\x03\x04"
# Seconds to wait before each retry of a failed tile; the EA server
# occasionally answers 404 ("Cannot GET") for tiles that download fine moments
# later. Not rate limiting: it happens after a handful of requests too.
RETRY_WAITS = (2, 5)


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
        super().__init__("LiDAR Fetcher: searching catalogues", on_finished)
        self.area = area
        self.datasets = {}

    def work(self):
        self.datasets = catalogue.search(self.area, self.feedback, self.log)


class RefreshTask(_Task):
    def __init__(self, on_finished):
        super().__init__("LiDAR Fetcher: refreshing dataset info", on_finished)

    def work(self):
        catalogue.refresh_metadata(self.feedback, self.log)


class Downloader:
    """Download each dataset into its own folder, then run the chosen processing.

    Layout: <out>/<product>_<year>_<res>/tiles/ holds the extracted tiles, and
    the VRT, merged raster and converted tiles sit alongside it, with
    lidar_fetcher.json recording every tile and the processing (see manifest).

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
            self.log(f"<b>{ds.source}: {ds.product_label} {ds.year} {ds.resolution_label}</b>: "
                     f"{len(ds.tiles)} tile(s)")
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
        for tile_id, uri in sorted(ds.tiles.items()):
            if self.isCanceled():
                return
            action, reason = manifest.check(tile_id, uri, ds.last_modified)
            if action == "skip":
                self.log(f"  {tile_id}: {reason}, skipped")
            elif action == "extract":
                self.log(f"  {tile_id}: {reason}")
                zip_name = manifest.zip_name(tile_id)
                files = self.unpack(os.path.join(tiles_dir, zip_name), ds, tiles_dir, keep=True)
                manifest.success(tile_id, uri, files, ds.last_modified, 0)
            else:
                self.log(f"  {tile_id}: downloading{f' ({reason})' if reason else ''}")
                # A refetch replaces the tile, so clear what it produced last time.
                for name in manifest.old_files(tile_id):
                    path = os.path.join(tiles_dir, name)
                    if os.path.exists(path):
                        os.remove(path)
                attempts = [0]
                try:
                    files, zip_name = self.fetch_tile(ds, tile_id, uri, tiles_dir, attempts)
                except Exception as e:
                    if self.isCanceled():
                        return
                    # Recorded as failed, so the next run retries it.
                    error = str(e).splitlines()[0][:200]
                    manifest.failure(tile_id, uri, error, attempts[0])
                    self.failed.append((ds.folder_name, tile_id, error))
                    self.log(f"  <span style='color:#c00'>{tile_id}: failed ({error}), skipped</span>")
                else:
                    manifest.success(tile_id, uri, files, ds.last_modified, attempts[0], zip_name)
            self.done += 1
            self.setProgress(100.0 * self.done / max(self.total, 1))

    def fetch_tile(self, ds, tile_id, uri, tiles_dir, attempts):
        """Download one tile (retrying) and unpack it.

        Returns (file names, zip name or None). attempts is a one-item list
        counting the tries made, for the manifest.
        """
        for wait in RETRY_WAITS + (None,):
            attempts[0] += 1
            try:
                data = catalogue.request(uri, feedback=self.feedback)
                break
            except Exception as e:
                if self.isCanceled() or wait is None:
                    raise
                self.log(f"    attempt {attempts[0]} failed ({str(e).splitlines()[0][:120]}), "
                         f"retrying in {wait} s")
                self.sleep(wait)

        if data[:4] == ZIP_SIGNATURE:
            zip_name = f"{ds.folder_name}_{tile_id}.zip"
            zip_path = os.path.join(tiles_dir, zip_name)
            with open(zip_path, "wb") as f:
                f.write(data)
            keep = not self.options["delete_zips"]
            return self.unpack(zip_path, ds, tiles_dir, keep), zip_name if keep else None

        # Scottish and Welsh 2020-2023 tiles are the raster itself.
        name = os.path.basename(urlparse(uri).path) or tile_id
        with open(os.path.join(tiles_dir, name), "wb") as f:
            f.write(data)
        return [name], None

    def sleep(self, seconds):
        """Wait, but return straight away if the task is cancelled."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.isCanceled():
            time.sleep(0.1)

    def unpack(self, zip_path, ds, tiles_dir, keep):
        """Extract a tile zip into tiles_dir, flattening any folders.

        Millimetre grids are converted to metres. Returns the file names.
        """
        names = []
        with zipfile.ZipFile(zip_path) as z:
            for member in z.infolist():
                if member.is_dir():
                    continue
                member.filename = os.path.basename(member.filename)
                z.extract(member, tiles_dir)
                names.append(member.filename)
        if not keep:
            os.remove(zip_path)
        if ds.mm_units:
            names = self.to_metres(tiles_dir, names)
        return names

    def to_metres(self, tiles_dir, names):
        """Replace millimetre ASCII grids with metre GeoTIFFs; returns the new file names."""
        result = []
        for name in names:
            stem, ext = os.path.splitext(name)
            if ext.lower() != ".asc":
                result.append(name)
                continue
            dst = stem.replace("_mm_units", "") + ".tif"
            raster_tools.mm_to_metres(os.path.join(tiles_dir, name), os.path.join(tiles_dir, dst))
            os.remove(os.path.join(tiles_dir, name))
            result.append(dst)
        self.log("  Converted heights from millimetres to metres")
        return result

    def process(self, ds, folder, tiles_dir):
        opts = self.options
        rasters = sorted({p for pat in RASTER_PATTERNS for p in glob.glob(os.path.join(tiles_dir, pat))})
        if not rasters:
            self.log("  No rasters in this dataset (point cloud?), nothing to process")
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
            # With no mosaic to attach them to, build pyramids on every tile.
            for path in pyramid_targets or rasters:
                if self.isCanceled():
                    return outputs
                self.log(f"  Building pyramids for {os.path.basename(path)}")
                raster_tools.build_pyramids(path, self.cancelled)

        return outputs or rasters

    def warn(self, warning):
        if warning:
            self.log(f"  <span style='color:#b36b00'>Warning: {warning}</span>")


class DownloadTask(_Task):
    """Runs a Downloader in the background for the dialog."""

    def __init__(self, datasets, out_dir, options, on_finished):
        super().__init__("LiDAR Fetcher: downloading tiles", on_finished)
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
