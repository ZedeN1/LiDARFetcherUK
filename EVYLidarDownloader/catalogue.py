"""Find the EVY mapcache's LiDAR DTM tiles for an area.

The plugin downloads only through the EVY LiDAR cache on niagara (internal):
mapcache answers which of its datasets cover an area, with their tiles and
last modified dates (/lidar/api/tiles), and serves the files. Anyone who needs
a dataset it does not cache, or a download straight from the publisher, uses
another plugin (e.g. LIDAR Downloader UK).

All requests go through QgsBlockingNetworkRequest so QGIS proxy and SSL
settings apply. They block, so only call these from a QgsTask.
"""
import json
import math
import threading
from urllib.parse import urlencode

from qgis.core import (QgsBlockingNetworkRequest, QgsGeometry, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsFeedback, QgsRectangle, QgsSettings)
from qgis.PyQt.QtCore import QUrl, QByteArray
from qgis.PyQt.QtNetwork import QNetworkRequest

BNG = QgsCoordinateReferenceSystem("EPSG:27700")

DEFAULT_URL = "https://mapcache.niagara.ad.edenvaleyoung.com/lidar"
URL_SETTING = "EVYLidarDownloader/mapcache_url"

# Mapcache answers at most 50,000 km2 per request: ask in squares of this size (metres).
CHUNK = 100000

# The datasets mapcache caches (its datasets.py ids), for the Processing list.
# Labels are stable text so saved models and scripts keep working.
DATASETS = [
    ("ea/lidar_composite_dtm/1", "EA - LIDAR Composite DTM 2022 1m"),
    ("ea/lidar_composite_dtm/2", "EA - LIDAR Composite DTM 2022 2m"),
    ("wales/wales_lidar_dtm", "Wales - Welsh Government LiDAR DTM 2020-2023 1m"),
    ("scot/lidar/phase-1/dtm", "Scotland - LiDAR for Scotland Phase I DTM 1m"),
    ("scot/lidar/phase-2/dtm", "Scotland - LiDAR for Scotland Phase II DTM 1m"),
    ("scot/lidar/phase-3/dtm", "Scotland - LiDAR for Scotland Phase III DTM 50cm"),
    ("scot/lidar/phase-4/dtm", "Scotland - LiDAR for Scotland Phase IV DTM 50cm"),
    ("scot/lidar/phase-5/dtm", "Scotland - LiDAR for Scotland Phase V DTM 50cm"),
    ("scot/lidar/phase-6/dtm", "Scotland - LiDAR for Scotland Phase VI DTM 50cm"),
    ("scot/lidar/national-lidar-programme/dtm", "Scotland - Scottish National LiDAR Programme DTM 50cm"),
]
SOURCES = {"ea": "EA", "wales": "Wales", "scot": "Scotland"}


def base_url():
    """Mapcache's /lidar address (setting EVYLidarDownloader/mapcache_url, else the EVY default)."""
    return (QgsSettings().value(URL_SETTING, "") or DEFAULT_URL).rstrip("/")


def file_url(path):
    """Full URL of a path mapcache gave: 'ea/...' (after /lidar/) or '/lidar/ea/...' (from the host)."""
    base = base_url()
    if path.startswith("/"):
        return base[:-len("/lidar")] + path if base.endswith("/lidar") else base + path
    return f"{base}/{path}"


def resolution_label(metres):
    return f"{metres * 100:g}cm" if metres < 1 else f"{metres:g}m"


class Dataset:
    """One cached dataset and the tiles of it covering the area (from /lidar/api/tiles)."""

    def __init__(self, entry):
        self.id = entry["id"]                  # e.g. ea/lidar_composite_dtm/2
        self.name = entry["name"]              # e.g. ea_composite_dtm_2m (its WCS coverage)
        self.title = entry["title"]            # the publisher's record title
        self.source = SOURCES.get(self.id.split("/", 1)[0], self.id)
        self.resolution = entry["resolution"]
        self.resolution_label = resolution_label(self.resolution)
        self.last_modified = entry.get("date")
        self.ovr = bool(entry.get("ovr"))      # mapcache offers .ovr overviews for these tiles
        self.tiles = {}                        # tile id -> {"path", "bounds", "cached", "bytes"?}

    @property
    def key(self):
        return self.id

    @property
    def folder_name(self):
        return self.name

    @property
    def cached(self):
        return sum(1 for t in self.tiles.values() if t.get("cached"))


class Area:
    """The area of interest in British National Grid. Build it on the main thread."""

    def __init__(self, geom, crs, transform_context):
        self.bng = QgsGeometry(geom)
        self.bng.transform(QgsCoordinateTransform(crs, BNG, transform_context))

    def chunks(self):
        """Boxes of at most CHUNK x CHUNK metres covering the area, skipping empty ones."""
        rect = self.bng.boundingBox()
        x = math.floor(rect.xMinimum() / CHUNK) * CHUNK
        while x < rect.xMaximum():
            y = math.floor(rect.yMinimum() / CHUNK) * CHUNK
            while y < rect.yMaximum():
                box = QgsRectangle(max(x, rect.xMinimum()), max(y, rect.yMinimum()),
                                   min(x + CHUNK, rect.xMaximum()), min(y + CHUNK, rect.yMaximum()))
                if self.bng.intersects(QgsGeometry.fromRect(box)):
                    yield box
                y += CHUNK
            x += CHUNK


def request(url, data=None, feedback=None, content_type="application/json", timeout=None):
    """GET (or POST when data is given) and return the body as bytes.

    Raises RuntimeError on failure, including cancellation: an aborted
    QgsBlockingNetworkRequest reports no error and an empty body, which would
    otherwise pass for a (empty) download. The error text starts with
    "HTTP <code>" when the server answered.

    timeout (seconds) aborts the request after that long; without it the QGIS
    network timeout applies. It works through a feedback of its own, since
    QNetworkRequest.setTransferTimeout is ignored under Qt 5.
    """
    req = QNetworkRequest(QUrl(url))
    # Tiles are up to ~110 MB; keep them out of the QGIS network cache.
    req.setAttribute(QNetworkRequest.Attribute.CacheSaveControlAttribute, False)
    if data is not None:
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, content_type)

    parent = feedback
    timer = None
    if timeout:
        feedback = QgsFeedback()
        if parent is not None:
            parent.canceled.connect(feedback.cancel)
        timer = threading.Timer(timeout, feedback.cancel)
        timer.start()

    blocking = QgsBlockingNetworkRequest()
    try:
        if data is None:
            err = blocking.get(req, True, feedback)
        else:
            err = blocking.post(req, QByteArray(data), True, feedback)
    finally:
        if timer is not None:
            timer.cancel()

    if parent is not None and parent.isCanceled():
        raise RuntimeError("cancelled")
    if feedback is not None and feedback.isCanceled():
        raise RuntimeError(f"no answer within {timeout} s")
    if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        reply = blocking.reply()
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status:
            reason = reply.attribute(QNetworkRequest.Attribute.HttpReasonPhraseAttribute) or ""
            raise RuntimeError(f"HTTP {status} {reason}".strip())
        raise RuntimeError(blocking.errorMessage())
    return bytes(blocking.reply().content())


def http_status(error):
    """The HTTP status in a request() error, or None."""
    text = str(error)
    if text.startswith("HTTP "):
        try:
            return int(text.split()[1])
        except (IndexError, ValueError):
            return None
    return None


def _warn(log, text):
    log(f"<span style='color:#b36b00'>{text}</span>")


def search(area, feedback=None, log=print, wanted=None):
    """Cached datasets covering the area, as {id: Dataset}, with only the tiles that overlap it.

    wanted: dataset ids to ask for (None = all). Sources mapcache could not
    search are reported through log; the others are still returned.
    """
    datasets = {}
    engine = QgsGeometry.createGeometryEngine(area.bng.constGet())
    engine.prepareGeometry()
    warned = set()
    for box in area.chunks():
        if feedback is not None and feedback.isCanceled():
            break
        params = {"bbox": f"{math.floor(box.xMinimum())},{math.floor(box.yMinimum())},"
                          f"{math.ceil(box.xMaximum())},{math.ceil(box.yMaximum())}"}
        if wanted:
            params["datasets"] = ",".join(sorted(wanted))
        url = f"{base_url()}/api/tiles?{urlencode(params)}"
        try:
            body = json.loads(request(url, feedback=feedback))
        except Exception as e:
            if feedback is not None and feedback.isCanceled():
                break
            raise RuntimeError(f"mapcache did not answer ({e}). It is reachable from the EVY "
                               f"network only: {base_url()}") from e
        for err in body.get("errors", []):
            text = f"{err.get('source')}: mapcache could not search it ({err.get('error')})"
            if text not in warned:
                warned.add(text)
                _warn(log, text)
        for entry in body.get("datasets", []):
            ds = datasets.setdefault(entry["id"], Dataset(entry))
            for tile in entry["tiles"]:
                bounds = tile.get("bounds")
                if bounds:
                    # Keep the QgsGeometry alive: constGet() on a temporary would dangle.
                    rect = QgsGeometry.fromRect(QgsRectangle(*bounds))
                    if not engine.intersects(rect.constGet()) or engine.touches(rect.constGet()):
                        continue
                ds.tiles[tile["id"]] = tile
    return {k: ds for k, ds in datasets.items() if ds.tiles}
