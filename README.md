# LiDAR Fetcher UK — QGIS Plugin

Download LiDAR tiles for England, Scotland and Wales for an area of interest straight into a folder, with optional post-processing.

## Installation

1. In QGIS: **Plugins → Manage and Install Plugins → Settings**
2. Under *Plugin Repositories*, click **Add**
3. Enter any name and this URL:
   ```
   https://ZedeN1.github.io/qgis-plugins/plugins.xml
   ```
4. Go to the **All** tab, search for *LiDAR Fetcher UK*, and click **Install**.

Works with QGIS 3.22+ and QGIS 4.

## Usage

1. **Web → LiDAR Fetcher UK** (or the toolbar icon).
2. Pick the area: a polygon layer (optionally selected features only) or an extent (canvas, layer or drawn).
3. **Find available datasets** searches all three sources and lists every product, year and resolution covering the area, newest first, with its source, tile count and last modified date. Tick the datasets you want, or use the tick box in the top-left of the table to tick all of them (tick *Show non-LiDAR products* for EA CASI and aerial photography). Columns can be resized by dragging.
4. Choose an output folder and options, then **Download**.

Each dataset goes into its own folder, e.g. `lidar_composite_dtm_2022_1m/`:

```
lidar_composite_dtm_2022_1m/
  lidar_fetcher.json                       record of every tile and the processing run
  tiles/                                   extracted tiles and their zips
  lidar_composite_dtm_2022_1m.vrt          Create VRT
  lidar_composite_dtm_2022_1m_merged.tif   Merge into one file (one per Convert to format)
  tif/ asc/ flt/                           Convert to, when not merging
```

Options (defaults in brackets):
 - `Create VRT` (on) - virtual mosaic of all tiles.
 - `Build pyramids (all levels)` (on) - external compressed `.ovr` for the VRT and merged files, or for every tile if neither is made.
 - `Merge into one file` (off) - single raster covering all tiles.
 - `Convert to` GeoTIFF / ASC / FLT (none) - tick any combination: compressed GeoTIFF, ASCII Grid (`.asc`, 3 d.p.), ESRI Float (`.flt` + `.hdr`). When merging, one merged file per ticked format (GeoTIFF if none); otherwise every tile is converted into a folder per format.
 - `Add results to map` (on).
 - `Delete downloaded zip files` (off) - kept zips let a tile be re-extracted without downloading it again.

### Resuming and re-running

`lidar_fetcher.json` records, for every tile, its URL, status (ok / failed), attempts, when it was last tried and downloaded, any error, the files it produced and their size, and the dataset's last modified date at the time. Re-running into the same folder uses it to fetch only what is needed:
 - new tiles, tiles that failed last time, and tiles whose download link changed are downloaded;
 - tiles whose files have gone are re-extracted from their kept zip, or downloaded again;
 - tiles downloaded before the dataset's last modified date moved on are replaced;
 - everything else is skipped.

A failing tile is retried twice (after 2 s and 5 s), then recorded as failed while the other tiles and datasets carry on; the log lists what failed. Folders from v0.01 (`tiles/_downloaded.txt`) are migrated automatically.

## Processing Toolbox

Two algorithms under **LiDAR Fetcher UK**:

- **Download LiDAR tiles**: the dialog's download without the dialog, for batch runs, models and scripts. Give a polygon layer (honours *Selected features only*) or an extent, pick datasets from the list of every product (EA, all Scottish collections, Welsh 2020-2023 and archive), choose *Latest year only* (per resolution) or *All years*, optionally filter resolutions (`1m`, `50cm, 1m`; leave empty to include point clouds), and set the same options as the dialog. Uses the same cache, `lidar_fetcher.json` resume and retries, so dialog and Processing runs can share an output folder. Loads the VRT / merged files when finished and reports the number of failed tiles.
- **Convert raster to FLT / ASC / GeoTIFF**: converts any raster, with an optional pyramids step. Use *Run as Batch Process* for many files. FLT headers are written from the raster's outer corner (no half-cell shift).

Example (Python console):

```python
processing.run("lidarfetcheruk:downloadlidar", {
    "AREA": "my_polygons", "DATASETS": ["EA - LIDAR Composite DTM"], "YEARS": 0,
    "RESOLUTIONS": "1m", "MERGE": True, "CONVERT": ["FLT"], "OUTPUT": "C:/LiDAR"})
```

## Data sources

| Source | Service | Datasets | Tiles |
|--------|---------|----------|-------|
| EA (England) | Environment Agency survey API | Composite DTM/DSM, time-stamped tiles, National LiDAR Programme, point clouds | 5 km zips |
| Scotland | Scottish Remote Sensing Portal API | Phases 1-6, National LiDAR Programme 2025, Outer Hebrides 2019, Orkney 2023, HES projects (DTM, DSM, LAZ) | GeoTIFF / LAZ files |
| Wales | DataMapWales WFS tile catalogues | Welsh Government LiDAR 2020-2023 (1 m DTM/DSM), NRW archive 1998-2015 | 1 km GeoTIFFs / 10 km zips |

Notes:
 - The NRW archive stores heights in millimetres as ASCII grids with no projection. They are converted on download to GeoTIFFs in metres (EPSG:27700).
 - Multi-part areas are dissolved and searched part by part, and detailed outlines are simplified (with a small outward buffer) to stay under the APIs' size limits. Welsh tiles that only touch the area are dropped.
 - Only the sources whose country overlaps the area are searched for tiles.
 - A source that is down is reported in the log and skipped, so the others still return results.

**Last modified** is the most recent change of any kind to the dataset or its metadata, since reprocessing can change data long after it was flown:
 - EA: latest of the data.gov.uk metadata and revision dates (the same as `last_any_modified` in the Gov_API LiDAR monitor; the DEFRA catalogue records add nothing newer).
 - Scotland: latest of the collection's metadata and reference dates and any tile's metadata date.
 - Wales: the DataMapWales tile catalogue's last updated date.

### Dataset info cache

Dataset names and last modified dates (not the tiles, which depend on the area) are kept in `<QGIS profile>\LiDARFetcherUK\catalogue_cache.json`, e.g. `%APPDATA%\QGIS\QGIS3\profiles\default\LiDARFetcherUK\`, and shown under the results table with their age.
 - **Find available datasets** uses the cache, so only the tile search goes online. A source's info is fetched when it has none cached, or once its cache is older than 30 days.
 - **Refresh metadata** refetches it for all three sources now, and updates the dates in a table already shown (ticks are kept).
 - If a service does not answer within 5 seconds (data.gov.uk sometimes takes 10-15 s), the cached copy is kept and the log says so.
