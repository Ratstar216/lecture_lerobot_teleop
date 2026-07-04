#!/usr/bin/env python3
"""Create a visual sheet for mapping live cameras to trained LeRobot camera names.

Example:
    pixi run camera-map --data data --refresh

The output compares representative training frames such as
`observation.images.front` against current snapshots saved by
`lerobot-find-cameras` in `outputs/captured_images`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


VIDEO_KEY_PREFIX = "observation.images."


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def _load_camera_keys(data_root: Path) -> list[str]:
    info_path = data_root / "meta" / "info.json"
    if not info_path.exists():
        raise FileNotFoundError(f"Dataset metadata not found: {info_path}")

    info = json.loads(info_path.read_text())
    keys = []
    for name, feature in info.get("features", {}).items():
        if name.startswith(VIDEO_KEY_PREFIX) and feature.get("dtype") in {"video", "image"}:
            keys.append(name)
    if not keys:
        raise ValueError(f"No camera features named {VIDEO_KEY_PREFIX}* found in {info_path}")
    return sorted(keys)


def _find_training_video(data_root: Path, camera_key: str) -> Path:
    video_dir = data_root / "videos" / camera_key
    candidates = sorted(video_dir.glob("chunk-*/file-*.mp4"))
    if not candidates:
        raise FileNotFoundError(f"No training video found for {camera_key} under {video_dir}")
    return candidates[0]


def _extract_training_frames(data_root: Path, out_dir: Path, seconds: float) -> list[tuple[str, Path]]:
    items = []
    for key in _load_camera_keys(data_root):
        name = key.removeprefix(VIDEO_KEY_PREFIX)
        src = _find_training_video(data_root, key)
        dst = out_dir / f"train_{name}.png"
        _run([
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            str(seconds),
            "-i",
            str(src),
            "-frames:v",
            "1",
            str(dst),
        ])
        items.append((f"training: {name}", dst))
    return items


def _live_camera_items(captured_dir: Path, only_current: bool) -> list[tuple[str, Path]]:
    paths = sorted(captured_dir.glob("opencv__dev_video*.png"), key=_video_sort_key)
    if only_current:
        current = _current_video_devices()
        paths = [p for p in paths if _device_from_snapshot(p) in current]
    return [(f"current: {_device_from_snapshot(p)}", p) for p in paths]


def _video_sort_key(path: Path) -> int:
    match = re.search(r"video(\d+)", path.name)
    return int(match.group(1)) if match else 10_000


def _device_from_snapshot(path: Path) -> str:
    match = re.search(r"opencv__dev_(video\d+)\.png$", path.name)
    return f"/dev/{match.group(1)}" if match else path.stem


def _current_video_devices() -> set[str]:
    return {str(p) for p in Path("/dev").glob("video*")}


def _font(size: int):
    for candidate in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _make_sheet(items: list[tuple[str, Path]], output: Path, cols: int, thumb_w: int, thumb_h: int) -> None:
    if not items:
        raise ValueError("No images available for the mapping sheet")

    label_h = 34
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * thumb_w, rows * (thumb_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    font = _font(18)

    for i, (label, path) in enumerate(items):
        x = (i % cols) * thumb_w
        y = (i // cols) * (thumb_h + label_h)
        img = Image.open(path).convert("RGB")
        img.thumbnail((thumb_w, thumb_h))
        px = x + (thumb_w - img.width) // 2
        py = y + label_h + (thumb_h - img.height) // 2
        draw.rectangle([x, y, x + thumb_w - 1, y + label_h - 1], fill=(235, 235, 235))
        draw.text((x + 8, y + 7), label, fill=(0, 0, 0), font=font)
        sheet.paste(img, (px, py))

    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data", type=Path, help="LeRobot dataset root containing meta/info.json")
    parser.add_argument("--captured", default=Path("outputs/captured_images"), type=Path, help="Directory created by lerobot-find-cameras")
    parser.add_argument("--output", default=Path("outputs/camera_mapping/camera_mapping_sheet.png"), type=Path, help="Output PNG path")
    parser.add_argument("--seconds", default=3.0, type=float, help="Timestamp to sample from each training video")
    parser.add_argument("--cols", default=3, type=int, help="Number of columns in the sheet")
    parser.add_argument("--refresh", action="store_true", help="Run lerobot-find-cameras before building the sheet")
    parser.add_argument("--include-stale", action="store_true", help="Include old snapshots for devices not currently present under /dev/video*")
    args = parser.parse_args()

    if args.refresh:
        _run(["lerobot-find-cameras"])

    out_dir = args.output.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    items = _extract_training_frames(args.data, out_dir, args.seconds)
    items.extend(_live_camera_items(args.captured, only_current=not args.include_stale))

    _make_sheet(items, args.output, cols=args.cols, thumb_w=320, thumb_h=240)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
