"""Environment Agency survey tile catalogue: search an area and fetch tiles.

The search endpoint takes a single WGS84 GeoJSON Polygon (no MultiPolygon, no
Feature wrapper) posted as application/geo+json, and returns every survey tile
it intersects. Each tile is a 5 km OS grid square served as a zip.

All requests go through QgsBlockingNetworkRequest so QGIS proxy and SSL
settings apply. They block, so only call these from a QgsTask.
"""
import json
from datetime import datetime, timezone

from qgis.core import QgsBlockingNetworkRequest
from qgis.PyQt.QtCore import QUrl, QByteArray
from qgis.PyQt.QtNetwork import QNetworkRequest

SEARCH_URL = "https://environment.data.gov.uk/backend/catalog/api/tiles/collections/survey/search"
DEFRA_RECORD = "https://environment.data.gov.uk/backend/catalog/api/catalog/data-sets/{}"

# Product ids that are LiDAR derived; everything else (CASI, aerial photography)
# is hidden unless the user asks for all survey products.
LIDAR_PREFIXES = ("lidar_", "national_lidar_programme_", "surfzone_")

# DEFRA catalogue record for each product, keyed (product id, resolution id),
# with resolution None matching any. The date is the last data revision seen by
# Gov_API/lidar_update_monitor.py on 2026-09-29, used when the live lookup fails.
CATALOGUE = {
    ("lidar_composite_dtm", "1"): ("13787b9a-26a4-4775-8523-806d13af58fc", "2023-12-15"),
    ("lidar_composite_dtm", "2"): ("09ea3b37-df3a-4e8b-ac69-fb0842227b04", "2023-12-15"),
    ("lidar_composite_dtm", "10"): ("ce8fe7e7-bed0-4889-8825-19b042e128d2", "2023-03-08"),
    ("lidar_composite_first_return_dsm", "1"): ("df4e3ec3-315e-48aa-aaaf-b5ae74d7b2bb", "2023-12-15"),
    ("lidar_composite_first_return_dsm", "2"): ("54167602-36c8-4b2b-80ec-ebb6267b6b1e", "2023-12-15"),
    ("lidar_composite_last_return_dsm", "1"): ("9ba4d5ac-d596-445a-9056-dae3ddec0178", "2023-12-15"),
    ("lidar_composite_last_return_dsm", "2"): ("f083c5dc-504f-4428-9811-a1b2519fa279", "2023-12-15"),
    ("lidar_tiles_dtm", None): ("dbadf364-0192-4bcf-a223-f3d403f08682", "2024-07-19"),
    ("lidar_tiles_dsm", None): ("1021ecff-6549-4dbe-b8a0-48ae72a3c698", "2024-07-19"),
    ("lidar_point_cloud", None): ("094d4ec8-4c21-4aa6-817f-b7e45843c5e0", "2024-07-19"),
    ("national_lidar_programme_dtm", None): ("2e8d0733-4f43-48b4-9e51-631c25d1b0a9", "2023-12-15"),
    ("national_lidar_programme_dsm", None): ("2e8d0733-4f43-48b4-9e51-631c25d1b0a9", "2023-12-15"),
    ("national_lidar_programme_first_return_dsm", None): ("2e8d0733-4f43-48b4-9e51-631c25d1b0a9", "2023-12-15"),
    ("national_lidar_programme_intensity", None): ("2e8d0733-4f43-48b4-9e51-631c25d1b0a9", "2023-12-15"),
    ("national_lidar_programme_point_cloud", None): ("2e8d0733-4f43-48b4-9e51-631c25d1b0a9", "2023-12-15"),
    ("national_lidar_programme_vom", None): ("ecae3bef-1e1d-4051-887b-9dc613c928ec", "2023-12-15"),
}

# Live DEFRA dates fetched this session, keyed by record id.
_live_dates = {}


class Dataset:
    """One product/year/resolution combination and the tiles covering the area."""

    def __init__(self, result):
        self.product_id = result["product"]["id"]
        self.product_label = result["product"]["label"]
        self.year = result["year"]["id"]
        self.resolution_id = result["resolution"]["id"]
        self.resolution_label = result["resolution"]["label"]
        self.tiles = {}
        self.last_modified = None

    @property
    def key(self):
        return self.product_id, self.year, self.resolution_id

    @property
    def is_lidar(self):
        return self.product_id.startswith(LIDAR_PREFIXES)

    @property
    def folder_name(self):
        res = self.resolution_label if self.resolution_id != "NaN" else ""
        return "_".join(p for p in (self.product_id, self.year, res) if p)


def request(url, data=None, feedback=None):
    """GET (or POST GeoJSON when data is given) and return the body as bytes."""
    req = QNetworkRequest(QUrl(url))
    # Tiles are up to ~110 MB; keep them out of the QGIS network cache.
    req.setAttribute(QNetworkRequest.Attribute.CacheSaveControlAttribute, False)
    blocking = QgsBlockingNetworkRequest()
    if data is None:
        err = blocking.get(req, True, feedback)
    else:
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/geo+json")
        err = blocking.post(req, QByteArray(data), True, feedback)

    if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        body = bytes(blocking.reply().content()).decode("utf-8", "replace")[:300]
        raise RuntimeError(f"{blocking.errorMessage()} {body}".strip())
    return bytes(blocking.reply().content())


def search(polygons, feedback=None):
    """Datasets covering any of the polygons, as {key: Dataset}.

    polygons is a list of GeoJSON Polygon coordinate arrays in WGS84.
    """
    datasets = {}
    for coords in polygons:
        if feedback is not None and feedback.isCanceled():
            break
        body = json.dumps({"type": "Polygon", "coordinates": coords}).encode()
        for result in json.loads(request(SEARCH_URL, body, feedback))["results"]:
            ds = Dataset(result)
            ds = datasets.setdefault(ds.key, ds)
            ds.tiles[result["tile"]["id"]] = result["uri"]
    return datasets


def catalogue_entry(product_id, resolution_id):
    return CATALOGUE.get((product_id, resolution_id)) or CATALOGUE.get((product_id, None))


def fill_last_modified(datasets, feedback=None):
    """Set last_modified on each dataset from the DEFRA catalogue record."""
    for ds in datasets:
        entry = catalogue_entry(ds.product_id, ds.resolution_id)
        if entry is None:
            continue
        record_id, fallback = entry
        if record_id not in _live_dates:
            try:
                record = json.loads(request(DEFRA_RECORD.format(record_id), feedback=feedback))
                modified = datetime.fromtimestamp(record["modified"] / 1000, tz=timezone.utc)
                _live_dates[record_id] = modified.strftime("%Y-%m-%d")
            except Exception:
                # Retired records return 403; keep the snapshot date instead.
                _live_dates[record_id] = None
        ds.last_modified = _live_dates[record_id] or fallback
