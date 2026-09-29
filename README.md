# LiDAR Fetcher UK — QGIS Plugin

Download Environment Agency LiDAR tiles for an area of interest straight into a folder, with optional post-processing. Coverage is England only.

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

1. **Raster → LiDAR Fetcher UK** (or the toolbar icon).
2. Pick the area: a polygon layer (optionally selected features only) or an extent (canvas, layer or drawn).
3. **Find available datasets** lists every product, year and resolution covering the area, with tile count and the dataset's last modified date from the DEFRA catalogue. Tick the datasets you want (tick *Show non-LiDAR products* for CASI and aerial photography).
4. Choose an output folder and options, then **Download**.

Each dataset goes into its own folder, e.g. `lidar_composite_dtm_2022_1m/`:

```
lidar_composite_dtm_2022_1m/
  tiles/                                  extracted tiles (+ _downloaded.txt)
  lidar_composite_dtm_2022_1m.vrt         Create VRT
  lidar_composite_dtm_2022_1m_merged.flt  Merge into one file (format from Convert to)
  flt/ asc/ tif/                          Convert to, when not merging
```

Options:
 - `Create VRT` - virtual mosaic of all tiles.
 - `Build pyramids (all levels)` - external compressed `.ovr` for the VRT and merged file, or for every tile if neither is made.
 - `Merge into one file` - single raster covering all tiles.
 - `Convert to` - GeoTIFF (compressed), ASCII Grid (`.asc`, 3 d.p.) or ESRI Float (`.flt` + `.hdr`). Applies to the merged file when merging, otherwise to every tile.
 - `Keep downloaded zip files`, `Add results to map`.

Re-running into the same folder skips tiles already downloaded, so a cancelled download can be resumed.

## Processing Toolbox

**LiDAR Fetcher UK → Convert raster to FLT / ASC / GeoTIFF** converts any raster, with an optional pyramids step. Use *Run as Batch Process* for many files. FLT headers are written from the raster's outer corner (no half-cell shift).

## Data source

Tiles come from the Environment Agency survey API (`environment.data.gov.uk/backend/catalog/api/tiles/collections/survey/search`), which takes a single WGS84 polygon per request. Multi-part areas are dissolved and queried part by part, and detailed outlines are simplified (with a small outward buffer) to stay under the API's size limits.
