"""Environment Agency survey tiles (England).

The search endpoint takes a single WGS84 GeoJSON Polygon (no MultiPolygon, no
Feature wrapper) posted as application/geo+json, and returns every survey tile
it intersects. Each tile is a 5 km OS grid square served as a zip.
"""
import json
from urllib.parse import urlencode

from qgis.core import QgsRectangle

from .catalogue import METADATA_TIMEOUT, Dataset, request

NAME = "EA"
# Rough extent of England in EPSG:27700; searches outside it are skipped.
COVERAGE = QgsRectangle(0, 0, 700000, 680000)

SEARCH_URL = "https://environment.data.gov.uk/backend/catalog/api/tiles/collections/survey/search"
# Only the date fields; the full records with resources are ~30 times larger.
CKAN_SEARCH = ("https://ckan.publishing.service.gov.uk/api/action/package_search?" + urlencode({
    "q": "title:lidar", "fq": "organization:environment-agency", "rows": 1000,
    "fl": "metadata_modified,extras_guid,extras_metadata-date,extras_dataset-reference-date"}))

# Product ids that are LiDAR derived; everything else (CASI, aerial photography)
# is hidden unless the user asks for all survey products.
LIDAR_PREFIXES = ("lidar_", "national_lidar_programme_", "surfzone_")

# Catalogue record (data.gov.uk "guid") for each product, keyed (product id,
# resolution id), with resolution None matching any. The date is last_any_modified
# as seen by Gov_API/lidar_update_monitor.py on 2026-09-29, used when neither the
# live lookup nor the cache has one.
CATALOGUE = {
    ("lidar_composite_dtm", "1"): ("13787b9a-26a4-4775-8523-806d13af58fc", "2025-08-01"),
    ("lidar_composite_dtm", "2"): ("09ea3b37-df3a-4e8b-ac69-fb0842227b04", "2025-08-01"),
    ("lidar_composite_dtm", "10"): ("ce8fe7e7-bed0-4889-8825-19b042e128d2", "2025-08-01"),
    ("lidar_composite_first_return_dsm", "1"): ("df4e3ec3-315e-48aa-aaaf-b5ae74d7b2bb", "2025-07-25"),
    ("lidar_composite_first_return_dsm", "2"): ("54167602-36c8-4b2b-80ec-ebb6267b6b1e", "2025-08-01"),
    ("lidar_composite_last_return_dsm", "1"): ("9ba4d5ac-d596-445a-9056-dae3ddec0178", "2025-08-01"),
    ("lidar_composite_last_return_dsm", "2"): ("f083c5dc-504f-4428-9811-a1b2519fa279", "2025-08-01"),
    ("lidar_tiles_dtm", None): ("dbadf364-0192-4bcf-a223-f3d403f08682", "2026-04-07"),
    ("lidar_tiles_dsm", None): ("1021ecff-6549-4dbe-b8a0-48ae72a3c698", "2026-04-07"),
    ("lidar_point_cloud", None): ("094d4ec8-4c21-4aa6-817f-b7e45843c5e0", "2026-04-07"),
    ("national_lidar_programme_dtm", None): ("2e8d0733-4f43-48b4-9e51-631c25d1b0a9", "2025-09-12"),
    ("national_lidar_programme_dsm", None): ("2e8d0733-4f43-48b4-9e51-631c25d1b0a9", "2025-09-12"),
    ("national_lidar_programme_first_return_dsm", None): ("2e8d0733-4f43-48b4-9e51-631c25d1b0a9", "2025-09-12"),
    ("national_lidar_programme_intensity", None): ("2e8d0733-4f43-48b4-9e51-631c25d1b0a9", "2025-09-12"),
    ("national_lidar_programme_point_cloud", None): ("2e8d0733-4f43-48b4-9e51-631c25d1b0a9", "2025-09-12"),
    ("national_lidar_programme_vom", None): ("ecae3bef-1e1d-4051-887b-9dc613c928ec", "2025-08-01"),
}


def fetch_metadata(feedback=None):
    """Last modified date per catalogue record, from one data.gov.uk request.

    The latest of metadata_modified, metadata-date and the revision date, which
    matches last_any_modified in Gov_API/lidar_update_monitor.py. The DEFRA
    catalogue records are not needed: their data date always equals the
    revision date here, and their metadata date is never the later one.
    """
    dates = {}
    body = json.loads(request(CKAN_SEARCH, feedback=feedback, timeout=METADATA_TIMEOUT))
    for pkg in body["result"]["results"]:
        found = [pkg.get("metadata_modified"), pkg.get("metadata-date")]
        try:
            found += [d["value"] for d in json.loads(pkg.get("dataset-reference-date") or "[]")
                      if d.get("type") == "revision"]
        except ValueError:
            pass
        found = [str(d)[:10] for d in found if d]
        if pkg.get("guid") and found:
            dates[pkg["guid"]] = max(found)
    return {"dates": dates}


def search(area, metadata, feedback=None):
    """Datasets covering the area (a catalogue.Area), as a list.

    metadata is what fetch_metadata returned (live or cached), or None.
    """
    datasets = {}
    for part in area.wgs84_parts:
        if feedback is not None and feedback.isCanceled():
            break
        coords = [[[round(p.x(), 7), round(p.y(), 7)] for p in ring] for ring in part.asPolygon()]
        body = json.dumps({"type": "Polygon", "coordinates": coords}).encode()
        for result in json.loads(request(SEARCH_URL, body, feedback))["results"]:
            product_id = result["product"]["id"]
            ds = Dataset(NAME, product_id, result["product"]["label"], result["year"]["id"],
                         result["resolution"]["id"], result["resolution"]["label"],
                         is_lidar=product_id.startswith(LIDAR_PREFIXES))
            ds = datasets.setdefault(ds.key, ds)
            ds.tiles[result["tile"]["id"]] = result["uri"]

    dates = (metadata or {}).get("dates", {})
    for ds in datasets.values():
        entry = CATALOGUE.get((ds.product_id, ds.resolution_id)) or CATALOGUE.get((ds.product_id, None))
        if entry is not None:
            ds.last_modified = dates.get(entry[0]) or entry[1]
    return list(datasets.values())
