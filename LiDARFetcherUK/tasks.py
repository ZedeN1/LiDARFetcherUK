"""Background tasks: catalogue search, and tile download plus post-processing."""
import glob
import os
import traceback
import zipfile

from qgis.core import QgsTask, QgsFeedback
from qgis.PyQt.QtCore import pyqtSignal

from . import catalogue, raster_tools

RASTER_PATTERNS = ("*.tif", "*.tiff", "*.asc")
DONE_FILE = "_downloaded.txt"


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
    def __init__(self, polygons, on_finished):
        super().__init__("LiDAR Fetcher: searching catalogue", on_finished)
        self.polygons = polygons
        self.datasets = {}

    def work(self):
        self.datasets = catalogue.search(self.polygons, self.feedback)
        catalogue.fill_last_modified(self.datasets.values(), self.feedback)


class DownloadTask(_Task):
    """Download each dataset into its own folder, then run the chosen processing.

    Layout: <out>/<product>_<year>_<res>/tiles/ holds the extracted tiles, and
    the VRT, merged raster and converted tiles sit alongside it.
    """

    def __init__(self, datasets, out_dir, options, on_finished):
        super().__init__("LiDAR Fetcher: downloading tiles", on_finished)
        self.datasets = datasets
        self.out_dir = out_dir
        self.options = options
        self.outputs = []  # (dataset folder name, [paths to add to the map])
        self.total = sum(len(d.tiles) for d in datasets)
        self.done = 0

    def cancelled(self):
        return self.isCanceled()

    def work(self):
        for ds in self.datasets:
            if self.isCanceled():
                return
            folder = os.path.join(self.out_dir, ds.folder_name)
            tiles_dir = os.path.join(folder, "tiles")
            os.makedirs(tiles_dir, exist_ok=True)
            self.log(f"<b>{ds.product_label} {ds.year} {ds.resolution_label}</b>: {len(ds.tiles)} tile(s)")
            self.download(ds, tiles_dir)
            if self.isCanceled():
                return
            self.outputs.append((ds.folder_name, self.process(ds, folder, tiles_dir)))

    def download(self, ds, tiles_dir):
        done_path = os.path.join(tiles_dir, DONE_FILE)
        done = set()
        if os.path.exists(done_path):
            with open(done_path) as f:
                done = {line.strip() for line in f if line.strip()}

        for tile_id, uri in sorted(ds.tiles.items()):
            if self.isCanceled():
                return
            if tile_id in done:
                self.log(f"  {tile_id}: already downloaded, skipped")
            else:
                self.log(f"  {tile_id}: downloading")
                data = catalogue.request(uri, feedback=self.feedback)
                zip_path = os.path.join(tiles_dir, f"{ds.folder_name}_{tile_id}.zip")
                with open(zip_path, "wb") as f:
                    f.write(data)
                with zipfile.ZipFile(zip_path) as z:
                    for member in z.infolist():
                        if member.is_dir():
                            continue
                        # Flatten any folders inside the zip into tiles_dir.
                        member.filename = os.path.basename(member.filename)
                        z.extract(member, tiles_dir)
                if not self.options["keep_zips"]:
                    os.remove(zip_path)
                with open(done_path, "a") as f:
                    f.write(tile_id + "\n")
            self.done += 1
            self.setProgress(100.0 * self.done / max(self.total, 1))

    def process(self, ds, folder, tiles_dir):
        opts = self.options
        rasters = sorted({p for pat in RASTER_PATTERNS for p in glob.glob(os.path.join(tiles_dir, pat))})
        if not rasters:
            self.log("  No rasters in this dataset (point cloud?), nothing to process")
            return []
        if not (opts["vrt"] or opts["merge"] or opts["pyramids"] or opts["format"]):
            return rasters

        name = ds.folder_name
        fmt = opts["format"]
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
            merge_fmt = fmt or "GTiff"
            ext = raster_tools.FORMATS[merge_fmt][1]
            merged = os.path.join(folder, name + "_merged" + ext)
            self.log(f"  Merging into {os.path.basename(merged)}")
            self.warn(raster_tools.convert(vrt_path, merged, merge_fmt, self.cancelled))
            outputs.append(merged)
            pyramid_targets.append(merged)
            if not opts["vrt"]:
                os.remove(vrt_path)
        elif fmt:
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
