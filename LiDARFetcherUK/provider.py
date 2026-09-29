"""Processing Toolbox provider: raster conversion that works on any raster."""
import os

from qgis.PyQt.QtGui import QIcon
from qgis.core import (QgsProcessingProvider, QgsProcessingAlgorithm, QgsProcessingException,
                       QgsProcessingParameterRasterLayer, QgsProcessingParameterEnum,
                       QgsProcessingParameterBoolean, QgsProcessingParameterFileDestination)

from . import raster_tools

ICON = os.path.join(os.path.dirname(__file__), 'icon.png')

# Formats offered by the algorithm, in enum order.
CONVERT_FORMATS = ["FLT", "AAIGrid", "GTiff"]


class LidarFetcherProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
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
