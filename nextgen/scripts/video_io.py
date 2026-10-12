#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class VideoInfo:
    coded_width: int
    coded_height: int
    width: int
    height: int
    fps: float
    total_frames: int
    duration: float
    rotation_degrees: int


def probe_video(path: Path) -> VideoInfo:
    """Return video metadata and the display-oriented dimensions.

    Many phone videos (including ADVIO's iPhone videos) are physically encoded
    as 1280x720 while carrying a 90-degree display rotation. The calibration,
    however, is defined in the displayed portrait frame (720x1280). We therefore
    expose both the coded and display-oriented dimensions here.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError(f"No video stream found in {path}")
    stream = streams[0]

    coded_width = int(stream["width"])
    coded_height = int(stream["height"])

    rotation = 0
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        try:
            rotation = int(round(float(tags["rotate"])))
        except Exception:
            rotation = 0
    if rotation == 0:
        for side_data in stream.get("side_data_list") or []:
            if "rotation" in side_data:
                try:
                    rotation = int(round(float(side_data["rotation"])))
                    break
                except Exception:
                    pass

    normalized_rotation = rotation % 360
    if normalized_rotation in (90, 270):
        width, height = coded_height, coded_width
    else:
        width, height = coded_width, coded_height

    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    try:
        fps = float(Fraction(rate))
    except Exception:
        fps = 0.0
    if fps <= 0:
        raise RuntimeError(f"Invalid video frame rate reported by ffprobe: {rate}")

    duration = float((payload.get("format") or {}).get("duration") or 0.0)
    nb_frames = stream.get("nb_frames")
    try:
        total_frames = int(nb_frames) if nb_frames not in (None, "N/A") else 0
    except Exception:
        total_frames = 0
    if total_frames <= 0 and duration > 0:
        total_frames = max(1, int(round(duration * fps)))

    return VideoInfo(
        coded_width=coded_width,
        coded_height=coded_height,
        width=width,
        height=height,
        fps=fps,
        total_frames=total_frames,
        duration=duration,
        rotation_degrees=rotation,
    )


class RawVideoReader:
    """Sequential BGR decoder in display orientation.

    FFmpeg's normal autorotation is intentionally retained. This is important for
    ADVIO: the MOV is encoded as landscape frames but documented/captured as a
    portrait camera stream, and its calibration principal point is in that
    portrait frame.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.info = probe_video(self.path)
        self.frame_bytes = self.info.width * self.info.height * 3
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(self.path),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vsync",
            "0",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "pipe:1",
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=10**8,
        )
        if self.proc.stdout is None:
            raise RuntimeError("Failed to open FFmpeg stdout pipe")

    @property
    def fps(self) -> float:
        return self.info.fps

    @property
    def width(self) -> int:
        return self.info.width

    @property
    def height(self) -> int:
        return self.info.height

    @property
    def total_frames(self) -> int:
        return self.info.total_frames

    def read(self) -> tuple[bool, np.ndarray | None]:
        assert self.proc.stdout is not None
        raw = self.proc.stdout.read(self.frame_bytes)
        if len(raw) == 0:
            return False, None
        if len(raw) != self.frame_bytes:
            self.release()
            raise RuntimeError(
                f"FFmpeg returned a partial raw frame: {len(raw)}/{self.frame_bytes} bytes"
            )
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(
            self.info.height, self.info.width, 3
        )
        return True, frame.copy()

    def release(self) -> None:
        if getattr(self, "proc", None) is None:
            return
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=2)
        self.proc = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
