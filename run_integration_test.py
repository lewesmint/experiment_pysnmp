"""Launch snmp-sim, wait for it to be ready, run the integration test script, then shut it down.

Usage (from experiment_pysnmp folder with its venv active):
    python run_integration_test.py [--script integration_test.txt]
"""

from __future__ import annotations

import argparse
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

SNMP_SIM_DIR = Path(__file__).resolve().parent.parent.parent / "snmp-sim"
SNMP_SIM_PYTHON = SNMP_SIM_DIR / ".venv" / "bin" / "python"
SNMP_SIM_ENTRY = SNMP_SIM_DIR / "run_agent_with_rest.py"

REST_HOST = "127.0.0.1"
REST_PORT = 8800
SNMP_HOST = "127.0.0.1"
SNMP_PORT = 11161

AGENT_READY_TIMEOUT = 30.0   # seconds to wait for agent startup
AGENT_POLL_INTERVAL = 0.25


def _tcp_reachable(host: str, port: int) -> bool:
    """Return True if a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _udp_reachable(host: str, port: int) -> bool:
    """Return True if the UDP port appears open (sends a byte, no error)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            sock.sendto(b"\x00", (host, port))
        return True
    except OSError:
        return False


def _wait_for_agent(timeout: float) -> bool:
    """Block until the REST API and SNMP ports are reachable, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _tcp_reachable(REST_HOST, REST_PORT) and _udp_reachable(SNMP_HOST, SNMP_PORT):
            return True
        time.sleep(AGENT_POLL_INTERVAL)
    return False


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="End-to-end integration test runner")
    parser.add_argument(
        "--script",
        default="integration_test.txt",
        help="path to the manager command script (default: integration_test.txt)",
    )
    parser.add_argument(
        "--agent-url",
        default=f"http://{REST_HOST}:{REST_PORT}",
        help="snmp-sim REST API base URL",
    )
    return parser.parse_args()


def main() -> None:
    """Run the simulator process and manager script as a single E2E flow."""
    args = _parse_args()

    if not SNMP_SIM_PYTHON.exists():
        print(f"ERROR: snmp-sim venv not found at {SNMP_SIM_PYTHON}")
        sys.exit(1)
    if not SNMP_SIM_ENTRY.exists():
        print(f"ERROR: run_agent_with_rest.py not found at {SNMP_SIM_ENTRY}")
        sys.exit(1)

    print(f"Starting snmp-sim agent from {SNMP_SIM_DIR} ...")
    result_code = 1
    with subprocess.Popen(
        [str(SNMP_SIM_PYTHON), str(SNMP_SIM_ENTRY)],
        cwd=str(SNMP_SIM_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as agent_proc:
        try:
            print(f"Waiting up to {AGENT_READY_TIMEOUT}s for agent on "
                  f"REST:{REST_PORT} / SNMP:{SNMP_PORT} ...")
            if not _wait_for_agent(AGENT_READY_TIMEOUT):
                print("ERROR: agent did not become reachable in time")
                agent_proc.terminate()
                sys.exit(1)
            print("Agent is ready.\n")

            manager_python = Path(sys.executable)
            manager_script = Path(__file__).parent / "manager_app.py"
            cmd = [
                str(manager_python),
                str(manager_script),
                "--startup-listen",
                "--script", args.script,
                "--agent-url", args.agent_url,
            ]
            result = subprocess.run(
                cmd,
                cwd=str(Path(__file__).parent),
                check=False,
            )
            result_code = result.returncode

        finally:
            print("\nStopping snmp-sim agent ...")
            agent_proc.send_signal(signal.SIGINT)
            try:
                agent_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                agent_proc.kill()

    sys.exit(result_code)


if __name__ == "__main__":
    main()
