"""
Build the YouTube extension package.

    python build.py                                    unsigned dist/kareokie-ext-youtube-<version>.zip
    python build.py --unsigned                         unsigned even when KAREOKIE_SIGNING_KEY is set (testing)
    python build.py --key <private.pem> --key-id <id>  signed dist/kareokie-ext-youtube-<version>.kext
                                                       (or set KAREOKIE_SIGNING_KEY / KAREOKIE_SIGNING_KEY_ID)
    python build.py ... --index <index.json> [--base-url https://.../]
                                                       also write a gallery index listing the package

Uses kext.py (tools/kext.py in this repository, or ../tools/kext.py inside the KAREOKIE repository).
Works on Windows and macOS. Files the Finder leaves behind (.DS_Store, ._*) are never packed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# not part of the package; kext.pack() skips most of these itself, the Finder's files it doesn't know
NOT_PACKED = shutil.ignore_patterns(".*", "dist", "_deps", "__pycache__", "keys", "tests", "*.pyc")


def _kext():
    for path in (os.path.join(HERE, "tools", "kext.py"), os.path.join(HERE, "..", "tools", "kext.py")):
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("kext", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
    raise SystemExit("kext.py not found (expected tools/kext.py)")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--key", default=os.environ.get("KAREOKIE_SIGNING_KEY"))
    p.add_argument("--key-id", default=os.environ.get("KAREOKIE_SIGNING_KEY_ID"))
    p.add_argument("--out", default=os.path.join(HERE, "dist"))
    p.add_argument("--index", default=None)
    p.add_argument("--base-url", default="")
    p.add_argument("--unsigned", action="store_true", help="don't sign, even with a key in the environment")
    a = p.parse_args()
    kext = _kext()
    m = kext.read_manifest(HERE)
    with tempfile.TemporaryDirectory() as tmp:
        folder = os.path.join(tmp, "youtube")
        shutil.copytree(HERE, folder, ignore=NOT_PACKED)
        payload = kext.pack(folder)
    signed = bool(a.key and a.key_id) and not a.unsigned
    if signed:
        with open(a.key, "rb") as f:
            data = kext.sign(payload, f.read(), a.key_id)
    else:
        print("no signing key given: building an UNSIGNED package (users will see a warning)")
        data = payload
    out = os.path.join(a.out, kext.package_name(m, signed))
    kext._write(out, data)
    if a.index:
        index_dir = os.path.dirname(os.path.abspath(a.index))
        url = a.base_url + os.path.basename(out) if a.base_url else os.path.relpath(out, index_dir).replace("\\", "/")
        entry = kext.index_entry(out, url, official=signed)
        doc = {"version": 1, "name": "", "extensions": []}
        if os.path.isfile(a.index):
            with open(a.index, "r", encoding="utf-8") as f:
                doc = json.load(f)
        doc["extensions"] = [e for e in doc.get("extensions", []) if e.get("id") != m["id"]] + [entry]
        kext._write(a.index, (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
