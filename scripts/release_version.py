#!/usr/bin/env python3
import argparse
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-ref-name", required=True)
    args = parser.parse_args()

    version = project_version()
    expected_tag = f"v{version}"
    if args.github_ref_name != expected_tag:
        print(
            f"GITHUB_REF_NAME {args.github_ref_name!r} does not match project version tag "
            f"{expected_tag!r}",
            file=sys.stderr,
        )
        return 1

    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
