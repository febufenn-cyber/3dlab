"""Minimal pure-Python reader/writer for COLMAP binary models.

Implements the documented COLMAP binary format (cameras.bin / images.bin /
points3D.bin) so the pipeline can parse GLOMAP/COLMAP output without binding
pycolmap into the CPU test environment. Format reference:
https://colmap.github.io/format.html (verified against COLMAP's
scripts/python/read_write_model.py, BSD-3).

The writer exists for unit tests and synthetic fixtures; production models
come from COLMAP/GLOMAP binaries.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# model_id -> (name, num_params) — from COLMAP src/colmap/sensor/models.h
CAMERA_MODELS: dict[int, tuple[str, int]] = {
    0: ("SIMPLE_PINHOLE", 3),
    1: ("PINHOLE", 4),
    2: ("SIMPLE_RADIAL", 4),
    3: ("RADIAL", 5),
    4: ("OPENCV", 8),
    5: ("OPENCV_FISHEYE", 8),
    6: ("FULL_OPENCV", 12),
    7: ("FOV", 5),
    8: ("SIMPLE_RADIAL_FISHEYE", 4),
    9: ("RADIAL_FISHEYE", 5),
    10: ("THIN_PRISM_FISHEYE", 12),
}
CAMERA_MODEL_IDS = {name: mid for mid, (name, _) in CAMERA_MODELS.items()}


@dataclass
class Camera:
    camera_id: int
    model: str
    width: int
    height: int
    params: np.ndarray  # model-specific intrinsics

    def focal(self) -> float:
        """Approximate focal length in pixels (fx or shared f)."""
        return float(self.params[0])


@dataclass
class Image:
    image_id: int
    qvec: np.ndarray  # rotation world->cam as quaternion (w, x, y, z)
    tvec: np.ndarray  # translation world->cam
    camera_id: int
    name: str
    xys: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    point3d_ids: np.ndarray = field(default_factory=lambda: np.zeros((0,), dtype=np.int64))

    def rotmat(self) -> np.ndarray:
        return qvec2rotmat(self.qvec)

    def cam_center(self) -> np.ndarray:
        """Camera center in world coordinates: C = -R^T t."""
        return -self.rotmat().T @ self.tvec

    def num_valid_points(self) -> int:
        return int((self.point3d_ids >= 0).sum())


@dataclass
class Point3D:
    point3d_id: int
    xyz: np.ndarray
    rgb: np.ndarray  # uint8 (3,)
    error: float
    track_image_ids: np.ndarray = field(default_factory=lambda: np.zeros((0,), dtype=np.int32))


@dataclass
class ColmapModel:
    cameras: dict[int, Camera]
    images: dict[int, Image]
    points: dict[int, Point3D]

    def points_array(self) -> tuple[np.ndarray, np.ndarray]:
        """(N,3) float64 xyz and (N,3) uint8 rgb in insertion order."""
        if not self.points:
            return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.uint8)
        xyz = np.stack([p.xyz for p in self.points.values()])
        rgb = np.stack([p.rgb for p in self.points.values()])
        return xyz, rgb


def qvec2rotmat(q: np.ndarray) -> np.ndarray:
    w, x, y, z = (float(v) for v in q)
    return np.array(
        [
            [1 - 2 * y * y - 2 * z * z, 2 * x * y - 2 * z * w, 2 * x * z + 2 * y * w],
            [2 * x * y + 2 * z * w, 1 - 2 * x * x - 2 * z * z, 2 * y * z - 2 * x * w],
            [2 * x * z - 2 * y * w, 2 * y * z + 2 * x * w, 1 - 2 * x * x - 2 * y * y],
        ]
    )


def _read(fid, fmt: str) -> tuple:
    fmt = "<" + fmt  # little-endian, packed (no native alignment padding)
    size = struct.calcsize(fmt)
    data = fid.read(size)
    if len(data) != size:
        raise ValueError("Truncated COLMAP binary file")
    return struct.unpack(fmt, data)


def read_cameras_binary(path: Path) -> dict[int, Camera]:
    cameras: dict[int, Camera] = {}
    with open(path, "rb") as fid:
        (num_cameras,) = _read(fid, "Q")
        for _ in range(num_cameras):
            camera_id, model_id, width, height = _read(fid, "iiQQ")
            if model_id not in CAMERA_MODELS:
                raise ValueError(f"Unknown COLMAP camera model id {model_id}")
            name, num_params = CAMERA_MODELS[model_id]
            params = np.array(_read(fid, "d" * num_params))
            cameras[camera_id] = Camera(camera_id, name, int(width), int(height), params)
    return cameras


def read_images_binary(path: Path) -> dict[int, Image]:
    images: dict[int, Image] = {}
    with open(path, "rb") as fid:
        (num_images,) = _read(fid, "Q")
        for _ in range(num_images):
            image_id, *qt, camera_id = _read(fid, "idddddddi")
            qvec = np.array(qt[:4])
            tvec = np.array(qt[4:7])
            name_bytes = b""
            while True:
                c = fid.read(1)
                if c == b"\x00" or c == b"":
                    break
                name_bytes += c
            (num_points2d,) = _read(fid, "Q")
            raw = _read(fid, "ddq" * num_points2d)
            xys = np.array(raw).reshape(-1, 3)[:, :2] if num_points2d else np.zeros((0, 2))
            ids = (
                np.array(raw[2::3], dtype=np.int64)
                if num_points2d
                else np.zeros((0,), dtype=np.int64)
            )
            images[image_id] = Image(
                image_id, qvec, tvec, camera_id, name_bytes.decode("utf-8"), xys, ids
            )
    return images


def read_points3d_binary(path: Path) -> dict[int, Point3D]:
    points: dict[int, Point3D] = {}
    with open(path, "rb") as fid:
        (num_points,) = _read(fid, "Q")
        for _ in range(num_points):
            pid, x, y, z, r, g, b, error = _read(fid, "QdddBBBd")
            (track_len,) = _read(fid, "Q")
            track = _read(fid, "ii" * track_len)
            points[pid] = Point3D(
                pid,
                np.array([x, y, z]),
                np.array([r, g, b], dtype=np.uint8),
                float(error),
                np.array(track[0::2], dtype=np.int32),
            )
    return points


def read_model(model_dir: Path) -> ColmapModel:
    model_dir = Path(model_dir)
    return ColmapModel(
        cameras=read_cameras_binary(model_dir / "cameras.bin"),
        images=read_images_binary(model_dir / "images.bin"),
        points=read_points3d_binary(model_dir / "points3D.bin"),
    )


# --- writers (tests / fixtures) -------------------------------------------


def write_cameras_binary(cameras: dict[int, Camera], path: Path) -> None:
    with open(path, "wb") as fid:
        fid.write(struct.pack("<Q", len(cameras)))
        for cam in cameras.values():
            model_id = CAMERA_MODEL_IDS[cam.model]
            _, num_params = CAMERA_MODELS[model_id]
            fid.write(struct.pack("<iiQQ", cam.camera_id, model_id, cam.width, cam.height))
            fid.write(struct.pack("<" + "d" * num_params, *cam.params[:num_params]))


def write_images_binary(images: dict[int, Image], path: Path) -> None:
    with open(path, "wb") as fid:
        fid.write(struct.pack("<Q", len(images)))
        for im in images.values():
            fid.write(
                struct.pack(
                    "<idddddddi", im.image_id, *im.qvec.tolist(), *im.tvec.tolist(), im.camera_id
                )
            )
            fid.write(im.name.encode("utf-8") + b"\x00")
            n = len(im.xys)
            fid.write(struct.pack("<Q", n))
            for (x, y), pid in zip(im.xys, im.point3d_ids):
                fid.write(struct.pack("<ddq", float(x), float(y), int(pid)))


def write_points3d_binary(points: dict[int, Point3D], path: Path) -> None:
    with open(path, "wb") as fid:
        fid.write(struct.pack("<Q", len(points)))
        for p in points.values():
            fid.write(
                struct.pack(
                    "<QdddBBBd",
                    p.point3d_id,
                    *p.xyz.tolist(),
                    *(int(v) for v in p.rgb),
                    p.error,
                )
            )
            fid.write(struct.pack("<Q", len(p.track_image_ids)))
            for img_id in p.track_image_ids:
                fid.write(struct.pack("<ii", int(img_id), 0))


def write_model(model: ColmapModel, model_dir: Path) -> None:
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    write_cameras_binary(model.cameras, model_dir / "cameras.bin")
    write_images_binary(model.images, model_dir / "images.bin")
    write_points3d_binary(model.points, model_dir / "points3D.bin")
