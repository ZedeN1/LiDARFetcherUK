# LiDAR Fetcher UK — QGIS Plugin

Download LiDAR DTM tiles for England, Scotland and Wales for an area of interest straight into a folder, with optional post-processing. **For use inside EVY**: every download goes through the EVY LiDAR cache (mapcache), so a tile fetched once is local for everyone. Outside the EVY network the plugin cannot reach it.

For datasets the cache does not hold, or downloads straight from the publishers, use another plugin such as LIDAR Downloader UK.

## Installation

1. In QGIS: **Plugins → Manage and Install Plugins → Settings**
2. Under *Plugin Repositories*, click **Add**
3. Enter any name and this URL:
   ```
   https://ZedeN1.github.io/qgis-plugins/plugins.xml
   ```
4. Go to the **All** tab, search for *LiDAR Fetcher UK*, and click **Install**.

Works with QGIS 3.22+ and QGIS 4.

## Datasets

| Source | Dataset | Resolution |
|--------|---------|------------|
| EA (England) | LIDAR Composite DTM 2022 | 1 m, 2 m |
| Wales | Welsh Government LiDAR DTM 2020-2023 | 1 m |
| Scotland | LiDAR for Scotland Phase I, II DTM | 1 m |
| Scotland | LiDAR for Scotland Phase III-VI DTM | 50 cm |
| Scotland | Scottish National LiDAR Programme DTM | 50 cm |

## Usage

1. **Web → LiDAR Fetcher UK** (or the toolbar icon).
2. Pick the area: a polygon layer (optionally selected features only) or an extent (canvas, layer or drawn).
3. **Find available datasets** asks the cache which datasets cover the area: source, dataset, resolution, number of tiles, how many of them are already in the cache, and the dataset's last modified date. Tick the datasets you want, or use the tick box in the top-left of the table to tick all of them.
4. Choose an output folder and options, then **Download**.

Tiles not yet in the cache are fetched from the publisher by the cache first, which takes longer. Tiles the publisher does not have (sea along the coast, for example) are reported as *no data* and skipped.

Each dataset goes into its own folder, e.g. `ea_composite_dtm_2m/`:

```
ea_composite_dtm_2m/
  lidar_fetcher.json               record of every tile and the processing run
  tiles/                           the tiles' files, as published (plus .ovr overviews)
  ea_composite_dtm_2m.vrt          Create VRT
  ea_composite_dtm_2m_merged.tif   Merge into one file (one per Convert to format)
  tif/ asc/ flt/                   Convert to, when not merging
```

Options (defaults in brackets):
 - `Create VRT` (on) - virtual mosaic of all tiles.
 - `Build pyramids (all levels)` (on) - tiles come with the cache's ready-made `.ovr` overviews (Scottish tiles have them built in); the VRT and merged files get their own compressed `.ovr`.
 - `Merge into one file` (off) - single raster covering all tiles.
 - `Convert to` GeoTIFF / ASC / FLT (none) - tick any combination: compressed GeoTIFF, ASCII Grid (`.asc`, 3 d.p.), ESRI Float (`.flt` + `.hdr`). When merging, one merged file per ticked format (GeoTIFF if none); otherwise every tile is converted into a folder per format.
 - `Add results to map` (on).

### Resuming and re-running

`lidar_fetcher.json` records, for every tile, its cache path, status (ok / no data / failed), attempts, when it was last tried and downloaded, any error, the files it produced and their size, and the dataset's last modified date at the time. Re-running into the same folder fetches only what is needed:
 - new tiles, tiles that failed last time and tiles whose files have gone are downloaded;
 - tiles downloaded before the dataset's last modified date moved on are replaced;
 - everything else is skipped.

While the cache is busy fetching from the publisher, a tile is retried after 5, 15 and 30 s, then recorded as failed while the other tiles and datasets carry on; the log lists what failed.

## Processing Toolbox

Two algorithms under **LiDAR Fetcher UK**:

- **Download LiDAR tiles**: the dialog's download without the dialog, for batch runs, models and scripts. Give a polygon layer (honours *Selected features only*) or an extent, pick datasets, and set the same options as the dialog. Uses the same `lidar_fetcher.json` resume, so dialog and Processing runs can share an output folder. Loads the VRT / merged files when finished and reports the number of failed tiles.
- **Convert raster to FLT / ASC / GeoTIFF**: converts any raster, with an optional pyramids step. Use *Run as Batch Process* for many files. FLT headers are written from the raster's outer corner (no half-cell shift).

Example (Python console):

```python
processing.run("lidarfetcheruk:downloadlidar", {
    "AREA": "my_polygons", "DATASETS": ["EA - LIDAR Composite DTM 2022 1m"],
    "MERGE": True, "CONVERT": ["FLT"], "OUTPUT": "C:/LiDAR"})
```

## Cache address

The cache is `https://mapcache.niagara.ad.edenvaleyoung.com/lidar`. To use another, set it in the Python console:

```python
QgsSettings().setValue("LiDARFetcherUK/mapcache_url", "https://other-host/lidar")
```
