"""A Windows venv launcher must not hide memory in the actual worker process."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def test_process_tree_sampling_includes_allocated_worker_memory():
    path = Path(__file__).parents[1] / 'scripts/check_company_workflow.py'
    spec = importlib.util.spec_from_file_location('workflow_measurement', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    allocation = 80 * 1024 * 1024
    program = 'import sys; block=bytearray(80*1024*1024); print("ready",flush=True); sys.stdin.readline()'
    process = subprocess.Popen([sys.executable, '-c', program], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        assert process.stdout.readline().strip() == 'ready'
        assert module.process_tree_rss(process.pid) >= allocation
    finally:
        process.communicate(input='stop\n', timeout=10)
    assert process.returncode == 0
