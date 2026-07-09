"""Core module for the GEID Automation Pipeline.

This module contains the GEIDPipelineManager class, which handles
automated satellite imagery downloading, spatial optimization,
and data deduplication using Google Earth Images Downloader (GEID).
"""

import os
import re
import time
import datetime
import tempfile
import gc
import shutil
import subprocess
from typing import List, Tuple, Dict, Any

import geopandas as gpd
from shapely.geometry import box
import rasterio
from rasterio.merge import merge


class GEIDPipelineManager:
    """Manager class for Google Earth Images Downloader (GEID) pipeline.

    Handles spatial preprocessing, asynchronous process control,
    log parsing for temporal accuracy, and image patch management.

    Attributes:
        base_dir (str): The root directory for operations and output.
        grid_chunk_size (int): Number of tiles to group into a single grid mesh.
        fill_ratio_threshold (float): Threshold to determine if grid splitting is needed.
        geid_exe_path (str): The resolved absolute path to the GEID executable.
        log_pattern (re.Pattern): Pre-compiled regex pattern for parsing GEID logs.
    """

    def __init__(self, base_dir: str, geid_exe_path: str = "downloader.exe", 
                 grid_chunk_size: int = 16, fill_ratio_threshold: float = 0.6):
        """Initializes the pipeline manager with configuration parameters.

        Args:
            base_dir (str): Base working directory for inputs and outputs.
            geid_exe_path (str, optional): Path or command name for the GEID executable. 
                Defaults to "downloader.exe".
            grid_chunk_size (int, optional): Grid size in tiles for spatial splitting. 
                Defaults to 16.
            fill_ratio_threshold (float, optional): Ratio below which bounding box 
                will be split into grids. Defaults to 0.6.

        Raises:
            FileNotFoundError: If the specified geid_exe_path cannot be found in the system PATH.
        """
        self.base_dir = base_dir
        self.grid_chunk_size = grid_chunk_size
        self.fill_ratio_threshold = fill_ratio_threshold
        
        self.geid_exe_path = shutil.which(geid_exe_path)
        if not self.geid_exe_path:
            raise FileNotFoundError(
                f"Executable not found: {geid_exe_path}. "
                "Ensure it is added to the system PATH or provide an absolute path."
            )

        self.log_pattern = re.compile(r"(ges[h]?_\d+_\d+_\d+\.jpg):\s*(?:(\d{4}-\d{1,2}-\d{1,2})\s+)?Ok")

    def _calculate_fill_ratio(self, gdf: gpd.GeoDataFrame) -> float:
        """Calculates the ratio of the actual polygon area to its bounding box area.

        Projects the GeoDataFrame to Web Mercator (EPSG:3857) to ensure metric accuracy.

        Args:
            gdf (gpd.GeoDataFrame): The input spatial data containing polygons.

        Returns:
            float: The calculated fill ratio ranging from 0.0 to 1.0.
        """
        if gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
            
        gdf_proj = gdf.to_crs(epsg=3857)
        actual_area = gdf_proj.geometry.area.sum()
        
        minx, miny, maxx, maxy = gdf_proj.total_bounds
        bbox_area = box(minx, miny, maxx, maxy).area
        
        if bbox_area == 0:
            return 1.0
            
        return float(actual_area / bbox_area)

    def _generate_dynamic_grid(self, bounds: Tuple[float, float, float, float], zoom: int) -> gpd.GeoDataFrame:
        """Generates a spatial grid mesh based on the zoom level and chunk size.

        Args:
            bounds (Tuple[float, float, float, float]): The total bounds (minx, miny, maxx, maxy).
            zoom (int): The target zoom level for downloading.

        Returns:
            gpd.GeoDataFrame: A GeoDataFrame containing the generated grid polygons
                in EPSG:4326 coordinate reference system.
        """
        minx, miny, maxx, maxy = bounds
        tile_width_deg = 360.0 / (2 ** zoom)
        chunk_size_deg = tile_width_deg * self.grid_chunk_size
        
        polygons = []
        curr_x = minx
        while curr_x < maxx:
            curr_y = miny
            while curr_y < maxy:
                polygons.append(box(curr_x, curr_y, curr_x + chunk_size_deg, curr_y + chunk_size_deg))
                curr_y += chunk_size_deg
            curr_x += chunk_size_deg
            
        return gpd.GeoDataFrame({'geometry': polygons}, crs="EPSG:4326")

    def _wait_for_completion(self, temp_dir: str, task_name: str, process: subprocess.Popen, timeout_sec: int = 3600) -> bool:
        """Blocks execution until the downloader process finishes.

        Enforces a strict timeout to prevent infinite hangs caused by
        silent crashes or frozen external executable states.

        Args:
            temp_dir (str): The temporary directory where logs are saved.
            task_name (str): The name of the current task to locate the correct log file.
            process (subprocess.Popen): The running subprocess object.
            timeout_sec (int, optional): Maximum allowed time in seconds for the process. 
                Defaults to 3600.

        Returns:
            bool: True when the task is successfully completed, False if it timed out.
        """
        log_file_path = os.path.join(temp_dir, f"{task_name}_log.txt")
        start_time = time.time()
        task_completed = False

        while time.time() - start_time < timeout_sec:
            if process.poll() is not None:
                break
                
            if os.path.exists(log_file_path):
                try:
                    with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        if "Task finished!" in f.read():
                            task_completed = True
                            break
                except PermissionError:
                    pass
            time.sleep(2)

        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            
        return task_completed

    def _parse_log_and_relocate(self, temp_dir: str, target_roi_name: str, output_sub_dir: str, task_name: str, 
                                target_date: str, zoom: int, bbox: Tuple[float, float, float, float]) -> None:
        """Parses logs and relocates raw tile patches into isolated subdirectories.

        Reads the output logs from the GEID executable to extract accurate
        historical capture dates, and moves the corresponding JPG/JGW files.

        Args:
            temp_dir (str): The temporary directory containing downloaded tiles and logs.
            target_roi_name (str): The unique identifier for the region (e.g., objid).
            output_sub_dir (str): The hierarchical path string (e.g., 'Country/Mineral').
            task_name (str): The name of the specific execution task.
            target_date (str): The originally requested date from the user.
            zoom (int): The zoom level of the downloaded imagery.
            bbox (Tuple[float, float, float, float]): The bounding box of the spatial chunk.
        """
        log_file_path = os.path.join(temp_dir, f"{task_name}_log.txt")
        if not os.path.exists(log_file_path):
            return

        date_mapping = {}
        log_lines_by_date = {}
        
        with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                match = self.log_pattern.search(line)
                if match:
                    filename = match.group(1)
                    raw_date = match.group(2)
                    
                    if raw_date:
                        parts = raw_date.split('-')
                        actual_date = f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
                    else:
                        actual_date = "Current_Default"
                        
                    date_mapping[filename] = actual_date
                    if actual_date not in log_lines_by_date:
                        log_lines_by_date[actual_date] = []
                    log_lines_by_date[actual_date].append(line.strip())

        for root, _, files in os.walk(temp_dir):
            for file in files:
                if file.endswith('.jpg'):
                    if file in date_mapping:
                        actual_date = date_mapping[file]
                    else:
                        continue
                        
                    dest_dir = os.path.join(self.base_dir, "output", output_sub_dir, f"patches_{target_roi_name}", actual_date)
                    os.makedirs(dest_dir, exist_ok=True)
                    
                    base_name = os.path.splitext(file)[0]
                    jpg_path = os.path.join(root, file)
                    jgw_path = os.path.join(root, f"{base_name}.jgw")
                    
                    dest_jpg = os.path.join(dest_dir, file)
                    dest_jgw = os.path.join(dest_dir, f"{base_name}.jgw")
                    
                    if not os.path.exists(dest_jpg):
                        shutil.move(jpg_path, dest_jpg)
                    if os.path.exists(jgw_path) and not os.path.exists(dest_jgw):
                        shutil.move(jgw_path, dest_jgw)

        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        for actual_date, log_lines in log_lines_by_date.items():
            dest_dir = os.path.join(self.base_dir, "output", output_sub_dir, f"patches_{target_roi_name}", actual_date)
            if os.path.exists(dest_dir):
                report_path = os.path.join(dest_dir, f"{target_roi_name}_extraction_report.txt")
                with open(report_path, "a", encoding="utf-8") as rf:
                    rf.write(f"==================================================\n")
                    rf.write(f"Task Execution Time : {current_time}\n")
                    rf.write(f"Task Identifier     : {task_name}\n")
                    rf.write(f"Requested Target    : {target_date}\n")
                    rf.write(f"Actual Capture Date : {actual_date}\n")
                    rf.write(f"Zoom Level          : {zoom}\n")
                    rf.write(f"Bounding Box (4326) : {bbox}\n")
                    rf.write(f"Total Patches Found : {len(log_lines)}\n")
                    rf.write(f"--------------------------------------------------\n")
                    rf.write(f"[Original Log Entries]\n")
                    for line in log_lines:
                        rf.write(f"{line}\n")
                    rf.write(f"\n\n")

    def download_roi(self, roi_gdf: gpd.GeoDataFrame, roi_name: str, output_sub_dir: str, target_dates: List[str], zoom: int) -> None:
        """Executes the main downloader pipeline for a specific region of interest.

        Args:
            roi_gdf (gpd.GeoDataFrame): GeoDataFrame containing the ROI polygon(s).
            roi_name (str): Unique identifier for the region (e.g., objid).
            output_sub_dir (str): The hierarchical path string (e.g., 'Country/Mineral').
            target_dates (List[str]): List of target dates in YYYY-MM-DD format.
            zoom (int): Target zoom level.
        """
        if roi_gdf.crs is None or roi_gdf.crs.to_epsg() != 4326:
            roi_gdf = roi_gdf.to_crs(epsg=4326)

        fill_ratio = self._calculate_fill_ratio(roi_gdf)
        
        target_bboxes = []
        if fill_ratio >= self.fill_ratio_threshold:
            minx, miny, maxx, maxy = roi_gdf.total_bounds
            target_bboxes.append((minx, miny, maxx, maxy))
        else:
            grid_mesh = self._generate_dynamic_grid(roi_gdf.total_bounds, zoom)
            intersected = gpd.sjoin(grid_mesh, roi_gdf, predicate='intersects')
            for _, row in intersected.iterrows():
                target_bboxes.append(row.geometry.bounds)

        for target_date in target_dates:
            for i, bbox in enumerate(target_bboxes):
                minx, miny, maxx, maxy = bbox
                task_name = f"{roi_name}_{target_date}_{i}"
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    cmd = [
                        self.geid_exe_path,
                        f"{task_name}.geid",
                        str(zoom), str(zoom),
                        str(minx), str(maxx),
                        str(maxy), str(miny),
                        temp_dir
                    ]

                    if target_date.lower() != "current":
                        cmd.append(target_date)

                    proc = subprocess.Popen(cmd)
                    self._wait_for_completion(temp_dir, task_name, proc)

                    self._parse_log_and_relocate(
                        temp_dir=temp_dir,
                        target_roi_name=roi_name,
                        output_sub_dir=output_sub_dir,
                        task_name=task_name,
                        target_date=target_date,
                        zoom=zoom,
                        bbox=bbox
                    )

    def merge_patches(self, target_folder: str, output_filename: str) -> Tuple[Any, Dict[str, Any]]:
        """Merges JPG patches memory-efficiently.

        Passes file paths directly to rasterio to prevent holding 
        unclosed dataset descriptors and exhausting system RAM (OOM).

        Args:
            target_folder (str): The directory containing the tile pairs.
            output_filename (str): Name identifier for the merged output.

        Returns:
            Tuple[Any, Dict[str, Any]]: A tuple containing the merged NumPy array 
                and the updated rasterio metadata dictionary.

        Raises:
            FileNotFoundError: If no JPG files are found in the target directory.
        """
        file_list = []
        for root, _, files in os.walk(target_folder):
            for file in files:
                if file.endswith('.jpg'):
                    file_list.append(os.path.join(root, file))
                    
        if not file_list:
            raise FileNotFoundError(f"No JPG patches found in {target_folder}")

        mosaic, out_trans = merge(file_list)
        
        with rasterio.open(file_list[0]) as first_src:
            out_meta = first_src.meta.copy()
            
        out_meta.update({
            "driver": "GTiff",
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": out_trans,
            "crs": "EPSG:4326"
        })

        gc.collect()
        return mosaic, out_meta

    def convert_to_geotiff(self, image_array: Any, meta: Dict[str, Any], output_path: str) -> str:
        """Compresses and saves a merged image array as a GeoTIFF.

        Args:
            image_array (Any): The NumPy array containing the image data.
            meta (Dict[str, Any]): The spatial metadata dictionary for rasterio.
            output_path (str): The exact file path to save the output GeoTIFF.

        Returns:
            str: The absolute path to the generated GeoTIFF file.
        """
        meta.update({"compress": "lzw"})
        with rasterio.open(output_path, "w", **meta) as dest:
            dest.write(image_array)
        return output_path