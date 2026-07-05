import signal
import subprocess
import sys
import time
from pathlib import Path


def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()


def main() -> None:
    root_dir = Path(__file__).resolve().parent.parent

    fastapi_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "src.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ]
    streamlit_cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "streamlit_app.py",
    ]

    fastapi_proc = subprocess.Popen(fastapi_cmd, cwd=root_dir)
    streamlit_proc = subprocess.Popen(streamlit_cmd, cwd=root_dir)

    def _shutdown(*_args):
        _terminate_process(fastapi_proc)
        _terminate_process(streamlit_proc)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while True:
            if fastapi_proc.poll() is not None or streamlit_proc.poll() is not None:
                break
            time.sleep(0.25)
    finally:
        _shutdown()

    if fastapi_proc.returncode not in (None, 0):
        raise SystemExit(fastapi_proc.returncode)
    if streamlit_proc.returncode not in (None, 0):
        raise SystemExit(streamlit_proc.returncode)
