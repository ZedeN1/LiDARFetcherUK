def classFactory(iface):
    from .plugin import EvyLidarDownloaderPlugin
    return EvyLidarDownloaderPlugin(iface)
