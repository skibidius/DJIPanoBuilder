"""DJI Pano Builder — shared core package.

Both the CLI (``dji_pano.py``) and the GUI (``dji_pano_gui.py``) use this
package, so the stitching logic lives in exactly one place.
"""

from .metadata import (Camera, read_xmp_gimbal, read_exif_focal,
                       build_gpano_xmp, build_output_exif)
from .geometry import rotation_matrix, warp_to_equirect
from .blending import estimate_gains, fill_holes
from .pipeline import (build_panorama, find_pano_folders, list_pano_files,
                       tiny_planet)
from .progress import UI

__all__ = [
    "Camera", "read_xmp_gimbal", "read_exif_focal", "build_gpano_xmp",
    "build_output_exif", "rotation_matrix", "warp_to_equirect",
    "estimate_gains", "fill_holes", "build_panorama", "find_pano_folders",
    "list_pano_files", "tiny_planet", "UI",
]
