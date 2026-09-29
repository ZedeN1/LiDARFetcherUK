"""Processing Toolbox provider: LiDAR download, and raster conversion for any raster."""
import os
import re

from qgis.PyQt.QtGui import QIcon
from qgis.core import (QgsProcessing, QgsProcessingProvider, QgsProcessingAlgorithm,
                       QgsProcessingException, QgsProcessingContext, QgsGeometry,
                       QgsProcessingParameterRasterLayer, QgsProcessingParameterEnum,
                       QgsProcessingParameterBoolean, QgsProcessingParameterFileDestination,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterExtent,
                       QgsProcessingParameterString, QgsProcessingParameterFolderDestination,
                       QgsProcessingOutputNumber)

from . import catalogue, raster_tools
from .tasks import Downloader

try:
    from qgis.core import Qgis
    POLYGON_SOURCE = Qgis.ProcessingSourceType.VectorPolygon
except AttributeError:
    POLYGON_SOURCE = QgsProcessing.TypeVectorPolygon

ICON = os.path.join(os.path.dirname(__file__), 'icon.png')

# Formats offered by the algorithm, in enum order.
CONVERT_FORMATS = ["FLT", "AAIGrid", "GTiff"]


class LidarFetcherProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
        self.addAlgorithm(DownloadLidarAlgorithm())
        self.addAlgorithm(ConvertRasterAlgorithm())

    def id(self):
        return 'lidarfetcheruk'

    def name(self):
        return 'LiDAR Fetcher UK'

    def icon(self):
        return QIcon(ICON)


class ConvertRasterAlgorithm(QgsProcessingAlgorithm):
    INPUT = 'INPUT'
    FORMAT = 'FORMAT'
    PYRAMIDS = 'PYRAMIDS'
    OUTPUT = 'OUTPUT'

    def createInstance(self):
        return ConvertRasterAlgorithm()

    def name(self):
        return 'convertraster'

    def displayName(self):
        return 'Convert raster to FLT / ASC / GeoTIFF'

    def icon(self):
        return QIcon(ICON)

    def shortHelpString(self):
        return ("Converts band 1 of a raster to ESRI Float (.flt + .hdr), ASCII Grid (.asc) "
                "or compressed GeoTIFF.\n\n"
                "FLT headers are written from the raster's outer corner, so the grid is not "
                "shifted by half a cell. FLT cells must be square: if the source pixels are "
                "not, the mean cell size is used and the drift is reported.\n\n"
                "ASCII Grids are written to millimetre precision (3 decimal places).\n\n"
                "Use 'Run as Batch Process' to convert many rasters at once.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(self.INPUT, 'Input raster'))
        self.addParameter(QgsProcessingParameterEnum(
            self.FORMAT, 'Output format',
            options=[raster_tools.FORMATS[f][0] for f in CONVERT_FORMATS], defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.PYRAMIDS, 'Build pyramids (all levels)', defaultValue=False))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, 'Output raster',
            'ESRI Float (*.flt);;ASCII Grid (*.asc);;GeoTIFF (*.tif)'))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException('Invalid input raster')
        fmt = CONVERT_FORMATS[self.parameterAsEnum(parameters, self.FORMAT, context)]
        output = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        # Make the extension match the chosen format, whatever was typed.
        ext = raster_tools.FORMATS[fmt][1]
        output = os.path.splitext(output)[0] + ext
        os.makedirs(os.path.dirname(output) or '.', exist_ok=True)

        feedback.pushInfo(f'Writing {output}')
        try:
            warning = raster_tools.convert(layer.source(), output, fmt, feedback.isCanceled)
            if warning:
                feedback.reportError(warning)
            if self.parameterAsBoolean(parameters, self.PYRAMIDS, context) and not feedback.isCanceled():
                feedback.pushInfo('Building pyramids')
                raster_tools.build_pyramids(output, feedback.isCanceled)
        except Exception as e:
            raise QgsProcessingException(str(e))

        return {self.OUTPUT: output}


def _metres(text):
    """'50cm' / '0.5m' / '1 m' -> metres, or None (point densities, 'N/A')."""
    match = re.fullmatch(r"\s*([\d.]+)\s*(cm|m)\s*", str(text).lower())
    if not match:
        return None
    value = float(match.group(1))
    return round(value / 100 if match.group(2) == "cm" else value, 4)


class DownloadLidarAlgorithm(QgsProcessingAlgorithm):
    """Same search, cache, manifest and processing as the dialog, for batch runs and models."""

    AREA = 'AREA'
    EXTENT = 'EXTENT'
    DATASETS = 'DATASETS'
    YEARS = 'YEARS'
    RESOLUTIONS = 'RESOLUTIONS'
    VRT = 'VRT'
    PYRAMIDS = 'PYRAMIDS'
    MERGE = 'MERGE'
    CONVERT = 'CONVERT'
    DELETE_ZIPS = 'DELETE_ZIPS'
    REFRESH = 'REFRESH'
    LOAD = 'LOAD'
    OUTPUT = 'OUTPUT'
    FAILED = 'FAILED_TILES'

    YEAR_OPTIONS = ["Latest year only", "All years"]
    # Convert to labels -> raster_tools format keys.
    CONVERT_OPTIONS = {"GeoTIFF": "GTiff", "ASC": "AAIGrid", "FLT": "FLT"}

    def createInstance(self):
        return DownloadLidarAlgorithm()

    def name(self):
        return 'downloadlidar'

    def displayName(self):
        return 'Download LiDAR tiles'

    def icon(self):
        return QIcon(ICON)

    def shortHelpString(self):
        return ("Downloads LiDAR tiles for England (Environment Agency), Scotland (Scottish Remote "
                "Sensing Portal) and Wales (DataMapWales) covering a polygon layer or an extent, "
                "with the same options as the LiDAR Fetcher UK dialog.\n\n"
                "Datasets: pick one or more products. Each is downloaded for every year and "
                "resolution that covers the area, or only its most recent year with 'Latest year "
                "only'. Resolutions narrows this further, e.g. '1m' or '50cm, 1m'; point clouds "
                "have no resolution, so leave it empty to include them.\n\n"
                "Each dataset goes into its own folder in the output folder, with "
                "lidar_fetcher.json recording every tile, so running again into the same folder "
                "only fetches what is missing, failed or updated. Welsh archive heights are "
                "converted from millimetres to metres.\n\n"
                "Convert to: one merged file per format when merging, otherwise every tile is "
                "converted into a folder per format.")

    def initAlgorithm(self, config=None):
        self.choices = catalogue.dataset_choices()
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.AREA, 'Area (polygon layer)', [POLYGON_SOURCE], optional=True))
        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT, 'Area as an extent (used when no polygon layer is given)', optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.DATASETS, 'Datasets', options=[label for _, label in self.choices],
            allowMultiple=True, usesStaticStrings=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.YEARS, 'Years', options=self.YEAR_OPTIONS, defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            self.RESOLUTIONS, 'Resolutions (e.g. 1m, 50cm; empty for all)', defaultValue='',
            optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.VRT, 'Create VRT', defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.PYRAMIDS, 'Build pyramids (all levels)', defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MERGE, 'Merge into one file', defaultValue=False))
        self.addParameter(QgsProcessingParameterEnum(
            self.CONVERT, 'Convert to', options=list(self.CONVERT_OPTIONS), allowMultiple=True,
            optional=True, usesStaticStrings=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DELETE_ZIPS, 'Delete downloaded zip files', defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.REFRESH, 'Refresh dataset info (names, dates) first', defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.LOAD, 'Load results (VRT / merged files) when finished', defaultValue=True))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT, 'Output folder'))
        self.addOutput(QgsProcessingOutputNumber(self.FAILED, 'Number of tiles that failed'))

    def _area(self, parameters, context):
        source = self.parameterAsSource(parameters, self.AREA, context)
        if source is not None:
            geoms = [f.geometry() for f in source.getFeatures() if f.hasGeometry()]
            if not geoms:
                raise QgsProcessingException('The area layer has no polygons.')
            return catalogue.Area(QgsGeometry.unaryUnion(geoms), source.sourceCrs(),
                                  context.transformContext())
        if not parameters.get(self.EXTENT):
            raise QgsProcessingException('Give an area: a polygon layer or an extent.')
        crs = self.parameterAsExtentCrs(parameters, self.EXTENT, context)
        geom = self.parameterAsExtentGeometry(parameters, self.EXTENT, context, crs)
        # Densify so the edges stay straight in the projected CRS after reprojection.
        return catalogue.Area(geom.densifyByCount(20), crs, context.transformContext())

    def processAlgorithm(self, parameters, context, feedback):
        def log(text):
            plain = re.sub(r"<[^>]+>", "", text)
            if "color:#c00" in text or "color:#b36b00" in text:
                feedback.pushWarning(plain)
            else:
                feedback.pushInfo(plain)

        labels = {label: key for key, label in self.choices}
        wanted = {labels[label] for label in self.parameterAsEnumStrings(parameters, self.DATASETS, context)
                  if label in labels}
        if not wanted:
            raise QgsProcessingException('Choose at least one dataset.')
        resolutions = set()
        for token in self.parameterAsString(parameters, self.RESOLUTIONS, context).split(','):
            if token.strip():
                value = _metres(token)
                if value is None:
                    raise QgsProcessingException(f"Resolution '{token.strip()}' not understood; "
                                                 f"use e.g. 1m or 50cm.")
                resolutions.add(value)
        area = self._area(parameters, context)
        out_dir = self.parameterAsString(parameters, self.OUTPUT, context)

        if self.parameterAsBoolean(parameters, self.REFRESH, context):
            catalogue.refresh_metadata(feedback, log)
        found = catalogue.search(area, feedback, log)
        if feedback.isCanceled():
            return {}

        chosen = [ds for ds in found.values() if ds.choice_key in wanted
                  and (not resolutions or _metres(ds.resolution_label) in resolutions)]
        if self.parameterAsEnum(parameters, self.YEARS, context) == 0:
            latest = {}
            for ds in chosen:
                group = (ds.choice_key, ds.resolution_id)
                if group not in latest or ds.year > latest[group].year:
                    latest[group] = ds
            chosen = list(latest.values())
        chosen.sort(key=lambda d: (d.choice_key, d.year, d.resolution_label))
        if not chosen:
            feedback.reportError('None of the chosen datasets covers this area '
                                 '(with the year and resolution filters).')
            return {self.OUTPUT: out_dir, self.FAILED: 0}
        feedback.pushInfo(f"{len(chosen)} dataset(s), {sum(len(d.tiles) for d in chosen)} tile(s):")
        for ds in chosen:
            feedback.pushInfo(f"  {ds.source}: {ds.product_label} {ds.year} {ds.resolution_label}")

        options = {
            "vrt": self.parameterAsBoolean(parameters, self.VRT, context),
            "pyramids": self.parameterAsBoolean(parameters, self.PYRAMIDS, context),
            "merge": self.parameterAsBoolean(parameters, self.MERGE, context),
            "delete_zips": self.parameterAsBoolean(parameters, self.DELETE_ZIPS, context),
            # An empty optional enum comes back as [''], not [].
            "formats": [self.CONVERT_OPTIONS[label] for label in
                        self.parameterAsEnumStrings(parameters, self.CONVERT, context) if label],
        }
        os.makedirs(out_dir, exist_ok=True)
        downloader = Downloader(chosen, out_dir, options, feedback, log, feedback.setProgress)
        downloader.work()
        if feedback.isCanceled():
            return {}

        if self.parameterAsBoolean(parameters, self.LOAD, context):
            for folder_name, paths in downloader.outputs:
                # Mosaics only: loose tiles could mean hundreds of layers.
                for path in paths:
                    if path.endswith(".vrt") or "_merged." in os.path.basename(path):
                        name = os.path.splitext(os.path.basename(path))[0]
                        context.addLayerToLoadOnCompletion(
                            path, QgsProcessingContext.LayerDetails(name, context.project(), self.OUTPUT))
        return {self.OUTPUT: out_dir, self.FAILED: len(downloader.failed)}
