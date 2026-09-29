"""Scottish Remote Sensing Portal (Scotland).

Collections (e.g. scotland-gov/lidar/phase-3/dtm) are listed by
/search/collection, and /search/product takes a WKT footprint in WGS84 and
returns products with a direct GeoTIFF or LAZ link on a public S3 bucket.
The API also accepts a bbox, but ignores it, so always send a footprint.
"""
import json
import re

from qgis.core import QgsRectangle

from .catalogue import METADATA_TIMEOUT, Dataset, request

NAME = "Scotland"
# Rough extent of Scotland in EPSG:27700; searches outside it are skipped.
COVERAGE = QgsRectangle(0, 520000, 700000, 1250000)
API = "https://api.remotesensing.data.gov.scot"
PAGE = 1000

# Resolution or point density in a product name, e.g. ns66_1m_dtm_phase1, ns5663_4ppm_las_phase5.
RES_RE = re.compile(r"_(\d+(?:cm|m|ppm))_", re.IGNORECASE)

# Collection metadata fields kept in the cache.
KEEP = ("title", "temporalExtent", "metadataDate", "datasetReferenceDate")


def fetch_metadata(feedback=None):
    """Every LiDAR collection with its title and dates."""
    body = json.loads(request(API + "/search/collection/scotland-gov/*", feedback=feedback,
                              timeout=METADATA_TIMEOUT))
    # The OGC collection holds web services, not downloads.
    collections = {c["name"]: {k: v for k, v in (c.get("metadata") or {}).items() if k in KEEP}
                   for c in body["result"]
                   if "/lidar/" in c["name"] and not c["name"].endswith("/ogc")}
    return {"collections": collections}


def _years(metadata):
    """Survey years from the temporal extent; some records have begin and end swapped."""
    extent = metadata.get("temporalExtent") or {}
    years = sorted({str(v)[:4] for v in (extent.get("begin"), extent.get("end")) if v})
    if not years:
        return ""
    return years[0] if len(years) == 1 else f"{years[0]}-{years[-1]}"


def _date(value):
    return str(value)[:10] if value else None


def search(area, metadata, feedback=None):
    """Datasets covering the area (a catalogue.Area), as a list.

    metadata is what fetch_metadata returned (live or cached); the product
    search needs its collection names.
    """
    collections = (metadata or {}).get("collections")
    if not collections:
        raise RuntimeError("no collection list, live or cached")
    datasets = {}
    tile_dates = {}
    for part in area.wgs84_parts:
        offset = 0
        while True:
            if feedback is not None and feedback.isCanceled():
                return []
            query = {"collections": list(collections), "footprint": part.asWkt(7),
                     "limit": PAGE, "offset": offset}
            results = json.loads(request(API + "/search/product", json.dumps(query).encode(),
                                         feedback, "application/json"))["result"]
            for product in results:
                ds = _dataset(product, collections)
                ds = datasets.setdefault(ds.key, ds)
                ds.tiles[product["name"]] = product["data"]["product"]["http"]["url"]
                tile_date = _date((product.get("metadata") or {}).get("metadataDate"))
                if tile_date:
                    tile_dates[ds.key] = max(tile_date, tile_dates.get(ds.key, tile_date))
            if len(results) < PAGE:
                break
            offset += PAGE

    # Latest change of any kind: collection metadata, reference date or any tile's metadata.
    for key, ds in datasets.items():
        meta = collections.get(ds.collection, {})
        found = [_date(meta.get("metadataDate")), _date(meta.get("datasetReferenceDate")),
                 tile_dates.get(key)]
        found = [d for d in found if d]
        ds.last_modified = max(found) if found else None
    return list(datasets.values())


def _dataset(product, collections):
    collection = product["collectionName"]
    meta = collections.get(collection, {})
    match = RES_RE.search(product["name"])
    res = match.group(1).lower() if match else "N/A"

    # scotland-gov/lidar/outerheb-2019/dtm/50cm -> scotland_outerheb-2019_dtm
    slug = collection.split("/lidar/", 1)[-1].replace("/", "_")
    if slug.endswith("_" + res):
        slug = slug[:-len(res) - 1]
    ds = Dataset(NAME, "scotland_" + slug, meta.get("title") or collection, _years(meta), res, res)
    ds.collection = collection
    return ds
