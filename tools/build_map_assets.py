"""Build offline terrain and imagery assets for the Lianjiang digital twin.

Sources:
- Esri World Imagery tiles (satellite basemap)
- AWS Terrain Tiles / SRTM 1 arc-second elevation (N26E119, N26E120)

The generated files are intentionally local so the visualization remains
available without a network connection after the initial asset build.
"""

from __future__ import annotations

import gzip
import argparse
import io
import math
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend" / "assets" / "geo"
OUT.mkdir(parents=True, exist_ok=True)

# Bounding box covering Lianjiang, Huangqi Peninsula, Dinghai Bay,
# Luoyuan Bay and the Aojiang estuary. The front-end world transform uses
# lon=119.85 as x=0 and lat=26.37 as z=0.
WEST, EAST = 119.18, 120.38
SOUTH, NORTH = 26.02, 26.52
ZOOM = 12
TILE_SIZE = 256
# SRTM 1 arc-second (skadi) tiles carry 3601 samples across one degree, so the
# sample grid is 3600 intervals wide. Assuming 1200 here silently crops an
# inland strip and misaligns the terrain against the satellite mosaic.
HGT_SAMPLES = 3600
OUT_WIDTH = 2200
OUT_HEIGHT = round(OUT_WIDTH * ((NORTH - SOUTH) * 65.385) / ((EAST - WEST) * 43.333))

TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
DEM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/N26/{tile}.hgt.gz"
USER_AGENT = "DeepBlueGuardian/1.0 (offline map asset builder)"


def download(url: str, *, retries: int = 3) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"Failed to download {url}") from last_error


def lon_to_tile_x(lon: float, zoom: int) -> float:
    return (lon + 180.0) / 360.0 * (1 << zoom)


def lat_to_tile_y(lat: float, zoom: int) -> float:
    clipped = max(min(lat, 85.05112878), -85.05112878)
    radians = math.radians(clipped)
    return (
        1.0
        - math.asinh(math.tan(radians)) / math.pi
    ) / 2.0 * (1 << zoom)


def tile_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    scale = 1 << zoom
    x = (lon + 180.0) / 360.0 * scale * TILE_SIZE
    sin_lat = math.sin(math.radians(lat))
    y = (
        0.5
        - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)
    ) * scale * TILE_SIZE
    return x, y


def build_satellite_mosaic() -> Image.Image:
    x0f, y0f = tile_pixel(WEST, NORTH, ZOOM)
    x1f, y1f = tile_pixel(EAST, SOUTH, ZOOM)
    tx0, ty0 = int(math.floor(x0f / TILE_SIZE)), int(math.floor(y0f / TILE_SIZE))
    tx1, ty1 = int(math.floor((x1f - 1) / TILE_SIZE)), int(math.floor((y1f - 1) / TILE_SIZE))

    mosaic = Image.new(
        "RGB",
        ((tx1 - tx0 + 1) * TILE_SIZE, (ty1 - ty0 + 1) * TILE_SIZE),
        (4, 18, 34),
    )
    total = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
    done = 0
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            payload = download(TILE_URL.format(z=ZOOM, y=ty, x=tx))
            tile = Image.open(io.BytesIO(payload)).convert("RGB")
            mosaic.paste(tile, ((tx - tx0) * TILE_SIZE, (ty - ty0) * TILE_SIZE))
            done += 1
            if done % 25 == 0 or done == total:
                print(f"satellite tiles {done}/{total}", flush=True)

    left = round(x0f - tx0 * TILE_SIZE)
    top = round(y0f - ty0 * TILE_SIZE)
    right = round(x1f - tx0 * TILE_SIZE)
    bottom = round(y1f - ty0 * TILE_SIZE)
    cropped = mosaic.crop((left, top, right, bottom))
    return cropped.resize((OUT_WIDTH, OUT_HEIGHT), Image.Resampling.LANCZOS)


def read_hgt(tile: str) -> np.ndarray:
    payload = gzip.decompress(download(DEM_URL.format(tile=tile)))
    size = int(math.sqrt(len(payload) / 2))
    if size * size * 2 != len(payload):
        raise ValueError(f"Unexpected HGT size for {tile}: {len(payload)}")
    return np.frombuffer(payload, dtype=">i2").reshape((size, size))


def build_dem() -> np.ndarray:
    west = read_hgt("N26E119")
    east = read_hgt("N26E120")
    combined = np.concatenate([west, east[:, 1:]], axis=1)

    col0 = (WEST - 119.0) * HGT_SAMPLES
    col1 = (EAST - 119.0) * HGT_SAMPLES
    row0 = (27.0 - NORTH) * HGT_SAMPLES
    row1 = (27.0 - SOUTH) * HGT_SAMPLES
    crop = combined[
        max(0, int(round(row0))) : min(combined.shape[0], int(round(row1)) + 1),
        max(0, int(round(col0))) : min(combined.shape[1], int(round(col1)) + 1),
    ]
    elevation = np.asarray(crop, dtype=np.float32)
    return elevation


def colorize(satellite: Image.Image, elevation: np.ndarray) -> Image.Image:
    """Grade the satellite mosaic into the digital-twin palette.

    Land keeps the satellite albedo, so vegetation cover, road networks,
    aquaculture rafts and intertidal flats stay recognisable, and only receives
    a cool cinematic grade plus an elevation-driven ridge light.
    Water is rebuilt from the distance-to-coast field so the nearshore shelf,
    the estuarine sediment plume and the deep basin form one continuous
    bathymetric gradient instead of the blocky seams of the source tiles.
    """
    elevation_img = Image.fromarray(elevation.astype(np.float32), mode="F")
    elevation_img = elevation_img.resize(satellite.size, Image.Resampling.BILINEAR)
    height = np.asarray(elevation_img, dtype=np.float32)
    water = height <= 1.0
    land = ~water

    rgb = np.asarray(satellite, dtype=np.float32) / 255.0
    luma = (rgb * np.array([0.299, 0.587, 0.114], dtype=np.float32)).sum(axis=2)

    # 陆地：保留真实反照率，压暗整体并增强植被绿色通道
    contrast = np.clip((rgb - 0.34) * 1.30 + 0.30, 0.0, 1.0)
    grey = contrast.mean(axis=2, keepdims=True)
    saturated = grey + (contrast - grey) * 1.26
    cool = np.clip(saturated * np.array([0.76, 1.0, 0.95], dtype=np.float32), 0.0, 1.0)
    ridge = np.clip(height / 700.0, 0.0, 1.0)[..., None]
    land_rgb = cool * (0.80 + ridge * 0.52)
    land_rgb += np.array([0.012, 0.055, 0.052], dtype=np.float32) * (1.0 - ridge)
    # 远处岛礁所在瓦片时相不同，反照率偏深蓝，这里用绿通道下限与亮度地板
    # 把它们拉回植被色，避免在海面上出现整块深色矩形。
    land_rgb[..., 1] = np.maximum(land_rgb[..., 1], land_rgb[..., 2] * 1.06)
    land_rgb = np.maximum(land_rgb, np.array([0.028, 0.070, 0.052], dtype=np.float32))
    land_rgb = np.clip(land_rgb, 0.0, 1.0)

    # 水体：由距岸距离重建陆架—深槽渐变，近岸叠加潮滩亮度带
    # DEM 在岛礁、养殖平台处存在缺测，用影像中的深蓝像素补齐水体掩膜
    image_water = (
        (rgb[..., 2] > rgb[..., 1] * 1.06)
        & (rgb[..., 2] > rgb[..., 0] * 1.30)
        & (luma < 0.34)
    )
    # 孔洞填充代替形态学闭运算：闭运算会收缩数组四边并生成伪海岸线
    water_mask = ndimage.binary_fill_holes(water | image_water)

    # 距岸距离重建陆架—深槽渐变，用平滑步长避免生硬的色环
    # EDT 默认把数组边界当作背景，先补齐再裁剪，否则四边会出现伪海岸。
    # 补齐宽度必须大于陆架衰减尺度，使裁剪区域不受该背景影响。
    pad = 380
    padded = np.pad(water_mask, pad, mode="edge")
    distance = ndimage.distance_transform_edt(padded)[pad:-pad, pad:-pad]
    shelf = np.clip(1.0 - distance / 320.0, 0.0, 1.0)
    shelf = (shelf * shelf * (3.0 - 2.0 * shelf))[..., None]
    deep = np.array([0.014, 0.068, 0.148], dtype=np.float32)
    mid = np.array([0.030, 0.200, 0.330], dtype=np.float32)
    shore = np.array([0.062, 0.470, 0.545], dtype=np.float32)
    water_rgb = deep + (mid - deep) * shelf
    band = np.clip(1.0 - distance / 95.0, 0.0, 1.0)
    band = (band * band * (3.0 - 2.0 * band))[..., None]
    water_rgb = water_rgb * (1.0 - band) + shore * band

    # 源影像在深水区存在不同时相瓦片拼接，先平滑再以低权重叠加潮流纹理
    water_luma = ndimage.gaussian_filter(luma, sigma=6.0)
    texture = np.clip(water_luma[..., None] - 0.10, 0.0, 1.0) * np.array([0.28, 0.72, 1.0], dtype=np.float32) * 0.12
    water_rgb = np.clip(water_rgb + texture, 0.0, 1.0)

    result = np.where(water_mask[..., None], water_rgb, land_rgb)
    image = Image.fromarray((result * 255).astype(np.uint8), mode="RGB")
    return image.filter(ImageFilter.UnsharpMask(radius=1.3, percent=86, threshold=3))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download tiles even when cached rasters exist")
    args = parser.parse_args()

    satellite_path = OUT / "lianjiang_satellite_source.jpg"
    elevation_path = OUT / "lianjiang_elevation.npy"

    if satellite_path.exists() and not args.force:
        print("reusing cached satellite mosaic", flush=True)
        satellite = Image.open(satellite_path).convert("RGB")
    else:
        print("building satellite mosaic", flush=True)
        satellite = build_satellite_mosaic()
        satellite.save(satellite_path, quality=90, optimize=True)

    if elevation_path.exists() and not args.force:
        print("reusing cached DEM", flush=True)
        elevation = np.load(elevation_path)
    else:
        print("building DEM", flush=True)
        elevation = build_dem()
        np.save(elevation_path, elevation)

    height_image = Image.fromarray(elevation.astype(np.uint16), mode="I;16")
    height_image.resize((1100, OUT_HEIGHT // 2), Image.Resampling.BILINEAR).save(
        OUT / "lianjiang_height.png", optimize=True
    )

    height8 = np.clip(elevation, 0, 1200) / 1200 * 255
    Image.fromarray(height8.astype(np.uint8), mode="L").resize(
        (1100, OUT_HEIGHT // 2), Image.Resampling.BILINEAR
    ).save(OUT / "lianjiang_height8.png", optimize=True)

    land = elevation > 2
    coast = land ^ ndimage.binary_erosion(land, iterations=2)
    coast = ndimage.binary_dilation(coast, iterations=2)
    coast_alpha = ndimage.gaussian_filter(coast.astype(np.float32), sigma=1.8)
    coast_alpha = np.clip(coast_alpha * 2.6, 0, 1)
    coast_rgba = np.zeros((*coast_alpha.shape, 4), dtype=np.uint8)
    coast_rgba[..., 0] = 51
    coast_rgba[..., 1] = 225
    coast_rgba[..., 2] = 255
    coast_rgba[..., 3] = (coast_alpha * 235).astype(np.uint8)
    Image.fromarray(coast_rgba, mode="RGBA").resize(
        (OUT_WIDTH, OUT_HEIGHT), Image.Resampling.BILINEAR
    ).save(OUT / "lianjiang_coast.png", optimize=True)

    print("colorizing terrain", flush=True)
    terrain = colorize(satellite, elevation)
    terrain.save(OUT / "lianjiang_terrain.jpg", quality=91, optimize=True)

    metadata = (
        "Lianjiang terrain basemap\n"
        f"bbox={WEST},{SOUTH},{EAST},{NORTH}\n"
        "satellite=Esri World Imagery (ArcGIS REST tile service)\n"
        "elevation=AWS Terrain Tiles / SRTM 1 arc-second, N26E119 + N26E120\n"
        f"output={OUT_WIDTH}x{OUT_HEIGHT}\n"
    )
    (OUT / "SOURCE.txt").write_text(metadata, encoding="utf-8")
    print(f"wrote assets to {OUT}", flush=True)


if __name__ == "__main__":
    main()
