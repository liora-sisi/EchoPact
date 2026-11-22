"""Explicit download of one pinned public model; never reads an Echo database."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from backend.memory.local_embedding_config import MODEL, REVISION, FILES, QUERY_PREFIX


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    target = args.destination.resolve()
    if (target / "echo-model-manifest.json").exists():
        parser.error("manifest already exists; reuse it or choose a new directory")
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HOME"] = str(target / ".cache" / "hf")
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    from huggingface_hub import snapshot_download
    snapshot_download(MODEL, revision=REVISION, allow_patterns=FILES,
                      local_dir=target, token=False, max_workers=2)
    manifest = {"model": MODEL, "revision": REVISION, "dimensions": 512,
                "query_prefix": QUERY_PREFIX, "normalize_embeddings": True,
                "files": {name: digest(target / name) for name in FILES}}
    with (target / "echo-model-manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": "downloaded", "model": MODEL, "revision": REVISION,
                      "bytes": sum((target / n).stat().st_size for n in FILES)}))


if __name__ == "__main__":
    main()
