#!/usr/bin/env python3
"""Merge a LeRobot v2.1 dataset and a compatible v3.0 dataset into v2.1.

LeRobot 0.3.3 (the version pinned by this repository) trains from v2.1
datasets.  The v3 dataset is converted instead of being copied beside v2 files:
each shared v3 video is split into one video per episode, and the associated
Parquet and metadata are renumbered into v2.1's layout.  All videos are
encoded as H.264 so the merged metadata has one truthful codec declaration.

The sources are never modified.  The output must not exist.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

import av
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


VIDEO_KEYS = (
    "observation.images.front",
    "observation.images.upper",
    "observation.images.depth",
)


def read_json(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(value, f, indent=4)
        f.write("\n")


def read_jsonl(path: Path) -> list[dict]:
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, allow_nan=False))
            f.write("\n")


def load_v3_episodes(source: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((source / "meta" / "episodes").rglob("*.parquet")):
        rows.extend(pq.read_table(path).to_pylist())
    rows.sort(key=lambda row: row["episode_index"])
    expected = list(range(len(rows)))
    actual = [row["episode_index"] for row in rows]
    if actual != expected:
        raise ValueError(f"v3 episode indices are not contiguous: {actual}")
    return rows


def v3_stats_to_v2(row: dict, episode_index: int, global_start: int) -> dict:
    """Convert v3's flattened per-episode stats into v2's JSON object."""
    length = row["length"]
    features = ("action", "observation.state", *VIDEO_KEYS, "timestamp", "frame_index", "episode_index", "index", "task_index")
    stats: dict[str, dict] = {}
    for feature in features:
        prefix = f"stats/{feature}/"
        stats[feature] = {
            key.removeprefix(prefix): value
            for key, value in row.items()
            if key.startswith(prefix) and key.removeprefix(prefix) in {"min", "max", "mean", "std", "count"}
        }

    # v3 episode indices and global frame indices refer to the source dataset.
    stats["episode_index"] = {
        "min": [episode_index], "max": [episode_index], "mean": [float(episode_index)],
        "std": [0.0], "count": [length],
    }
    stats["index"] = {
        "min": [global_start], "max": [global_start + length - 1],
        "mean": [global_start + (length - 1) / 2],
        "std": [((length**2 - 1) / 12) ** 0.5], "count": [length],
    }
    return {"episode_index": episode_index, "stats": stats}


def split_video(source: Path, start_s: float, frames: int, target: Path, fps: int) -> None:
    """Extract exactly ``frames`` images from a source video into a v2 video."""
    target.parent.mkdir(parents=True, exist_ok=True)
    input_container = av.open(source)
    input_stream = input_container.streams.video[0]
    output_container = av.open(target, "w")
    output_stream = output_container.add_stream("libx264", rate=fps)
    output_stream.width = input_stream.width
    output_stream.height = input_stream.height
    output_stream.pix_fmt = "yuv420p"
    output_stream.options = {"preset": "veryfast", "crf": "23"}

    # Timestamps in v3 metadata lie exactly on the 30 Hz video clock. Seek to
    # the preceding keyframe, discard earlier decoded frames, and rebase output
    # PTS so a v2 loader sees frame i at i / fps.
    input_container.seek(int(start_s * av.time_base), any_frame=False, backward=True)
    decoded = 0
    for frame in input_container.decode(input_stream):
        if frame.time is None or frame.time + 0.5 / fps < start_s:
            continue
        frame.pts = decoded
        frame.time_base = Fraction(1, fps)
        for packet in output_stream.encode(frame):
            output_container.mux(packet)
        decoded += 1
        if decoded == frames:
            break
    for packet in output_stream.encode():
        output_container.mux(packet)
    output_container.close()
    input_container.close()
    if decoded != frames:
        raise ValueError(f"{source}: requested {frames} frames at {start_s}s, decoded {decoded}")


def renumber_v3_parquet(source: Path, episode: dict, output_episode: int, global_start: int, target: Path) -> None:
    file_index = episode["data/file_index"]
    source_path = source / "data" / "chunk-000" / f"file-{file_index:03d}.parquet"
    start = episode["dataset_from_index"]
    length = episode["length"]
    # dataset_from_index is global in v3, while each parquet file starts at a
    # known global offset.  Locate the requested rows from the episode range.
    source_table = pq.read_table(source_path)
    episode_mask = pc.equal(source_table["episode_index"], pa.scalar(episode["episode_index"], pa.int64()))
    table = source_table.filter(episode_mask)
    if table.num_rows != length:
        raise ValueError(f"episode {episode['episode_index']}: expected {length} parquet rows, got {table.num_rows}")

    arrays = []
    for field in table.schema:
        column = table[field.name]
        if field.name == "episode_index":
            column = pa.chunked_array([pa.array([output_episode] * length, type=pa.int64())])
        elif field.name == "index":
            column = pa.chunked_array([pa.array(range(global_start, global_start + length), type=pa.int64())])
        arrays.append(column)
    result = pa.Table.from_arrays(arrays, schema=table.schema)
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(result, target)


def validate_output(root: Path, episodes: int, frames: int) -> None:
    info = read_json(root / "meta" / "info.json")
    assert info["codebase_version"] == "v2.1"
    assert info["total_episodes"] == episodes
    assert info["total_frames"] == frames
    episode_rows = read_jsonl(root / "meta" / "episodes.jsonl")
    stat_rows = read_jsonl(root / "meta" / "episodes_stats.jsonl")
    assert len(episode_rows) == len(stat_rows) == episodes
    assert [row["episode_index"] for row in episode_rows] == list(range(episodes))
    assert sum(row["length"] for row in episode_rows) == frames
    for episode in (0, episodes - 1):
        data = root / "data" / "chunk-000" / f"episode_{episode:06d}.parquet"
        assert data.is_file(), data
        table = pq.read_table(data, columns=["episode_index", "index"])
        assert set(table["episode_index"].to_pylist()) == {episode}
    for key in VIDEO_KEYS:
        for episode in (0, episodes - 1):
            video = root / "videos" / "chunk-000" / key / f"episode_{episode:06d}.mp4"
            assert video.is_file(), video


def merge(v2: Path, v3: Path, output: Path) -> None:
    v2_info = read_json(v2 / "meta" / "info.json")
    v3_info = read_json(v3 / "meta" / "info.json")
    if v2_info.get("codebase_version") != "v2.1" or v3_info.get("codebase_version") != "v3.0":
        raise ValueError("Expected a v2.1 first source and a v3.0 second source.")
    if v2_info["fps"] != v3_info["fps"]:
        raise ValueError("Datasets have different FPS values.")
    if v2_info["features"] != v3_info["features"]:
        raise ValueError("Datasets have incompatible feature definitions.")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {output}")

    v2_episodes = read_jsonl(v2 / "meta" / "episodes.jsonl")
    v2_stats = read_jsonl(v2 / "meta" / "episodes_stats.jsonl")
    v3_episodes = load_v3_episodes(v3)
    v2_task_rows = read_jsonl(v2 / "meta" / "tasks.jsonl")
    v3_tasks = pq.read_table(v3 / "meta" / "tasks.parquet").to_pylist()
    if v2_task_rows != v3_tasks:
        raise ValueError("Datasets use different tasks/task indices; task remapping is not implemented.")

    output.mkdir()
    try:
        # Existing v2 Parquet files already use the target layout. Videos are
        # re-encoded below so one feature declaration accurately describes all
        # episodes in the merged dataset. Do not copy source .git/.cache files.
        shutil.copytree(v2 / "data", output / "data")

        output_episodes = list(v2_episodes)
        output_stats = list(v2_stats)
        global_start = v2_info["total_frames"]
        fps = v2_info["fps"]

        for episode in v2_episodes:
            episode_index = episode["episode_index"]
            print(f"Transcoding v2 episode {episode_index:02d} ({episode['length']} frames)", flush=True)
            for key in VIDEO_KEYS:
                split_video(
                    v2 / "videos" / "chunk-000" / key / f"episode_{episode_index:06d}.mp4",
                    0.0, episode["length"],
                    output / "videos" / "chunk-000" / key / f"episode_{episode_index:06d}.mp4", fps,
                )

        for v3_episode in v3_episodes:
            source_episode = v3_episode["episode_index"]
            output_episode = len(output_episodes)
            length = v3_episode["length"]
            print(f"Converting v3 episode {source_episode:02d} -> v2 episode {output_episode:02d} ({length} frames)", flush=True)
            renumber_v3_parquet(
                v3, v3_episode, output_episode, global_start,
                output / "data" / "chunk-000" / f"episode_{output_episode:06d}.parquet",
            )
            for key in VIDEO_KEYS:
                source_video = v3 / "videos" / key / "chunk-000" / f"file-{v3_episode[f'videos/{key}/file_index']:03d}.mp4"
                split_video(
                    source_video, v3_episode[f"videos/{key}/from_timestamp"], length,
                    output / "videos" / "chunk-000" / key / f"episode_{output_episode:06d}.mp4", fps,
                )
            output_episodes.append({"episode_index": output_episode, "tasks": v3_episode["tasks"], "length": length})
            output_stats.append(v3_stats_to_v2(v3_episode, output_episode, global_start))
            global_start += length

        info = dict(v2_info)
        info["total_episodes"] = len(output_episodes)
        info["total_frames"] = global_start
        info["splits"] = {"train": f"0:{len(output_episodes)}"}
        for key in VIDEO_KEYS:
            info["features"][key]["info"]["video.codec"] = "h264"
        write_json(output / "meta" / "info.json", info)
        write_jsonl(output / "meta" / "episodes.jsonl", output_episodes)
        write_jsonl(output / "meta" / "episodes_stats.jsonl", output_stats)
        write_jsonl(output / "meta" / "tasks.jsonl", v2_task_rows)
        validate_output(output, len(output_episodes), global_start)
    except BaseException:
        # A partial dataset can look valid enough to be used accidentally.
        shutil.rmtree(output, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v2", type=Path, default=Path("data/record-0704"), help="LeRobot v2.1 source dataset")
    parser.add_argument("--v3", type=Path, default=Path("data/record-testv2"), help="LeRobot v3.0 source dataset")
    parser.add_argument("--output", type=Path, default=Path("data/record-merged"), help="new v2.1 dataset directory")
    args = parser.parse_args()
    merge(args.v2.resolve(), args.v3.resolve(), args.output.resolve())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
