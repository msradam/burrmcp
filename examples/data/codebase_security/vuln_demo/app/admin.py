"""Admin operations. DO NOT USE: deliberately vulnerable."""

import os
import subprocess


def ping_host(host: str) -> str:
    """Reach out to ``host``. CWE-78: command injection via shell=True."""
    return subprocess.check_output(  # B602
        f"ping -c 1 {host}", shell=True, text=True
    )


def run_diagnostic(cmd: str) -> int:
    """Run an arbitrary diagnostic. CWE-78: os.system on user input."""
    return os.system(cmd)  # B605
