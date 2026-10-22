"""Package Lambdas that need third-party libraries (currently the ops assistant).

Installs Linux x86_64 wheels for Python 3.12 into build/<function>/ so the zip
Terraform creates runs on Lambda even when built on macOS or Windows.

    python scripts/build_lambdas.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = {"lambda_ops_assistant": ROOT / "app" / "lambda_ops_assistant"}


def build(name: str, source: Path) -> Path:
    target = ROOT / "build" / name
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--quiet",
            "--target", str(target),
            "--platform", "manylinux2014_x86_64",
            "--implementation", "cp",
            "--python-version", "3.12",
            "--only-binary=:all:",
            "-r", str(source / "requirements.txt"),
        ],
        check=True,
    )
    shutil.copy2(source / "handler.py", target / "handler.py")
    return target


if __name__ == "__main__":
    for function_name, source_dir in FUNCTIONS.items():
        print(f"Built {function_name} -> {build(function_name, source_dir)}")
