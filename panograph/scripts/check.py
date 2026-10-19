#!/usr/bin/env python3
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path


def version(module_name: str) -> str:
    module = importlib.import_module(module_name)
    return str(getattr(module, "__version__", "ok"))


def main() -> None:
    report = {
        "python": sys.version.split()[0],
        "torch": version("torch"),
        "numpy": version("numpy"),
        "PIL": version("PIL"),
        "yaml": version("yaml"),
        "huggingface_hub": version("huggingface_hub"),
        "panovggt_repo": Path("/opt/PanoVGGT").is_dir(),
        "sphr_repo": Path("/opt/sphr").is_dir(),
        "sphr_build": (Path("/opt/sphr") / ".next").is_dir(),
    }
    try:
        report["node"] = subprocess.check_output(["node", "--version"], text=True).strip()
        report["npm"] = subprocess.check_output(["npm", "--version"], text=True).strip()
    except Exception as exc:
        report["node_error"] = str(exc)

    try:
        from panovggt.models.panovggt_model import PanoVGGTModel  # noqa: F401
        report["panovggt_import"] = True
    except Exception as exc:
        report["panovggt_import"] = False
        report["panovggt_error"] = repr(exc)

    print(json.dumps(report, indent=2))
    required = ["panovggt_repo", "sphr_repo", "sphr_build", "panovggt_import"]
    if not all(report.get(key) for key in required):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
