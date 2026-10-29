#!/usr/bin/env python3
"""Supervise one reconstruction process tree without importing ML libraries."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

GIB = 1024 ** 3


def bounded_env(name: str, default: float, minimum: float, maximum: float) -> float:
    value = float(os.environ.get(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}; got {value}")
    return value


def limits() -> dict[str, float]:
    return {
        "max_gpu_temp_c": bounded_env("PR_MAX_GPU_TEMP_C", 83, 60, 83),
        "min_free_vram_gb": bounded_env("PR_MIN_FREE_VRAM_GB", 1, 1, 16),
        "min_host_ram_gb": bounded_env("PR_MIN_HOST_RAM_GB", 12, 12, 96),
        "min_free_disk_gb": bounded_env("PR_MIN_FREE_DISK_GB", 15, 15, 4096),
        "poll_seconds": bounded_env("PR_WATCHDOG_INTERVAL_SECONDS", 5, 1, 5),
    }


def snapshot(output: Path) -> dict[str, float]:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.free,temperature.gpu", "--format=csv,noheader,nounits"],
        check=True, capture_output=True, text=True, timeout=4,
    )
    rows = [line for line in result.stdout.splitlines() if line.strip()]
    if len(rows) != 1:
        raise RuntimeError(f"Expected one visible GPU; telemetry reported {len(rows)}")
    memory, temperature = [float(value.strip()) for value in rows[0].split(",")]
    meminfo = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    if "MemAvailable" not in meminfo:
        raise RuntimeError("Host MemAvailable telemetry is missing")
    available_kb = float(meminfo["MemAvailable"].split()[0])
    return {
        "free_vram_gb": memory / 1024,
        "gpu_temp_c": temperature,
        "host_available_ram_gb": available_kb * 1024 / GIB,
        "free_disk_gb": shutil.disk_usage(output).free / GIB,
    }


def unsafe_reason(sample: dict[str, float], threshold: dict[str, float]) -> str | None:
    comparisons = (
        ("gpu_temp_c", "max_gpu_temp_c", True),
        ("free_vram_gb", "min_free_vram_gb", False),
        ("host_available_ram_gb", "min_host_ram_gb", False),
        ("free_disk_gb", "min_free_disk_gb", False),
    )
    for metric, limit, upper in comparisons:
        value, boundary = sample[metric], threshold[limit]
        if not (float("-inf") < value < float("inf")):
            return f"Invalid {metric} telemetry: {value}"
        crossed = value >= boundary if upper else value < boundary
        if crossed:
            return f"{metric}={value:.2f} crossed {limit}={boundary:.2f}"
    return None


def stop_tree(child: subprocess.Popen) -> None:
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    # The group can outlive its leading process (for example CUDA worker jobs).
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def exit_code(code: int) -> int:
    return code if code >= 0 else 128 - code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("Provide a child command after --")
    output = Path("/workspace/outputs/nextgen")
    log_dir = Path("/workspace/outputs/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"precision_watchdog_{stamp}_{os.getpid()}.jsonl"
    received: list[int] = []
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, lambda signum, frame: received.append(signum))

    def record(event: str, **fields) -> None:
        item = {"time": dt.datetime.now(dt.timezone.utc).isoformat(), "stage": args.stage, "event": event, **fields}
        with log_path.open("a") as handle:
            handle.write(json.dumps(item, allow_nan=False) + "\n")
        if event != "sample":
            print(f"[precision watchdog] {event}: {fields}", flush=True)

    child = None
    try:
        record("startup", log=str(log_path))
        threshold = limits()
        sample = snapshot(output)
        reason = unsafe_reason(sample, threshold)
        record("preflight", limits=threshold, sample=sample)
        if reason:
            record("stop", reason=reason, exit_code=75)
            return 75
        if received:
            record("stop", reason="signal before child start", exit_code=128 + received[0])
            return 128 + received[0]
        child = subprocess.Popen(command, start_new_session=True)
        while True:
            code = child.poll()
            if code is not None:
                # A failed helper can leave workers behind after its shell has
                # exited. Reap any remaining members of this dedicated group.
                stop_tree(child)
                record("stop", reason="child exited", child_exit_code=code, exit_code=exit_code(code))
                return exit_code(code)
            if received:
                stop_tree(child)
                record("stop", reason=f"received signal {received[0]}", exit_code=128 + received[0])
                return 128 + received[0]
            sample = snapshot(output)
            reason = unsafe_reason(sample, threshold)
            if reason:
                stop_tree(child)
                record("stop", reason=reason, sample=sample, exit_code=75)
                return 75
            record("sample", sample=sample)
            # Short waits keep Docker stop / Ctrl-C responsive between samples.
            deadline = time.monotonic() + threshold["poll_seconds"]
            while not received and child.poll() is None and time.monotonic() < deadline:
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        if child is not None:
            stop_tree(child)
        record("stop", reason=f"Safety telemetry/configuration failed: {exc}", exit_code=75)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
