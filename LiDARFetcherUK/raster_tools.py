"""GDAL helpers: VRT, pyramids, and conversion to GeoTIFF, ASCII Grid and ESRI FLT.

Uses the GDAL Python bindings shipped with QGIS, since the GDAL command line
tools are not always on PATH in a QGIS install.
"""
import math
import os

from osgeo import gdal, osr

gdal.UseExceptions()

# Output formats: key -> (label, extension)
FORMATS = {
    "": ("Keep original", None),
    "GTiff": ("GeoTIFF (.tif)", ".tif"),
    "AAIGrid": ("ASCII Grid (.asc)", ".asc"),
    "FLT": ("ESRI Float (.flt)", ".flt"),
}

# Overviews stop once the longest side drops below this many pixels.
MIN_OVERVIEW_SIZE = 256

# Relative tolerance below which XDIM and YDIM count as identical.
SQUARE_TOL = 1e-12


def _progress(callback):
    """Adapt a () -> bool 'cancelled' check to a GDAL progress callback."""
    if callback is None:
        return None
    return lambda complete, message, data: 0 if callback() else 1


def build_vrt(sources, vrt_path, cancelled=None):
    ds = gdal.BuildVRT(vrt_path, sources, callback=_progress(cancelled))
    if ds is None:
        raise RuntimeError(f"Could not build {os.path.basename(vrt_path)}")
    ds = None
    return vrt_path


def overview_levels(width, height):
    """Every power-of-two level down to the first one under MIN_OVERVIEW_SIZE."""
    levels = []
    factor = 2
    longest = max(width, height)
    while longest / (factor // 2) > MIN_OVERVIEW_SIZE:
        levels.append(factor)
        factor *= 2
    return levels


def build_pyramids(path, cancelled=None):
    """Build all overview levels into an external, compressed .ovr file."""
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    levels = overview_levels(ds.RasterXSize, ds.RasterYSize)
    if levels:
        gdal.SetConfigOption("COMPRESS_OVERVIEW", "DEFLATE")
        try:
            ds.BuildOverviews("AVERAGE", levels, callback=_progress(cancelled))
        finally:
            gdal.SetConfigOption("COMPRESS_OVERVIEW", None)
    ds = None
    return levels


def convert(src, dst, fmt, cancelled=None):
    """Write src to dst as fmt ("GTiff", "AAIGrid" or "FLT").

    Returns a warning string, or None.
    """
    if fmt == "FLT":
        return write_flt(src, dst, cancelled)

    if fmt == "GTiff":
        ds = gdal.Open(src)
        is_float = gdal.GetDataTypeName(ds.GetRasterBand(1).DataType).startswith("Float")
        ds = None
        options = ["COMPRESS=DEFLATE", "PREDICTOR=3" if is_float else "PREDICTOR=2",
                   "TILED=YES", "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS"]
    elif fmt == "AAIGrid":
        # Millimetre precision keeps ASCII grids a sensible size.
        options = ["DECIMAL_PRECISION=3"]
    else:
        raise ValueError(f"Unknown format {fmt}")

    out = gdal.Translate(dst, src, format=fmt, creationOptions=options, callback=_progress(cancelled))
    if out is None:
        raise RuntimeError(f"Could not write {os.path.basename(dst)}")
    out = None
    return None


def mm_to_metres(src, dst):
    """Rewrite a millimetre height grid as a Float32 GeoTIFF in metres (EPSG:27700).

    For the NRW LiDAR archive, whose ASCII grids hold heights in millimetres
    and come without a .prj.
    """
    ds = gdal.Open(src)
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    data = band.ReadAsArray().astype("float32")
    if nodata is not None:
        mask = data == nodata
        data /= 1000.0
        data[mask] = nodata
    else:
        data /= 1000.0

    driver = gdal.GetDriverByName("GTiff")
    out = driver.Create(dst, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Float32,
                        ["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES"])
    out.SetGeoTransform(ds.GetGeoTransform())
    out.SetProjection(ds.GetProjection() or _bng_wkt())
    out_band = out.GetRasterBand(1)
    if nodata is not None:
        out_band.SetNoDataValue(nodata)
    out_band.WriteArray(data)
    out_band = None
    out = None
    ds = None


def _bng_wkt():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(27700)
    return srs.ExportToWkt()


def _fmt(value):
    """Format a coordinate or cell size without losing sub-millimetre precision."""
    if abs(value) >= 1e15:
        # e.g. the float32 minimum used as nodata; exponent form round-trips exactly.
        return f"{value:.17g}"
    text = f"{value:.9f}".rstrip('0').rstrip('.')
    return text if text not in ('', '-') else '0'


def write_flt(src, flt_path, cancelled=None):
    """Write band 1 of src as an ESRI FLT grid (.flt + .hdr + .prj).

    GDAL writes the raw Float32 data through
    its EHdr driver, then the header is rewritten in ESRI FLT form. The corner is
    taken straight from the geotransform (outer upper-left corner), so there is
    no half-cell shift. FLT cells must be square; if they are not, the mean cell
    size is used and the resulting drift is returned as a warning.
    """
    ds = gdal.Open(src)
    gt = ds.GetGeoTransform()
    ncols, nrows = ds.RasterXSize, ds.RasterYSize
    if gt[2] != 0.0 or gt[4] != 0.0:
        raise RuntimeError(f"{os.path.basename(src)} is rotated and cannot be written as FLT")

    xdim, ydim = abs(gt[1]), abs(gt[5])
    warning = None
    if math.isclose(xdim, ydim, rel_tol=SQUARE_TOL):
        cellsize = xdim
    else:
        cellsize = (xdim + ydim) / 2.0
        drift = max(ncols * abs(xdim - cellsize), nrows * abs(ydim - cellsize))
        warning = (f"{os.path.basename(flt_path)}: pixels are not square, cellsize forced to "
                   f"{_fmt(cellsize)} (drift up to {drift:.3f} units)")

    nodata = ds.GetRasterBand(1).GetNoDataValue()
    if nodata is None:
        nodata = -9999.0
    left = gt[0]
    top = gt[3] if gt[5] < 0 else gt[3] + nrows * ydim

    # Scratch name so an existing .bil next to the output is never touched.
    base = os.path.splitext(flt_path)[0]
    tmp_base = base + "__flt_tmp"
    tmp_bil = tmp_base + ".bil"
    try:
        out = gdal.Translate(tmp_bil, ds, format="EHdr", outputType=gdal.GDT_Float32,
                             bandList=[1], callback=_progress(cancelled))
        out = None
        ds = None

        byteorder = "LSBFIRST"
        with open(tmp_base + ".hdr") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].upper() == "BYTEORDER" and parts[1].upper().startswith("M"):
                    byteorder = "MSBFIRST"

        with open(base + ".hdr", "w") as f:
            f.write(f"ncols {ncols}\n")
            f.write(f"nrows {nrows}\n")
            f.write(f"xllcorner {_fmt(left)}\n")
            f.write(f"yllcorner {_fmt(top - nrows * cellsize)}\n")
            f.write(f"cellsize {_fmt(cellsize)}\n")
            f.write(f"NODATA_value {_fmt(nodata)}\n")
            f.write(f"byteorder {byteorder}\n")

        os.replace(tmp_bil, flt_path)
        if os.path.exists(tmp_base + ".prj"):
            os.replace(tmp_base + ".prj", base + ".prj")
    finally:
        for ext in (".bil", ".hdr", ".prj", ".stx", ".bil.aux.xml"):
            if os.path.exists(tmp_base + ext):
                os.remove(tmp_base + ext)
    return warning
