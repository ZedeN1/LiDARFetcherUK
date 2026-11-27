"""Processing Toolbox provider: LiDAR download, and raster conversion for any raster."""
import os
import re

from qgis.PyQt.QtGui import QIcon
from qgis.core import (QgsProcessing, QgsProcessingProvider, QgsProcessingAlgorithm,
                       QgsProcessingException, QgsProcessingContext, QgsGeometry,
                       QgsProcessingParameterRasterLayer, QgsProcessingParameterEnum,
                       QgsProcessingParameterBoolean, QgsProcessingParameterFileDestination,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterExtent,
                       QgsProcessingParameterFolderDestination,
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


class DownloadLidarAlgorithm(QgsProcessingAlgorithm):
    """Same mapcache search, manifest and processing as the dialog, for batch runs and models."""

    AREA = 'AREA'
    EXTENT = 'EXTENT'
    DATASETS = 'DATASETS'
    VRT = 'VRT'
    PYRAMIDS = 'PYRAMIDS'
    MERGE = 'MERGE'
    CONVERT = 'CONVERT'
    LOAD = 'LOAD'
    OUTPUT = 'OUTPUT'
    FAILED = 'FAILED_TILES'

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
        return ("Downloads LiDAR DTM tiles from the EVY LiDAR cache (mapcache, EVY network only) "
                "covering a polygon layer or an extent, with the same options as the LiDAR "
                "Fetcher UK dialog.\n\n"
                "Datasets: the DTMs mapcache caches (EA LIDAR Composite DTM 2022 1 m and 2 m, "
                "Welsh Government LiDAR DTM 2020-2023, Scotland Phase 1-6 and the Scottish "
                "National LiDAR Programme).\n\n"
                "Each dataset goes into its own folder in the output folder, with "
                "lidar_fetcher.json recording every tile, so running again into the same folder "
                "only fetches what is missing, failed or updated.\n\n"
                "Convert to: one merged file per format when merging, otherwise every tile is "
                "converted into a folder per format.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.AREA, 'Area (polygon layer)', [POLYGON_SOURCE], optional=True))
        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT, 'Area as an extent (used when no polygon layer is given)', optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.DATASETS, 'Datasets', options=[label for _, label in catalogue.DATASETS],
            allowMultiple=True, usesStaticStrings=True))
        self.addParameter(QgsProcessingParameterBoolean(self.VRT, 'Create VRT', defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.PYRAMIDS, 'Build pyramids (all levels)', defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MERGE, 'Merge into one file', defaultValue=False))
        self.addParameter(QgsProcessingParameterEnum(
            self.CONVERT, 'Convert to', options=list(self.CONVERT_OPTIONS), allowMultiple=True,
            optional=True, usesStaticStrings=True))
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

        labels = {label: key for key, label in catalogue.DATASETS}
        # An empty optional static-string enum comes back as [''], not [].
        wanted = {labels[label] for label in self.parameterAsEnumStrings(parameters, self.DATASETS, context)
                  if label in labels}
        if not wanted:
            raise QgsProcessingException('Choose at least one dataset.')
        area = self._area(parameters, context)
        out_dir = self.parameterAsString(parameters, self.OUTPUT, context)

        try:
            found = catalogue.search(area, feedback, log, wanted)
        except RuntimeError as e:
            raise QgsProcessingException(str(e))
        if feedback.isCanceled():
            return {}
        chosen = sorted((ds for ds in found.values() if ds.id in wanted), key=lambda d: d.id)
        if not chosen:
            feedback.reportError('None of the chosen datasets covers this area.')
            return {self.OUTPUT: out_dir, self.FAILED: 0}
        feedback.pushInfo(f"{len(chosen)} dataset(s), {sum(len(d.tiles) for d in chosen)} tile(s):")
        for ds in chosen:
            feedback.pushInfo(f"  {ds.source}: {ds.title} ({ds.resolution_label}), {len(ds.tiles)} tile(s)")

        options = {
            "vrt": self.parameterAsBoolean(parameters, self.VRT, context),
            "pyramids": self.parameterAsBoolean(parameters, self.PYRAMIDS, context),
            "merge": self.parameterAsBoolean(parameters, self.MERGE, context),
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
