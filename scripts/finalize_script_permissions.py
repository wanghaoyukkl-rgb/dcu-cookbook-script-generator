#!/usr/bin/env python3
"""Add the required sharing permissions to a generated serve script and its directory."""

import argparse
import json
import stat
import sys
from pathlib import Path


SCRIPT_PERMISSIONS = (
    stat.S_IXUSR
    | stat.S_IRGRP
    | stat.S_IWGRP
    | stat.S_IROTH
    | stat.S_IWOTH
)
DIRECTORY_PERMISSIONS = stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH


def octal_mode(path):
    return "{:04o}".format(stat.S_IMODE(path.stat().st_mode))


def finalize_permissions(script_path):
    script_path = script_path.expanduser()
    if not script_path.is_absolute():
        raise ValueError("script path must be absolute or start with ~")
    if script_path.is_symlink() or not script_path.is_file():
        raise ValueError("script path must be a regular file, not a symlink")

    output_dir = script_path.parent
    script_path.chmod(stat.S_IMODE(script_path.stat().st_mode) | SCRIPT_PERMISSIONS)
    output_dir.chmod(stat.S_IMODE(output_dir.stat().st_mode) | DIRECTORY_PERMISSIONS)
    return {
        "script_path": str(script_path),
        "script_mode": octal_mode(script_path),
        "output_dir": str(output_dir),
        "output_dir_mode": octal_mode(output_dir),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Add required group/other permissions to a generated serve script."
    )
    parser.add_argument("--script-path", required=True, help="Absolute generated script path.")
    args = parser.parse_args()

    try:
        result = finalize_permissions(Path(args.script_path))
    except (OSError, ValueError) as exc:
        print("permission finalization failed: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
