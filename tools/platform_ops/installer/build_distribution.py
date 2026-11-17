"""Create one user download from the exact compiled installer and payload."""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile


def sha(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.build / "package.json").read_text(encoding="utf-8"))
    if manifest["compiler_exit"] != 0 or manifest["engineering_fixture"]:
        parser.error("A successful non-fixture compilation is required")
    exe = args.build / "FinanceAnalyze-Setup.exe"
    payload = args.build / "FinanceAnalyze.payload.zip"
    files = ((exe, "setup_sha256"), (payload, "payload_sha256"))
    for file, key in files:
        if sha(file) != manifest[key]:
            parser.error(f"Unexpected hash: {file.name}")
    with ZipFile(args.output, "x", compression=ZIP_STORED) as archive:
        for file, _ in files:
            archive.write(file, file.name)
        guide = Path(__file__).with_name("快速安装.txt")
        archive.write(guide, guide.name)
    print(json.dumps({"sha256": sha(args.output), "bytes": args.output.stat().st_size}))


if __name__ == "__main__":
    main()
