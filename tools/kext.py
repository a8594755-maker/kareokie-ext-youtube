"""
KAREOKIE extension packaging tool (standard library + `cryptography` for signing).

    python kext.py keygen --key-id kareokie-2026 [--out keys/]      new Ed25519 key pair (prints the public key)
    python kext.py pack <extension folder> [--out dist/]            unsigned package (any developer)
    python kext.py sign <extension folder> --key <private.pem> --key-id <id> [--out dist/]
                                                                    signed package (*.kext)
    python kext.py verify <package> --public <base64url>            check a signed package
    python kext.py index <package> [<package> ...] --out index.json [--base-url https://.../]
                                                                    a gallery index for packages

Packages are deterministic (sorted files, fixed timestamps, LF line endings), so the same source gives the same bytes.
The private key never goes into a repository: keep it offline (extensions/keys/ is gitignored).
See docs/extensions.md in the KAREOKIE repository.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import zipfile
from typing import Any, Dict, List, Optional, Tuple

SIG_FORMAT = "kareokie-ext-sig-1"
SKIP_DIRS = {"dist", "tests", "__pycache__", ".git", ".github", "_deps", ".pytest_cache", "keys", "node_modules"}
SKIP_FILES = {"build.py", ".gitignore", "_install.json", "index.json"}
FIXED_TIME = (2020, 1, 1, 0, 0, 0)
TEXT_EXTS = (".py", ".json", ".md", ".txt", ".cfg", ".toml", ".yml", ".yaml")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def read_manifest(folder: str) -> Dict[str, Any]:
    with open(os.path.join(folder, "manifest.json"), "r", encoding="utf-8-sig") as f:
        return json.load(f)


def pack(folder: str) -> bytes:
    """The extension folder as a deterministic zip (manifest.json at its root)."""
    files: List[Tuple[str, str]] = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            if name in SKIP_FILES or name.endswith((".pyc", ".kext", ".zip")):
                continue
            full = os.path.join(root, name)
            files.append((os.path.relpath(full, folder).replace("\\", "/"), full))
    if "manifest.json" not in [r for r, _ in files]:
        raise SystemExit(f"{folder}: manifest.json is missing")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, full in sorted(files):
            info = zipfile.ZipInfo(rel, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(full, "rb") as f:
                data = f.read()
            if rel.endswith(TEXT_EXTS):
                data = data.replace(b"\r\n", b"\n")  # same bytes whether git checked the file out with CRLF or LF
            zf.writestr(info, data)
    return buf.getvalue()


def sign(payload: bytes, private_pem: bytes, key_id: str) -> bytes:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_pem, password=None)
    sig = {
        "format": SIG_FORMAT,
        "keyId": key_id,
        "algorithm": "Ed25519",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "signature": _b64url(key.sign(payload)),  # type: ignore[union-attr]
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, data in (("extension.zip", payload), ("signature.json", json.dumps(sig, indent=1).encode("utf-8"))):
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return buf.getvalue()


def verify(package: bytes, public_b64: str) -> Dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        payload = zf.read("extension.zip")
        sig = json.loads(zf.read("signature.json"))
    Ed25519PublicKey.from_public_bytes(_b64url_decode(public_b64)).verify(_b64url_decode(sig["signature"]), payload)
    return sig


def manifest_of(package: bytes) -> Dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        names = set(zf.namelist())
        if names == {"extension.zip", "signature.json"}:
            with zipfile.ZipFile(io.BytesIO(zf.read("extension.zip"))) as inner:
                return json.loads(inner.read("manifest.json"))
        return json.loads(zf.read("manifest.json"))


def index_entry(package_path: str, download_url: str, official: bool = False) -> Dict[str, Any]:
    with open(package_path, "rb") as f:
        data = f.read()
    m = manifest_of(data)
    keys = ("id", "name", "version", "description", "author", "homepage", "license", "minAppVersion", "capabilities", "i18n")
    return {**{k: m[k] for k in keys if k in m}, "official": official, "downloadUrl": download_url,
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def package_name(m: Dict[str, Any], signed: bool) -> str:
    return f"kareokie-ext-{m['id']}-{m['version']}.{'kext' if signed else 'zip'}"


def _write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    print(f"wrote {path} ({len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()})")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--key-id", required=True)
    k.add_argument("--out", default="keys")
    pk = sub.add_parser("pack")
    pk.add_argument("folder")
    pk.add_argument("--out", default=None)
    s = sub.add_parser("sign")
    s.add_argument("folder")
    s.add_argument("--key", default=os.environ.get("KAREOKIE_SIGNING_KEY"), help="private key PEM (or KAREOKIE_SIGNING_KEY)")
    s.add_argument("--key-id", default=os.environ.get("KAREOKIE_SIGNING_KEY_ID"))
    s.add_argument("--out", default=None)
    v = sub.add_parser("verify")
    v.add_argument("package")
    v.add_argument("--public", required=True)
    ix = sub.add_parser("index")
    ix.add_argument("packages", nargs="+")
    ix.add_argument("--out", required=True)
    ix.add_argument("--base-url", default="", help="prefix of the download URLs (default: paths relative to the index)")
    ix.add_argument("--official", action="store_true")
    ix.add_argument("--name", default="")
    a = p.parse_args(argv)

    if a.cmd == "keygen":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        path = os.path.join(a.out, f"{a.key_id}.private.pem")
        if os.path.exists(path):
            raise SystemExit(f"{path} exists; refusing to overwrite a key")
        _write(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        print(f"public key ({a.key_id}): {_b64url(pub)}")
        print("Add it to backend/app/sources/signing.py (OFFICIAL_KEYS) and keep the private key offline.")
        return 0
    if a.cmd in ("pack", "sign"):
        m = read_manifest(a.folder)
        payload = pack(a.folder)
        out_dir = a.out or os.path.join(a.folder, "dist")
        if a.cmd == "pack":
            _write(os.path.join(out_dir, package_name(m, False)), payload)
            return 0
        if not a.key or not a.key_id:
            raise SystemExit("sign needs --key <private.pem> and --key-id <id> (or KAREOKIE_SIGNING_KEY / KAREOKIE_SIGNING_KEY_ID)")
        with open(a.key, "rb") as f:
            _write(os.path.join(out_dir, package_name(m, True)), sign(payload, f.read(), a.key_id))
        return 0
    if a.cmd == "verify":
        with open(a.package, "rb") as f:
            sig = verify(f.read(), a.public)
        print(f"OK: signed by {sig['keyId']}")
        return 0
    if a.cmd == "index":
        out_dir = os.path.dirname(os.path.abspath(a.out))
        entries = []
        for pkg in a.packages:
            url = a.base_url + os.path.basename(pkg) if a.base_url else os.path.relpath(os.path.abspath(pkg), out_dir).replace("\\", "/")
            entries.append(index_entry(pkg, url, a.official))
        doc = {"version": 1, "name": a.name, "extensions": entries}
        _write(a.out, (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
