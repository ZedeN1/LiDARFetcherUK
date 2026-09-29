import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
                                 QLabel, QPushButton, QRadioButton, QCheckBox, QComboBox,
                                 QTreeWidget, QTreeWidgetItem, QHeaderView, QProgressBar,
                                 QTextBrowser, QMessageBox)
from qgis.core import (QgsApplication, QgsProject, QgsSettings, QgsGeometry, QgsRasterLayer,
                       QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsMapLayerProxyModel)
from qgis.gui import QgsMapLayerComboBox, QgsExtentWidget, QgsFileWidget

from . import raster_tools
from .tasks import SearchTask, DownloadTask

SETTINGS = "LiDARFetcherUK/"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

# The search API rejects very detailed polygons, so simplify above this.
MAX_VERTICES = 500
# Ask before downloading more tiles than this (each is roughly 20 to 110 MB).
WARN_TILES = 50

try:
    from qgis.core import Qgis
    POLYGON_FILTER = Qgis.LayerFilter.PolygonLayer
except AttributeError:
    POLYGON_FILTER = QgsMapLayerProxyModel.Filter.PolygonLayer


def _to_coords(polygon):
    """QgsGeometry.asPolygon() rings to GeoJSON Polygon coordinates."""
    return [[[round(p.x(), 7), round(p.y(), 7)] for p in ring] for ring in polygon]


class LidarFetcherDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.task = None
        self.datasets = {}
        self.setWindowTitle("LiDAR Fetcher UK")
        self.resize(640, 720)

        layout = QVBoxLayout(self)

        # --- Area of interest ---
        area = QGroupBox("Area of interest")
        grid = QGridLayout(area)
        self.layer_radio = QRadioButton("Polygon layer")
        self.extent_radio = QRadioButton("Extent")
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(POLYGON_FILTER)
        self.selected_only = QCheckBox("Selected features only")
        self.extent_widget = QgsExtentWidget()
        self.extent_widget.setMapCanvas(iface.mapCanvas())
        self.extent_widget.toggleDialogVisibility.connect(self.setVisible)
        grid.addWidget(self.layer_radio, 0, 0)
        grid.addWidget(self.layer_combo, 0, 1)
        grid.addWidget(self.selected_only, 1, 1)
        grid.addWidget(self.extent_radio, 2, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.extent_widget, 2, 1)
        grid.setColumnStretch(1, 1)
        layout.addWidget(area)

        self.layer_radio.toggled.connect(self._update_area_widgets)
        (self.layer_radio if self.layer_combo.currentLayer() else self.extent_radio).setChecked(True)
        self._update_area_widgets()

        # --- Datasets ---
        search_row = QHBoxLayout()
        self.search_btn = QPushButton("Find available datasets")
        self.search_btn.clicked.connect(self.search)
        self.show_all = QCheckBox("Show non-LiDAR products")
        self.show_all.toggled.connect(self._filter_rows)
        search_row.addWidget(self.search_btn)
        search_row.addStretch()
        search_row.addWidget(self.show_all)
        layout.addLayout(search_row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Dataset", "Year", "Resolution", "Tiles", "Last modified"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 5):
            self.tree.header().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree, 1)

        # --- Output ---
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output folder"))
        self.folder = QgsFileWidget()
        self.folder.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        out_row.addWidget(self.folder, 1)
        layout.addLayout(out_row)

        opts = QGroupBox("Options")
        og = QGridLayout(opts)
        self.vrt_cb = QCheckBox("Create VRT")
        self.pyramids_cb = QCheckBox("Build pyramids (all levels)")
        self.merge_cb = QCheckBox("Merge into one file")
        self.keep_zips_cb = QCheckBox("Keep downloaded zip files")
        self.add_to_map_cb = QCheckBox("Add results to map")
        self.format_combo = QComboBox()
        for key, (label, _) in raster_tools.FORMATS.items():
            self.format_combo.addItem(label, key)
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Convert to"))
        fmt_row.addWidget(self.format_combo, 1)
        og.addWidget(self.vrt_cb, 0, 0)
        og.addWidget(self.pyramids_cb, 1, 0)
        og.addWidget(self.merge_cb, 2, 0)
        og.addLayout(fmt_row, 3, 0)
        og.addWidget(self.keep_zips_cb, 0, 1)
        og.addWidget(self.add_to_map_cb, 1, 1)
        self.format_combo.setToolTip("Applies to the merged file when merging, otherwise to every tile")
        layout.addWidget(opts)

        # --- Progress and log ---
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.log_box = QTextBrowser()
        self.log_box.setMaximumHeight(160)
        layout.addWidget(self.log_box)

        btns = QHBoxLayout()
        self.download_btn = QPushButton("Download")
        self.download_btn.clicked.connect(self.download)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btns.addStretch()
        btns.addWidget(self.download_btn)
        btns.addWidget(self.cancel_btn)
        btns.addWidget(close_btn)
        layout.addLayout(btns)

        self._load_settings()

    # ------------------------------------------------------------------ settings
    def _checkboxes(self):
        return {"vrt": self.vrt_cb, "pyramids": self.pyramids_cb, "merge": self.merge_cb,
                "keep_zips": self.keep_zips_cb, "add_to_map": self.add_to_map_cb}

    def _load_settings(self):
        s = QgsSettings()
        self.folder.setFilePath(s.value(SETTINGS + "folder", ""))
        defaults = {"vrt": True, "add_to_map": True}
        for key, cb in self._checkboxes().items():
            cb.setChecked(s.value(SETTINGS + key, defaults.get(key, False), type=bool))
        idx = self.format_combo.findData(s.value(SETTINGS + "format", ""))
        self.format_combo.setCurrentIndex(max(idx, 0))

    def _save_settings(self):
        s = QgsSettings()
        s.setValue(SETTINGS + "folder", self.folder.filePath())
        for key, cb in self._checkboxes().items():
            s.setValue(SETTINGS + key, cb.isChecked())
        s.setValue(SETTINGS + "format", self.format_combo.currentData())

    # ---------------------------------------------------------------------- area
    def _update_area_widgets(self):
        use_layer = self.layer_radio.isChecked()
        self.layer_combo.setEnabled(use_layer)
        self.selected_only.setEnabled(use_layer)
        self.extent_widget.setEnabled(not use_layer)
        if not use_layer:
            canvas = self.iface.mapCanvas()
            crs = canvas.mapSettings().destinationCrs()
            self.extent_widget.setOriginalExtent(canvas.extent(), crs)
            self.extent_widget.setCurrentExtent(canvas.extent(), crs)
            self.extent_widget.setOutputCrs(crs)

    def _area_polygons(self):
        """The area as a list of GeoJSON Polygon coordinates in WGS84."""
        project = QgsProject.instance()
        if self.layer_radio.isChecked():
            layer = self.layer_combo.currentLayer()
            if layer is None:
                raise ValueError("Choose a polygon layer.")
            feats = layer.getSelectedFeatures() if self.selected_only.isChecked() else layer.getFeatures()
            geoms = [f.geometry() for f in feats if f.hasGeometry()]
            if not geoms:
                raise ValueError("The layer has no (selected) polygons.")
            geom = QgsGeometry.unaryUnion(geoms)
            src_crs = layer.crs()
        else:
            rect = self.extent_widget.outputExtent()
            if rect.isEmpty():
                raise ValueError("Set an extent.")
            # Densify so the edges stay straight in the projected CRS after reprojection.
            geom = QgsGeometry.fromRect(rect).densifyByCount(20)
            src_crs = self.extent_widget.outputCrs()

        geom.transform(QgsCoordinateTransform(src_crs, WGS84, project))
        polygons = []
        for part in geom.asGeometryCollection():
            tol = 1e-5
            # Simplify detailed outlines, buffering by the same amount so no edge moves inwards.
            while part.constGet().nCoordinates() > MAX_VERTICES:
                part = part.simplify(tol).buffer(tol, 2)
                tol *= 2
            polygons.append(_to_coords(part.asPolygon()))
        return polygons

    # -------------------------------------------------------------------- search
    def search(self):
        try:
            polygons = self._area_polygons()
        except Exception as e:
            QMessageBox.warning(self, "LiDAR Fetcher UK", str(e))
            return
        self.tree.clear()
        self.log(f"Searching the Environment Agency catalogue ({len(polygons)} polygon(s))...")
        self._start(SearchTask(polygons, self._search_finished))

    def _search_finished(self, task, ok):
        self._task_done()
        if not ok:
            self._report_failure(task, "Search")
            return
        self.datasets = task.datasets
        self.tree.setSortingEnabled(False)
        for key, ds in self.datasets.items():
            item = QTreeWidgetItem([ds.product_label, ds.year, ds.resolution_label,
                                    f"{len(ds.tiles):>5}", ds.last_modified or ""])
            item.setData(0, Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            self.tree.addTopLevelItem(item)
        self.tree.setSortingEnabled(True)
        self.tree.sortItems(0, Qt.SortOrder.AscendingOrder)
        self._filter_rows()
        lidar = sum(ds.is_lidar for ds in self.datasets.values())
        self.log(f"Found {len(self.datasets)} dataset(s), {lidar} of them LiDAR.")
        if not self.datasets:
            self.log("Nothing found. The Environment Agency catalogue covers England only.")

    def _filter_rows(self):
        show_all = self.show_all.isChecked()
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            ds = self.datasets[item.data(0, Qt.ItemDataRole.UserRole)]
            item.setHidden(not (show_all or ds.is_lidar))

    # ------------------------------------------------------------------ download
    def _checked_datasets(self):
        chosen = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if not item.isHidden() and item.checkState(0) == Qt.CheckState.Checked:
                chosen.append(self.datasets[item.data(0, Qt.ItemDataRole.UserRole)])
        return chosen

    def download(self):
        chosen = self._checked_datasets()
        out_dir = self.folder.filePath()
        if not chosen:
            QMessageBox.warning(self, "LiDAR Fetcher UK", "Tick at least one dataset.")
            return
        if not out_dir:
            QMessageBox.warning(self, "LiDAR Fetcher UK", "Choose an output folder.")
            return
        tiles = sum(len(d.tiles) for d in chosen)
        if tiles > WARN_TILES:
            reply = QMessageBox.question(
                self, "LiDAR Fetcher UK",
                f"This will download {tiles} tiles (roughly 20 to 110 MB each). Continue?")
            if reply != QMessageBox.StandardButton.Yes:
                return
        os.makedirs(out_dir, exist_ok=True)
        self._save_settings()
        options = {key: cb.isChecked() for key, cb in self._checkboxes().items()}
        options["format"] = self.format_combo.currentData()
        self.log(f"Downloading {tiles} tile(s) to {out_dir}")
        self._start(DownloadTask(chosen, out_dir, options, self._download_finished))

    def _download_finished(self, task, ok):
        self._task_done()
        if not ok:
            self._report_failure(task, "Download")
            return
        self.progress.setValue(100)
        self.log("<b>Done.</b>")
        if self.add_to_map_cb.isChecked():
            self._add_to_map(task.outputs)

    def _add_to_map(self, outputs):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        for group_name, paths in outputs:
            if not paths:
                continue
            # Single mosaics go straight in; loose tiles get a group each.
            group = root.insertGroup(0, group_name) if len(paths) > 1 else None
            for path in paths:
                name = os.path.splitext(os.path.basename(path))[0]
                layer = QgsRasterLayer(path, name)
                if not layer.isValid():
                    self.log(f"Could not load {path}")
                    continue
                project.addMapLayer(layer, group is None)
                if group is not None:
                    group.addLayer(layer)

    # ---------------------------------------------------------------- task glue
    def _start(self, task):
        self.task = task
        task.message.connect(self.log)
        task.progressChanged.connect(lambda p: self.progress.setValue(int(p)))
        self.progress.setValue(0)
        self.search_btn.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        QgsApplication.taskManager().addTask(task)

    def _task_done(self):
        self.task = None
        self.search_btn.setEnabled(True)
        self.download_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    def _report_failure(self, task, what):
        if task.error:
            self.log(f"<span style='color:#c00'>{what} failed: "
                     f"{task.error.splitlines()[0]}</span>")
            QgsApplication.messageLog().logMessage(task.error, "LiDAR Fetcher UK")
        else:
            self.log(f"{what} cancelled.")

    def cancel(self):
        if self.task is not None:
            self.task.cancel()

    def cleanup(self):
        self.cancel()

    def log(self, text):
        self.log_box.append(text)
