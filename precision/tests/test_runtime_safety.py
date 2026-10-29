"""Small stdlib checks. No CUDA, Docker daemon, model imports, or real inputs."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("runtime_watchdog", ROOT / "precision/scripts/runtime_watchdog.py")
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)
SAFE = {"free_vram_gb": 8, "gpu_temp_c": 60, "host_available_ram_gb": 40, "free_disk_gb": 100}


class WatchdogTests(unittest.TestCase):
    def test_all_hard_limits_and_boundaries(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            threshold = watchdog.limits()
        self.assertIsNone(watchdog.unsafe_reason(SAFE, threshold))
        for metric, unsafe in (("free_vram_gb", .99), ("gpu_temp_c", 83), ("host_available_ram_gb", 11.99), ("free_disk_gb", 14.99)):
            with self.subTest(metric=metric):
                self.assertIn(metric, watchdog.unsafe_reason({**SAFE, metric: unsafe}, threshold))
        self.assertIsNone(watchdog.unsafe_reason({**SAFE, "free_vram_gb": 1, "free_disk_gb": 15, "host_available_ram_gb": 12}, threshold))
        self.assertIn("Invalid", watchdog.unsafe_reason({**SAFE, "gpu_temp_c": float("nan")}, threshold))

    def test_environment_cannot_disable_safety(self):
        for name, value in (("PR_MAX_GPU_TEMP_C", "100"), ("PR_MIN_FREE_VRAM_GB", "0"), ("PR_MIN_HOST_RAM_GB", "0"), ("PR_MIN_FREE_DISK_GB", "0"), ("PR_WATCHDOG_INTERVAL_SECONDS", "60")):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: value}, clear=True):
                with self.assertRaises(ValueError):
                    watchdog.limits()

    def run_main(self, samples, command):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            paths = {"/workspace/outputs/nextgen": directory, "/workspace/outputs/logs": directory}
            with mock.patch.object(watchdog, "Path", side_effect=lambda path: paths.get(str(path), Path(path))), \
                 mock.patch.object(watchdog, "snapshot", side_effect=samples), \
                 mock.patch.object(sys, "argv", ["watchdog", "--stage", "test", "--", *command]), \
                 mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
                code = watchdog.main()
            records = [json.loads(line) for file in directory.glob("*.jsonl") for line in file.read_text().splitlines()]
            self.assertEqual(records[0]["event"], "startup")
            self.assertEqual(records[-1]["event"], "stop")
            return code, records

    def test_preserves_child_error(self):
        code, records = self.run_main(lambda _: SAFE.copy(), [sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(code, 7)
        self.assertEqual(records[-1]["child_exit_code"], 7)

    def test_orphan_worker_is_stopped_after_its_parent_exits(self):
        with tempfile.TemporaryDirectory() as folder:
            pid_file = Path(folder) / 'worker.pid'
            program = ("import pathlib,subprocess,sys; "
                       "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                       "pathlib.Path(sys.argv[1]).write_text(str(p.pid))")
            code, _ = self.run_main(lambda _: SAFE.copy(), [sys.executable, '-c', program, str(pid_file)])
            worker = int(pid_file.read_text())
            try:
                self.assertEqual(code, 0)
                stat = Path(f'/proc/{worker}/stat')
                deadline = time.monotonic() + 1
                while stat.exists() and time.monotonic() < deadline:
                    try:
                        if stat.read_text().split()[2] == 'Z':
                            break
                    except FileNotFoundError:
                        break
                    time.sleep(.01)
                if stat.exists():
                    self.assertEqual(stat.read_text().split()[2], 'Z', 'Worker remains active after job completion')
            finally:
                try:
                    os.kill(worker, 9)
                except ProcessLookupError:
                    pass

    def test_stops_on_unsafe_runtime_telemetry(self):
        code, records = self.run_main([SAFE, {**SAFE, "gpu_temp_c": 85}], [sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertEqual(code, 75)
        self.assertIn("gpu_temp_c", records[-1]["reason"])

    def test_fails_closed_when_telemetry_unavailable(self):
        code, records = self.run_main(RuntimeError("missing GPU telemetry"), [sys.executable, "-c", "raise SystemExit(99)"])
        self.assertEqual(code, 75)
        self.assertIn("missing GPU telemetry", records[-1]["reason"])

    def test_exit_code_for_signal(self):
        self.assertEqual(watchdog.exit_code(-15), 143)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="precision tests ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copy(ROOT / "run_precision.sh", self.root / "run_precision.sh")
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        self.log = self.root / "docker.json"
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "NAMMA_SERVER": "1", "FAKE_DOCKER_LOG": str(self.log)}
        for name in list(self.env):
            if name.startswith(("NG_", "PR_")):
                del self.env[name]
        self.fake("docker", "import json, os, sys\nif sys.argv[1] == 'info':\n print(os.environ.get('FAKE_DOCKER_LIMITS', 'true true'))\nelse:\n open(os.environ['FAKE_DOCKER_LOG'], 'w').write(json.dumps(sys.argv[1:]))")
        self.fake("nvidia-smi", "import os\nprint(os.environ.get('FAKE_GPU_INFO', '24576, 22000, 60'))")
        self.fake("awk", "import os\nprint(os.environ.get('FAKE_MEM_INFO', '134217728 120000000'))")

    def fake(self, name, code):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n{code}\n")
        path.chmod(0o755)

    def run_launcher(self, *args, **env):
        return subprocess.run(["bash", str(self.root / "run_precision.sh"), *args], env={**self.env, **env}, text=True, capture_output=True, timeout=10)

    def test_help_is_local_and_heavy_requires_opt_in(self):
        self.assertEqual(self.run_launcher("help", NAMMA_SERVER="0").returncode, 0)
        result = self.run_launcher("pipeline", "video.mp4", "scene", NAMMA_SERVER="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Server-only", result.stderr)
        self.assertFalse(self.log.exists())

    def test_heavy_resource_flags_gpu_selection_and_quoting(self):
        result = self.run_launcher("pipeline", "a video.mp4", "scene", NG_GPU_DEVICE="GPU-abc", PR_TEST="a b")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.log.read_text())
        for expected in ("--memory=80g", "--memory-swap=80g", "--cpus=8", "--shm-size=4g", "--pids-limit=1024", "device=GPU-abc", "NG_GS_DATA_DEVICE=cpu", "PR_TEST=a b"):
            self.assertIn(expected, args)
        self.assertEqual(args[-3:], ["pipeline", "a video.mp4", "scene"])
        self.assertIn("/opt/namma-precision/scripts/runtime_watchdog.py", args)

    def test_recovery_reuses_a_read_only_precision_source_and_keeps_guards(self):
        source = self.root / 'outputs/precision/failed_scene'
        source.mkdir(parents=True)
        result = self.run_launcher('recover', 'failed_scene', 'new_scene', PR_ALLOW_VIEW_PRUNING='1', PR_TRAIN='0')
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.log.read_text())
        self.assertIn(f'{source}:/workspace/source:ro', args)
        self.assertIn('--memory=80g', args)
        self.assertIn('device=0', args)
        self.assertIn('PR_ALLOW_VIEW_PRUNING=1', args)
        self.assertIn('/opt/namma-precision/scripts/runtime_watchdog.py', args)
        self.assertEqual(args[-3:], ['recover', 'failed_scene', 'new_scene'])

    def test_audit_is_a_cpu_only_container(self):
        result = self.run_launcher('audit', 'failed_scene')
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.log.read_text())
        self.assertNotIn('--gpus', args)
        self.assertNotIn('/opt/namma-precision/scripts/runtime_watchdog.py', args)
        self.assertEqual(args[-2:], ['audit', 'failed_scene'])

    def test_rejects_paths_existing_outputs_and_small_gpu(self):
        for scene in ("../scene", "scene/child", "-scene"):
            self.assertNotEqual(self.run_launcher("train", scene).returncode, 0)
        destination = self.root / "outputs/precision/scene"
        destination.mkdir(parents=True)
        self.assertIn("Destination exists", self.run_launcher("pipeline", "v.mp4", "scene").stderr)
        self.assertNotEqual(self.run_launcher("train", "scene", FAKE_GPU_INFO="8192, 8000, 60").returncode, 0)
        self.assertFalse(self.log.exists())

    def test_rejects_low_host_ram_or_unenforced_docker_limits(self):
        for env, phrase in (({"FAKE_MEM_INFO": "8388608 7000000"}, "physical host RAM"),
                            ({"FAKE_MEM_INFO": "134217728 80000000"}, "currently available"),
                            ({"FAKE_DOCKER_LIMITS": "true false"}, "cgroup limits")):
            with self.subTest(env=env):
                result = self.run_launcher("train", "scene", **env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(phrase, result.stderr)
                self.assertFalse(self.log.exists())

    def test_source_read_only_and_cpu_check(self):
        source = self.root / "outputs/nextgen/source"
        source.mkdir(parents=True)
        result = self.run_launcher("refine", "source", "newscene")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{source}:/workspace/source:ro", json.loads(self.log.read_text()))
        result = self.run_launcher("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--gpus", json.loads(self.log.read_text()))

    def test_single_precision_job_lock(self):
        import fcntl
        runtime = self.root / ".runtime"
        runtime.mkdir()
        with (runtime / "precision.lock").open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_launcher("train", "scene")
        self.assertIn("already running", result.stderr)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
