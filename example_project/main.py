import argparse
import datetime
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

from geid_pipeline import GEIDPipelineManager


def generate_target_dates(start_year: int, end_date_str: str, target_months: list, day: int = 15) -> list:
    end_date_str_lower = end_date_str.lower()
    if end_date_str_lower in ["today", "now"]:
        end_date = datetime.datetime.now()
        append_current = True
    else:
        end_date = datetime.datetime.strptime(end_date_str, "%Y-%m-%d")
        append_current = False

    dates = []
    for year in range(start_year, end_date.year + 1):
        for month in target_months:
            target_dt = datetime.datetime(year, month, day)
            if target_dt <= end_date:
                dates.append(target_dt.strftime("%Y-%m-%d"))

    if append_current:
        dates.append("current")
    return dates


def parse_bbox(value: str):
    parts = [float(part.strip()) for part in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--bbox must be left,bottom,right,top")
    left, bottom, right, top = parts
    return left, bottom, right, top


def build_target_dates(args) -> list:
    target_dates = []
    if args.current:
        target_dates.append("current")
    if args.date:
        target_dates.extend(args.date)
    if args.start_year:
        target_dates.extend(
            generate_target_dates(
                start_year=args.start_year,
                end_date_str=args.end_date,
                target_months=args.target_months,
            )
        )
    if not target_dates:
        target_dates = ["current"]
    return list(dict.fromkeys(target_dates))


def merge_actual_date_folders(pipeline, patch_root: Path, output_root: Path, site_id: str):
    if not patch_root.exists():
        return
    for folder_path in sorted(p for p in patch_root.iterdir() if p.is_dir()):
        actual_date = folder_path.name
        output_tif = output_root / f"{site_id}_{actual_date}_Merged.tif"
        if output_tif.exists():
            continue
        image_array, meta = pipeline.merge_patches(
            target_folder=str(folder_path),
            output_filename=f"temp_{actual_date}",
        )
        pipeline.convert_to_geotiff(
            image_array=image_array,
            meta=meta,
            output_path=str(output_tif),
        )


def iter_sites_from_gpkg(gpkg_path: Path):
    gdf = gpd.read_file(gpkg_path)
    if "site_id" not in gdf.columns:
        raise ValueError("GeoPackage input requires a site_id column.")
    for _, row in gdf.iterrows():
        site_id = str(row["site_id"])
        group = str(row["group"]) if "group" in gdf.columns else "default"
        roi_gdf = gpd.GeoDataFrame(
            {"site_id": [site_id]},
            geometry=[row.geometry],
            crs=gdf.crs,
        )
        yield site_id, group, roi_gdf


def iter_sites_from_bbox(args):
    left, bottom, right, top = parse_bbox(args.bbox)
    site_id = args.site_id or "bbox_site"
    roi_gdf = gpd.GeoDataFrame(
        {"site_id": [site_id]},
        geometry=[box(left, bottom, right, top)],
        crs="EPSG:4326",
    )
    yield site_id, args.output_sub_dir, roi_gdf


def main():
    parser = argparse.ArgumentParser(description="Minimal GEID automation example.")
    parser.add_argument("--bbox", help="Single ROI bbox in left,bottom,right,top order.")
    parser.add_argument("--gpkg", help="GeoPackage containing site_id, optional group, and geometry.")
    parser.add_argument("--site-id", help="Site id for --bbox mode.")
    parser.add_argument("--output-sub-dir", default="manual_examples/bbox")
    parser.add_argument("--zoom", type=int, default=16)
    parser.add_argument("--date", action="append", help="Historical target date. Can be repeated.")
    parser.add_argument("--current", action="store_true", help="Request current imagery.")
    parser.add_argument("--start-year", type=int)
    parser.add_argument("--end-date", default="today")
    parser.add_argument("--target-months", nargs="+", type=int, default=[5, 8, 11])
    parser.add_argument("--geid-exe", default="downloader.exe")
    args = parser.parse_args()

    if not args.bbox and not args.gpkg:
        parser.error("Provide either --bbox or --gpkg.")

    base_dir = Path(__file__).parent.resolve()
    target_dates = build_target_dates(args)
    pipeline = GEIDPipelineManager(base_dir=str(base_dir), geid_exe_path=args.geid_exe)

    if args.gpkg:
        gpkg_path = Path(args.gpkg)
        if not gpkg_path.is_absolute():
            gpkg_path = base_dir / gpkg_path
        site_iter = iter_sites_from_gpkg(gpkg_path)
    else:
        site_iter = iter_sites_from_bbox(args)

    for site_id, output_sub_dir, roi_gdf in site_iter:
        output_sub_dir = output_sub_dir.replace("\\", "/")
        pipeline.download_roi(
            roi_gdf=roi_gdf,
            roi_name=site_id,
            output_sub_dir=output_sub_dir,
            target_dates=target_dates,
            zoom=args.zoom,
        )
        output_root = base_dir / "output" / output_sub_dir
        patch_root = output_root / f"patches_{site_id}"
        merge_actual_date_folders(pipeline, patch_root, output_root, site_id)


if __name__ == "__main__":
    main()
