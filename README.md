# DJI Pano Builder

Сборка качественной сферической панорамы из набора снимков дрона DJI
(режим **Sphere Panorama** / «Панорама 360»).

A quality stitcher for DJI drone spherical panoramas (Sphere Panorama mode).

---

## Русский

### Что это

DJI сохраняет панораму как набор отдельных кадров `PANO0001.JPG … PANO0025.JPG`
(обычно 3 кольца по 8 кадров + надир). В XMP-метаданных каждого кадра уже
записаны **точные углы подвеса**: `GimbalYawDegree`, `GimbalPitchDegree`,
`GimbalRollDegree`.

Скрипт использует эту ориентацию напрямую: каждый кадр перепроецируется на
сферу, а не «склеивается» по общим точкам. Благодаря этому:

* нет дрожания и «двоения» геометрии, которое бывает при автоматическом
  поиске соответствий;
* панорама получается равномерной и без накопления ошибки;
* результат — **стандартная равноугольная (equirectangular) панорама 2:1**
  с XMP-метаданными Google Photo Sphere (GPano), которую понимают
  просмотрщики 360° (Google Photos, Facebook, Ricoh/Insta360-плееры и т.д.).

### Возможности

* Точная сборка по углам подвеса из XMP (без feature matching).
* Выход 2:1 equirectangular JPEG + XMP GPano + GPS/EXIF из исходника.
* Аккуратное заполнение «дыры» в зените: диффузия цвета (уравнение Лапласа)
  вместо грубого копирования ближайшего пикселя — без тёмной каймы и полос.
* Мягкое смешивание кадров с косинусоидальными весами.
* Опциональное выравнивание яркости по перекрытиям (`--exposure auto`).
* Пакетная обработка: можно указать корневую папку — обработаются все наборы.
* Превью «маленькая планета» (`--preview`).
* Устойчивость к пропущенным кадрам: отсутствие одного снимка не ломает
  сборку (на этом месте останется мягкая «заплатка»).

### Требования

* Python 3.9+
* `numpy`
* `Pillow`

```bash
pip install -r requirements.txt
```

### Использование

```bash
# одна папка с кадрами
python3 dji_pano.py PANORAMA/006_0004 -o 006_0004_pano.jpg

# с превью «маленькая планета»
python3 dji_pano.py PANORAMA/006_0004 -o 006_0004_pano.jpg --preview

# пакетно: все подпапки с PANO*.JPG
python3 dji_pano.py PANORAMA --out-dir output -w 12000 --preview
```

### Опции

| Опция | Описание | По умолчанию |
|-------|----------|--------------|
| `-o, --output` | путь к выходному JPEG (для одной папки) | — |
| `-w, --width` | ширина панорамы в пикселях (высота = ширина / 2) | `12000` |
| `--preview` | сохранить превью «маленькая планета» | выкл. |
| `--no-fill` | не заполнять дыру в зените (оставить чёрной) | выкл. |
| `--exposure {none,auto}` | коррекция яркости: `none` — не трогать, `auto` — по перекрытиям | `none` |
| `--out-dir` | папка результатов при пакетной обработке | `.` |

### Как это работает

1. Из каждого `PANO*.JPG` читаются `GimbalYaw/Pitch/Roll` (XMP) и фокусное
   расстояние (EXIF). По размеру сенсора камеры вычисляются внутренние
   параметры `fx, fy`.
2. Для каждого кадра строится матрица поворота «камера → мир» (ENU).
3. Выходная equirect-карта заполняется обратным отображением: для каждого
   пикселя сферы находится направление луча, оно переводится в координаты
   кадра, и выполняется билинейная выборка.
4. Кадры смешиваются с весами, затухающими к краям.
5. Непокрытые области (зенит) заполняются диффузией цвета.
6. Результат сохраняется как 2:1 JPEG с XMP GPano.

### Про формат DJI Sphere

Стандартный набор DJI — **25 кадров**: 3 кольца по 8 кадров (pitch ≈ +20°,
−17°, −55°) и один надир (pitch ≈ −90°). Зенит (pitch > +48°) при этом
не снимается, поэтому в исходных данных его физически нет — скрипт
достраивает его плавным заполнением. Если небо в кадре пересвечено
(белое), заплатка будет незаметной.

Если в наборе не хватает одного кадра (например, 24 файла вместо 25),
панорама всё равно соберётся, но в месте пропуска будет мягкое размытое
пятно — данных там нет.

### Лицензия

MIT. См. [LICENSE](LICENSE).

---

## English

### What it is

DJI stores a spherical panorama as a set of separate frames `PANO0001.JPG …
PANO0025.JPG` (usually 3 rings × 8 frames + one nadir). Each frame's XMP
metadata already contains the **exact gimbal orientation**:
`GimbalYawDegree`, `GimbalPitchDegree`, `GimbalRollDegree`.

The script uses that orientation directly: every frame is reprojected onto a
sphere instead of being matched by features. This gives:

* no drift or geometry doubling typical of automatic feature matching;
* an even, globally consistent panorama;
* a **standard 2:1 equirectangular panorama** with Google Photo Sphere (GPano)
  XMP metadata, understood by 360° viewers (Google Photos, Facebook, etc.).

### Features

* Accurate stitching from XMP gimbal angles (no feature matching).
* 2:1 equirectangular JPEG output + GPano XMP + GPS/EXIF from the source.
* Smooth zenith hole filling via colour diffusion (Laplace equation) instead of
  nearest-pixel copying — no dark seam or streaks.
* Soft cosine-weighted blending.
* Optional exposure equalisation over overlaps (`--exposure auto`).
* Batch mode: point it at a root folder and every set is processed.
* "Tiny planet" preview (`--preview`).
* Tolerates missing frames: a missing shot does not break the stitch (a soft
  patch appears where data is absent).

### Requirements

* Python 3.9+
* `numpy`
* `Pillow`

```bash
pip install -r requirements.txt
```

### Usage

```bash
# a single folder
python3 dji_pano.py PANORAMA/006_0004 -o 006_0004_pano.jpg

# with a tiny-planet preview
python3 dji_pano.py PANORAMA/006_0004 -o 006_0004_pano.jpg --preview

# batch: every subfolder containing PANO*.JPG
python3 dji_pano.py PANORAMA --out-dir output -w 12000 --preview
```

### Options

| Option | Description | Default |
|--------|-------------|---------|
| `-o, --output` | output JPEG path (single folder) | — |
| `-w, --width` | panorama width in pixels (height = width / 2) | `12000` |
| `--preview` | also save a "tiny planet" preview | off |
| `--no-fill` | do not fill the zenith hole (leave it black) | off |
| `--exposure {none,auto}` | brightness correction: `none` or overlap-based `auto` | `none` |
| `--out-dir` | output folder for batch mode | `.` |

### How it works

1. Read `GimbalYaw/Pitch/Roll` (XMP) and focal length (EXIF) from each
   `PANO*.JPG`. Compute intrinsics `fx, fy` from the camera sensor size.
2. Build a camera-to-world rotation matrix (ENU) per frame.
3. Fill the output equirect map by inverse mapping: for each sphere pixel find
   the ray direction, transform it into the frame, and bilinearly sample.
4. Blend frames with weights that fade toward the edges.
5. Fill uncovered areas (zenith) by colour diffusion.
6. Save a 2:1 JPEG with GPano XMP.

### About the DJI Sphere format

A standard DJI set is **25 frames**: 3 rings × 8 frames (pitch ≈ +20°, −17°,
−55°) plus one nadir frame (pitch ≈ −90°). The zenith (pitch > +48°) is not
captured, so it is physically absent from the source data — the script
reconstructs it with a smooth fill. If the sky is overexposed (white), the
patch is invisible.

If a frame is missing (e.g. 24 files instead of 25), the panorama still
stitches, but a soft blurred patch appears where the data is absent.

### License

MIT. See [LICENSE](LICENSE).
