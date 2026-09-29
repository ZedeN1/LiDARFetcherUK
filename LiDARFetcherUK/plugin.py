import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsApplication

MENU = '&LiDAR Fetcher UK'


class LidarFetcherPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dialog = None
        self.provider = None

    def initProcessing(self):
        if self.provider is not None:
            return
        from .provider import LidarFetcherProvider
        self.provider = LidarFetcherProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        icon_path = os.path.join(self.plugin_dir, 'icon.png')
        self.action = QAction(QIcon(icon_path), 'LiDAR Fetcher UK', self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToWebMenu(MENU, self.action)

    def unload(self):
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
        if self.dialog is not None:
            self.dialog.cleanup()
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginWebMenu(MENU, self.action)

    def run(self):
        if self.dialog is None:
            from .dialog import LidarFetcherDialog
            self.dialog = LidarFetcherDialog(self.iface, self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
