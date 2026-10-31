#!/usr/bin/env python3
"""Build committed application source from ../rawroute without deploying it."""

from pathlib import Path
import subprocess


def main():
    source = Path(__file__).resolve().parent.parent / "rawroute"
    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()
    if git("status", "--porcelain", "--untracked-files=all"):
        raise SystemExit("Application source must be clean and committed before building.")
    revision = git("rev-parse", "HEAD")
    image = f"rawroute-local:sha-{revision}"
    subprocess.run(["docker", "build", "--build-arg", f"DEPLOYMENT_VERSION={revision}",
                    "-t", image, str(source)], check=True)
    print(f"Built {image}")
    print("This local image is for isolated development; deploy.py accepts published immutable tags or digests only.")


if __name__ == "__main__":
    main()
