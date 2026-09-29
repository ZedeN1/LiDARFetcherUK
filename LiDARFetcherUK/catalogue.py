"""Search every LiDAR source for an area and return a common list of datasets.

Sources, one module each:
  source_ea        Environment Agency survey tiles (England)
  source_scotland  Scottish Remote Sensing Portal (Scotland)
  source_wales     DataMapWales tile catalogues (Wales)

All requests go through QgsBlockingNetworkRequest so QGIS proxy and SSL
settings apply. They block, so only call these from a QgsTask.
"""
import threading

from qgis.core import (QgsBlockingNetworkRequest, QgsGeometry, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsFeedback)
from qgis.PyQt.QtCore import QUrl, QByteArray
from qgis.PyQt.QtNetwork import QNetworkRequest

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
BNG = QgsCoordinateReferenceSystem("EPSG:27700")

# Search APIs reject very detailed polygons, so simplify parts above this.
MAX_VERTICES = 500

# Dataset info requests give up after this many seconds and use the cache
# instead; data.gov.uk sometimes takes 10-15 s to answer.
METADATA_TIMEOUT = 5


class Dataset:
    """One product/year/resolution from one source, and the tiles covering the area."""

    def __init__(self, source, product_id, product_label, year, resolution_id, resolution_label,
                 is_lidar=True):
        self.source = source
        self.product_id = product_id
        self.product_label = product_label
        self.year = year
        self.resolution_id = resolution_id
        self.resolution_label = resolution_label
        self.is_lidar = is_lidar
        self.tiles = {}  # tile id -> download URL
        self.last_modified = None
        # Welsh archive grids store heights in millimetres; converted on download.
        self.mm_units = False
        # Matches the source's choices() entry, for the Processing dataset list.
        self.choice_key = f"{source}:{product_id}"

    @property
    def key(self):
        return self.source, self.product_id, self.year, self.resolution_id

    @property
    def folder_name(self):
        res = self.resolution_label if self.resolution_label != "N/A" else ""
        return "_".join(p for p in (self.product_id, self.year, res) if p)


class Area:
    """The area of interest, prepared for every source.

    wgs84_parts: simplified single polygons in WGS84 (EA and Scotland search one
    polygon at a time). bng: the full area in British National Grid (Wales).
    Build it on the main thread; it is read-only afterwards.
    """

    def __init__(self, geom, crs, transform_context):
        geom = QgsGeometry(geom)
        self.bng = QgsGeometry(geom)
        self.bng.transform(QgsCoordinateTransform(crs, BNG, transform_context))
        geom.transform(QgsCoordinateTransform(crs, WGS84, transform_context))

        self.wgs84_parts = []
        for part in geom.asGeometryCollection():
            tol = 1e-5
            # Simplify detailed outlines, buffering by the same amount so no edge moves inwards.
            while part.constGet().nCoordinates() > MAX_VERTICES:
                part = part.simplify(tol).buffer(tol, 2)
                tol *= 2
            self.wgs84_parts.append(part)


def request(url, data=None, feedback=None, content_type="application/geo+json", timeout=None):
    """GET (or POST when data is given) and return the body as bytes.

    Raises RuntimeError on failure, including cancellation: an aborted
    QgsBlockingNetworkRequest reports no error and an empty body, which would
    otherwise pass for a (empty) download.

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


def _warn(log, text):
    log(f"<span style='color:#b36b00'>{text}</span>")


def _sources():
    from . import source_ea, source_scotland, source_wales
    return source_ea, source_scotland, source_wales


def _refresh(source, store, feedback, log, why=None):
    """Fetch one source's dataset info into store; returns its (possibly old) entry."""
    from . import cache

    old = store.get(source.NAME)
    if why:
        log(f"{source.NAME}: {why}, fetching dataset info")
    try:
        store[source.NAME] = {"fetched": cache.now(), "metadata": source.fetch_metadata(feedback)}
    except Exception as e:
        if feedback is not None and feedback.isCanceled():
            raise
        if old:
            _warn(log, f"{source.NAME}: could not refresh dataset info ({e}); "
                       f"using cache from {cache.describe(old['fetched'])}")
        else:
            _warn(log, f"{source.NAME}: could not fetch dataset info ({e})")
    return store.get(source.NAME)


def _save(store, log):
    from . import cache

    try:
        cache.save(store)
    except OSError as e:
        _warn(log, f"Could not save the dataset info cache ({e})")


def dataset_choices():
    """[(choice key, label)] of every product from every source, for Processing.

    Labels are stable text ("EA - LIDAR Composite DTM") so saved models and
    scripts keep working; Scottish collections added since the plugin was
    written come from the cache.
    """
    from . import cache

    store = cache.load()
    choices = []
    for source in _sources():
        entry = store.get(source.NAME) or {}
        choices += source.choices(entry.get("metadata"))
    return choices


def refresh_metadata(feedback=None, log=print):
    """Refetch every source's dataset info (names, dates) and save the cache."""
    from . import cache

    store = cache.load()
    for source in _sources():
        if feedback is not None and feedback.isCanceled():
            return
        _refresh(source, store, feedback, log)
    _save(store, log)


def search(area, feedback=None, log=print):
    """Datasets from every source covering the area, as {key: Dataset}.

    Dataset info (names, dates) comes from the cache; it is fetched only when
    a source has none cached or the cache is older than cache.MAX_AGE_DAYS,
    otherwise refresh_metadata does it on request. Tiles are always searched
    live, but only for sources whose country overlaps the area. A source that
    fails is reported through log and skipped, so one service being down does
    not hide the others.
    """
    from . import cache

    cancelled = lambda: feedback is not None and feedback.isCanceled()
    store = cache.load()
    changed = False
    extent = area.bng.boundingBox()
    datasets = {}
    for source in _sources():
        if cancelled():
            break
        entry = store.get(source.NAME)
        if not entry:
            entry = _refresh(source, store, feedback, log, "no cached dataset info")
            changed = True
        elif cache.age_days(entry["fetched"]) > cache.MAX_AGE_DAYS:
            entry = _refresh(source, store, feedback, log,
                             f"cached dataset info older than {cache.MAX_AGE_DAYS} days")
            changed = True

        if cancelled() or not extent.intersects(source.COVERAGE):
            continue
        try:
            found = source.search(area, entry["metadata"] if entry else None, feedback)
        except Exception as e:
            if cancelled():
                break
            _warn(log, f"{source.NAME}: search failed ({e})")
            continue
        for ds in found:
            datasets[ds.key] = ds

    if changed:
        _save(store, log)
    return datasets
