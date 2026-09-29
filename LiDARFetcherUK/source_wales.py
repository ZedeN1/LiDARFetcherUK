"""DataMapWales LiDAR tile catalogues (Wales).

Two WFS index layers on the DataMapWales GeoServer, queried by a British
National Grid bbox:
  - Welsh Government LiDAR 2020-2023: 1 km tiles, each linking straight to a
    1 m DTM and DSM GeoTIFF.
  - NRW LiDAR archive 1998-2015: 2 km index squares whose links point at 10 km
    zips of ASCII grids in millimetres, so many squares share one zip.
The bbox also returns tiles that only touch the area, so every tile is checked
against the real outline.
"""
import json
import os
from urllib.parse import urlencode, urlparse

from qgis.core import QgsGeometry, QgsRectangle

from .catalogue import METADATA_TIMEOUT, Dataset, request

NAME = "Wales"
# Rough extent of Wales in EPSG:27700; searches outside it are skipped.
COVERAGE = QgsRectangle(140000, 160000, 370000, 400000)
WFS = "https://datamap.gov.wales/geoserver/wfs"
RESOURCE = "https://datamap.gov.wales/api/v2/resources/{}"
PAGE = 5000

NATIONAL = "geonode:welsh_government_lidar_tile_catalogue_2020_2023"
ARCHIVE = "geonode:nrw_lidar_tile_catalogue_archive"

# DataMapWales resource id and fallback last-updated date for each layer.
LAYER_RECORDS = {NATIONAL: (6986, "2024-10-14"), ARCHIVE: (6988, "2024-10-02")}


def fetch_metadata(feedback=None):
    """Last updated date of each tile catalogue layer."""
    dates = {}
    for layer, (record_id, _) in LAYER_RECORDS.items():
        res = json.loads(request(RESOURCE.format(record_id), feedback=feedback,
                                     timeout=METADATA_TIMEOUT))["resource"]
        found = [str(res.get(k))[:10] for k in ("last_updated", "date") if res.get(k)]
        if found:
            dates[layer] = max(found)
    return {"dates": dates}


def _features(layer, rect, feedback):
    """All features of layer whose bbox meets rect (EPSG:27700), across pages."""
    start = 0
    while True:
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                  "typeNames": layer, "outputFormat": "application/json",
                  "srsName": "EPSG:27700", "count": PAGE, "startIndex": start,
                  "bbox": f"{rect.xMinimum()},{rect.yMinimum()},{rect.xMaximum()},{rect.yMaximum()},EPSG:27700"}
        features = json.loads(request(f"{WFS}?{urlencode(params)}", feedback=feedback))["features"]
        yield from features
        if len(features) < PAGE:
            return
        start += PAGE


def _tile_rect(feature):
    """Tiles are axis-aligned squares, so their bounding box is exact enough."""
    coords = feature["geometry"]["coordinates"]
    while isinstance(coords[0][0], list):
        coords = [pt for ring in coords for pt in ring]
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    return QgsGeometry.fromRect(QgsRectangle(min(xs), min(ys), max(xs), max(ys)))


def search(area, metadata, feedback=None):
    """Datasets covering the area (a catalogue.Area), as a list.

    metadata is what fetch_metadata returned (live or cached), or None.
    """
    dates = (metadata or {}).get("dates", {})
    engine = QgsGeometry.createGeometryEngine(area.bng.constGet())
    engine.prepareGeometry()
    datasets = {}

    def add(layer, kind, year, res, tile_id, url, label):
        ds = Dataset(NAME, label[0], label[1].format(kind=kind.upper()), year, res, res)
        ds.mm_units = layer == ARCHIVE
        ds = datasets.setdefault(ds.key, ds)
        ds.tiles[tile_id] = url
        ds.last_modified = dates.get(layer) or LAYER_RECORDS[layer][1]

    for part in area.bng.asGeometryCollection():
        rect = part.boundingBox()
        for layer in (NATIONAL, ARCHIVE):
            for feature in _features(layer, rect, feedback):
                if feedback is not None and feedback.isCanceled():
                    return []
                # Keep the QgsGeometry alive: constGet() on a temporary would dangle.
                tile = _tile_rect(feature)
                if not engine.intersects(tile.constGet()) or engine.touches(tile.constGet()):
                    continue
                props = feature["properties"]
                for kind in ("dtm", "dsm"):
                    if layer == NATIONAL:
                        link = (props.get(f"{kind}_link") or "").strip()
                        if link:
                            add(layer, kind, "2020-2023", "1m", props["british_gr"].strip(),
                                "https://" + link,
                                (f"wales_lidar_{kind}", "Welsh Government LiDAR {kind}"))
                    else:
                        link = (props.get(f"{kind}_url") or "").strip()
                        if link:
                            url = "https://" + link
                            # One zip per 10 km square, shared by many index squares.
                            tile_id = os.path.splitext(os.path.basename(urlparse(url).path))[0]
                            res = f"{float(props['resolution']):g}m"
                            add(layer, kind, str(props["year"]).strip(), res, tile_id, url,
                                (f"nrw_archive_{kind}", "NRW LiDAR archive {kind}"))
    return list(datasets.values())
