def classFactory(iface):
    from .plugin import LidarFetcherPlugin
    return LidarFetcherPlugin(iface)
