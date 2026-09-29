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

# Collections as of 2026-09-29, so the Processing dataset list is complete before
# the first metadata fetch; newer ones are added from the cache.
KNOWN_COLLECTIONS = {
    "scotland-gov/lidar/national-lidar-programme/dtm": "Scottish National LiDAR Programme DTM",
    "scotland-gov/lidar/national-lidar-programme/dsm": "Scottish National LiDAR Programme DSM",
    "scotland-gov/lidar/national-lidar-programme/laz": "Scottish National LiDAR Programme LAZ",
    "scotland-gov/lidar/phase-1/dtm": "LiDAR for Scotland Phase I DTM",
    "scotland-gov/lidar/phase-1/dsm": "LiDAR for Scotland Phase I DSM",
    "scotland-gov/lidar/phase-1/laz": "LiDAR for Scotland Phase I LAS (LAZ)",
    "scotland-gov/lidar/phase-2/dtm": "LiDAR for Scotland Phase II DTM",
    "scotland-gov/lidar/phase-2/dsm": "LiDAR for Scotland Phase II DSM",
    "scotland-gov/lidar/phase-2/laz": "LiDAR for Scotland Phase II LAS (LAZ)",
    "scotland-gov/lidar/phase-3/dtm": "LiDAR for Scotland Phase III DTM",
    "scotland-gov/lidar/phase-3/dsm": "LiDAR for Scotland Phase III DSM",
    "scotland-gov/lidar/phase-3/laz": "LiDAR for Scotland Phase III LAS (LAZ)",
    "scotland-gov/lidar/phase-4/dtm": "LiDAR for Scotland Phase IV DTM",
    "scotland-gov/lidar/phase-4/dsm": "LiDAR for Scotland Phase IV DSM",
    "scotland-gov/lidar/phase-4/laz": "LiDAR for Scotland Phase IV LAS (LAZ)",
    "scotland-gov/lidar/phase-5/dtm": "LiDAR for Scotland Phase V DTM",
    "scotland-gov/lidar/phase-5/dsm": "LiDAR for Scotland Phase V DSM",
    "scotland-gov/lidar/phase-5/laz": "LiDAR for Scotland Phase V LAS (LAZ)",
    "scotland-gov/lidar/phase-6/dtm": "LiDAR for Scotland Phase VI DTM",
    "scotland-gov/lidar/phase-6/dsm": "LiDAR for Scotland Phase VI DSM",
    "scotland-gov/lidar/phase-6/laz": "LiDAR for Scotland Phase VI LAS (LAZ)",
    "scotland-gov/lidar/outerheb-2019/dtm/50cm": "LiDAR for Outer Hebrides 2019 - 50cm DTM",
    "scotland-gov/lidar/outerheb-2019/dsm/50cm": "LiDAR for Outer Hebrides 2019 - 50cm DSM",
    "scotland-gov/lidar/outerheb-2019/dtm/25cm": "LiDAR for Outer Hebrides 2019 - 25cm DTM",
    "scotland-gov/lidar/outerheb-2019/dsm/25cm": "LiDAR for Outer Hebrides 2019 - 25cm DSM",
    "scotland-gov/lidar/outerheb-2019/laz/4ppm": "LiDAR for Outer Hebrides 2019 - 4 PPM LAS (LAZ)",
    "scotland-gov/lidar/outerheb-2019/laz/16ppm": "LiDAR for Outer Hebrides 2019 - 16 PPM LAS (LAZ)",
    "scotland-gov/lidar/orkney-islands-council-23/dtm": "LiDAR for Orkney Islands Council 2023 DTM",
    "scotland-gov/lidar/orkney-islands-council-23/dsm": "LiDAR for Orkney Islands Council 2023 DSM",
    "scotland-gov/lidar/orkney-islands-council-23/laz": "LiDAR for Orkney Islands Council 2023 LAZ",
    "scotland-gov/lidar/hes/hes-2010/dtm": "HES LiDAR Data Stirling City and surrounding area (2010) DTM",
    "scotland-gov/lidar/hes/hes-2010/dsm": "HES LiDAR Data Stirling City and surrounding area (2010) DSM",
    "scotland-gov/lidar/hes/hes-2010/laz/dtm": "HES LiDAR Data Stirling City and surrounding area (2010) LAZ_DTM",
    "scotland-gov/lidar/hes/hes-2010/laz/dsm": "HES LiDAR Data Stirling City and surrounding area (2010) LAZ_DSM",
    "scotland-gov/lidar/hes/hes-2010s10/dtm": "LiDAR for Historic Environment Scotland Scottish Ten Project (2010) DTM",
    "scotland-gov/lidar/hes/hes-2010s10/dsm": "LiDAR for Historic Environment Scotland Scottish Ten Project (2010) DSM",
    "scotland-gov/lidar/hes/hes-2010s10/laz/dtm": "LiDAR for Historic Environment Scotland Scottish Ten Project (2010) LAZ_DTM",
    "scotland-gov/lidar/hes/hes-2010s10/laz/dsm": "LiDAR for Historic Environment Scotland Scottish Ten Project (2010) LAZ_DSM",
    "scotland-gov/lidar/hes/hes-2016/dtm": "LiDAR for Historic Environment Scotland Projects (2016) DTM",
    "scotland-gov/lidar/hes/hes-2016/dsm": "LiDAR for Historic Environment Scotland Projects (2016) DSM",
    "scotland-gov/lidar/hes/hes-2016-2017/dtm": "LiDAR for Historic Environment Scotland Projects (2016-2017 sub project 4) DTM",
    "scotland-gov/lidar/hes/hes-2016-2017/dsm": "LiDAR for Historic Environment Scotland Projects (2016-2017 sub project 4) DSM",
    "scotland-gov/lidar/hes/hes-2017/dtm": "LiDAR for Historic Environment Scotland Projects (2017) DTM",
    "scotland-gov/lidar/hes/hes-2017/dsm": "LiDAR for Historic Environment Scotland Projects (2017) DSM",
    "scotland-gov/lidar/hes/hes-2017sp3/dtm": "LiDAR for Historic Environment Scotland Project (2017 Sub Project 3) DTM",
    "scotland-gov/lidar/hes/hes-2017sp3/dsm": "LiDAR for Historic Environment Scotland Project (2017 Sub Project 3) DSM",
    "scotland-gov/lidar/hes/hes-2017sp3/laz": "LiDAR for Historic Environment Scotland Project (2017 Sub Project 3) LAZ",
    "scotland-gov/lidar/hes/hes-luing/dtm": "LiDAR for Historic Environment Scotland Projects Isle of Luing DTM",
    "scotland-gov/lidar/hes/hes-luing/dsm": "LiDAR for Historic Environment Scotland Projects Isle of Luing DSM",
    "scotland-gov/lidar/hes/hes-luing/laz": "LiDAR for Historic Environment Scotland Projects Isle of Luing LAZ",
}


def choices(metadata=None):
    """[(choice key, label)] for the Processing dataset list, one per collection."""
    collections = dict(KNOWN_COLLECTIONS)
    for name, meta in ((metadata or {}).get("collections") or {}).items():
        collections.setdefault(name, meta.get("title") or name)
    return [(f"{NAME}:{name}", f"{NAME} - {title}") for name, title in collections.items()]


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
    ds.choice_key = f"{NAME}:{collection}"
    return ds
