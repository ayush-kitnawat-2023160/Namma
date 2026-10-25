#!/usr/bin/env python3
from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path

import torch


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a Python script in-process after applying a CUDA allocator limit",
        add_help=False,
    )
    parser.add_argument("--fraction", type=float, default=0.90)
    parser.add_argument("script")
    args, script_args = parser.parse_known_args()

    if not 0.20 <= args.fraction <= 0.98:
        raise SystemExit(f"Unsafe/invalid GPU memory fraction: {args.fraction}")
    if torch.cuda.is_available():
        torch.cuda.set_per_process_memory_fraction(args.fraction, device=0)
        print(f"[GPU LIMIT] PyTorch process memory fraction capped at {args.fraction:.2f}")

    script = Path(args.script)
    if not script.is_file():
        raise SystemExit(f"Python entry script not found: {script}")
    sys.argv = [str(script), *script_args]
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
