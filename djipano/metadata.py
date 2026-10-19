"""Camera intrinsics and metadata: XMP gimbal angles, EXIF, GPano, GPS."""

import math
import re

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# DJI camera presets (wide camera). Sensor size converts EXIF focal length
# (mm) to pixels.
# ---------------------------------------------------------------------------
CAMERA_PRESETS = {
    # model: (sensor_width_mm, sensor_height_mm)
    "M3E": (17.3, 13.0),   # DJI Mavic 3 Enterprise, 4/3 CMOS 20MP
    "M3T": (17.3, 13.0),
    "M30": (12.0, 9.0),    # DJI Matrice 30 wide (1/2", ~8.0mm)
    "FC8282": (17.3, 13.0),
}
DEFAULT_SENSOR = (17.3, 13.0)  # 4/3"


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

XMP_NAMESPACE = r"drone-dji:"


def _first(match):
    return match.group(1) if match else None


def read_xmp_gimbal(path):
    """Возвращает (yaw, pitch, roll) в градусах из XMP drone-dji."""
    try:
        with Image.open(path) as im:
            xmp = im.info.get("xmp")
    except Exception:
        return None, None, None
    if not xmp:
        return None, None, None
    text = xmp.decode("utf-8", errors="replace") if isinstance(xmp, bytes) else str(xmp)

    def grab(name):
        return _first(re.search(XMP_NAMESPACE + name + r'="([^"]+)"', text))

    def to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return (
        to_float(grab("GimbalYawDegree")),
        to_float(grab("GimbalPitchDegree")),
        to_float(grab("GimbalRollDegree")),
    )


def read_exif_focal(path):
    """Возвращает (focal_mm, focal35_mm, make, model) из EXIF."""
    try:
        with Image.open(path) as im:
            ex = im.getexif() or {}
    except Exception:
        ex = {}

    def rat_to_float(v):
        try:
            if hasattr(v, "numerator") and hasattr(v, "denominator"):
                return float(v.numerator) / float(v.denominator)
            return float(v)
        except (TypeError, ValueError):
            return None

    return (
        rat_to_float(ex.get(37386)),   # FocalLength
        rat_to_float(ex.get(41989)),   # FocalLengthIn35mmFilm
        ex.get(271),                    # Make
        ex.get(272),                    # Model
    )


# ---------------------------------------------------------------------------
# Геометрия камеры
# ---------------------------------------------------------------------------

class Camera:
    def __init__(self, width, height, fx, fy):
        self.width = width
        self.height = height
        self.fx = fx
        self.fy = fy
        self.cx = width / 2.0
        self.cy = height / 2.0

    @classmethod
    def from_image(cls, path, model=None, sensor=None):
        with Image.open(path) as im:
            w, h = im.size
        focal_mm, focal35, make, mdl = read_exif_focal(path)
        model = model or mdl or make or "M3E"
        if sensor is None:
            sensor = CAMERA_PRESETS.get(model, DEFAULT_SENSOR)
        sw, sh = sensor

        # Фокусное в мм: берём из EXIF, если нет — из 35мм-эквивалента.
        if focal_mm:
            f_mm = focal_mm
        elif focal35:
            # переводим 35мм-эквивалент обратно в реальное фокусное
            crop = math.sqrt(36.0 ** 2 + 24.0 ** 2) / math.sqrt(sw ** 2 + sh ** 2)
            f_mm = focal35 / crop
        else:
            f_mm = 12.29  # запасное значение для Mavic 3

        fx = f_mm * w / sw
        fy = f_mm * h / sh
        return cls(w, h, fx, fy)


# ---------------------------------------------------------------------------
# Стандартный формат: XMP GPano (чтобы панораму понимали просмотрщики)
# ---------------------------------------------------------------------------

def build_gpano_xmp(width, height, heading=0.0, software="DJI Pano Builder"):
    """Формирует XMP-пакет Google Photo Sphere (equirectangular)."""
    text = f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:GPano="http://ns.google.com/photos/1.0/panorama/"
    GPano:ProjectionType="equirectangular"
    GPano:UsePanoramaViewer="True"
    GPano:CroppedAreaImageWidthPixels="{width}"
    GPano:CroppedAreaImageHeightPixels="{height}"
    GPano:FullPanoWidthPixels="{width}"
    GPano:FullPanoHeightPixels="{height}"
    GPano:CroppedAreaLeftPixels="0"
    GPano:CroppedAreaTopPixels="0"
    GPano:InitialViewHeadingDegrees="{heading:g}"
    GPano:InitialViewPitchDegrees="0"
    GPano:InitialViewRollDegrees="0"
    GPano:StitchingSoftware="{software}"
    GPano:PoseHeadingDegrees="{heading:g}"
    GPano:PosePitchDegrees="0"
    GPano:PoseRollDegrees="0"/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    return text.encode("utf-8")


def build_output_exif(paths):
    """Копирует GPS и базовые EXIF-теги из первого кадра в итоговую панораму."""
    ex = Image.Exif()
    try:
        with Image.open(paths[0]) as im:
            src = im.getexif()
    except Exception:
        return None
    for tag in (271, 272, 306, 36867):  # Make, Model, DateTime, DateTimeOriginal
        v = src.get(tag)
        if v is not None:
            ex[tag] = v
    try:
        gps = src.get_ifd(0x8825)
        if gps:
            ex[0x8825] = gps
    except Exception:
        pass
    return ex
