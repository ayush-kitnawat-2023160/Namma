#!/usr/bin/env python3
"""Tiny CPU-only check of the pinned HLoc/PyCOLMAP interface and DB schema."""
from __future__ import annotations

import inspect
import json
import sqlite3
import tempfile
from pathlib import Path


def check_sfm_compatibility() -> dict:
    import pycolmap
    from hloc import triangulation

    if pycolmap.__version__ != "3.11.1":
        raise RuntimeError("Precision requires PyCOLMAP 3.11.1 and the compatible pinned HLoc revision")
    # The adapter explicitly uses robust two-view estimation; this also avoids
    # old HLoc's unused pose-essential-matrix helper on recent PyCOLMAP versions.
    if "estimate_two_view_geometries" not in inspect.signature(triangulation.main).parameters:
        raise RuntimeError("HLoc triangulation lacks robust two-view estimation support")
    for refine in (False, True):
        options = pycolmap.IncrementalPipelineOptions({
            "ba_refine_focal_length": refine,
            "ba_refine_principal_point": False,
            "ba_refine_extra_params": False,
            "mapper": {
                "abs_pose_refine_focal_length": refine,
                "abs_pose_refine_extra_params": False,
            },
        })
        if options.ba_refine_focal_length != refine or options.mapper.abs_pose_refine_focal_length != refine:
            raise RuntimeError("PyCOLMAP did not accept the configured focal-length refinement policy")
    ba_options = pycolmap.BundleAdjustmentOptions()
    ba_options.refine_focal_length = True
    ba_options.refine_principal_point = False
    ba_options.refine_extra_params = False

    # Creating an actual tiny model and database catches HLoc's newer rigs/frames
    # API mismatch that a successful package import or version check would miss.
    # It does not extract features, optimize geometry, create tensors, or use CUDA.
    with tempfile.TemporaryDirectory(prefix="namma-sfm-api-") as temporary:
        root = Path(temporary)
        model_dir = root / "reference"
        model_dir.mkdir()
        (model_dir / "cameras.txt").write_text("1 PINHOLE 640 480 500 500 320 240\n")
        (model_dir / "images.txt").write_text(
            "1 1 0 0 0 0 0 0 1 000000.png\n\n"
            "2 1 0 0 0 -1 0 0 1 000001.png\n\n"
            "3 1 0 0 0 -2 0 0 1 000002.png\n\n"
        )
        (model_dir / "points3D.txt").write_text("")
        reconstruction = pycolmap.Reconstruction(str(model_dir))
        if reconstruction.num_reg_images() != 3:
            raise RuntimeError("PyCOLMAP could not read the reference camera model")
        reconstruction.update_point_3d_errors()
        database = root / "database.db"
        ids = triangulation.create_db_from_model(reconstruction, database)
        if sorted(ids) != ["000000.png", "000001.png", "000002.png"]:
            raise RuntimeError("HLoc did not preserve camera/image IDs")
        with sqlite3.connect(database) as connection:
            cameras = connection.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
            images = connection.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        if (cameras, images) != (1, 3):
            raise RuntimeError("HLoc database camera/image schema check failed")
    return {
        "pycolmap": pycolmap.__version__,
        "hloc_triangulation": str(Path(triangulation.__file__).resolve()),
        "reference_model_and_database": "passed (one camera, three images)",
        "focal_refinement_options": "passed (fixed and estimated)",
        "CUDA_work_executed": False,
    }


if __name__ == "__main__":
    print(json.dumps(check_sfm_compatibility(), indent=2))
