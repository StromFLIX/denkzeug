"""Prepare the pinned GridCraft artifact with the production installer, not a stub."""

import argparse
import importlib.util
import json
from pathlib import Path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="empty fixture directory")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    path = root / "images/pi-base/artcraft/install.py"
    module = importlib.util.spec_from_file_location("gridcraft_fixture_installer", path)
    assert module is not None and module.loader is not None
    installer = importlib.util.module_from_spec(module)
    module.loader.exec_module(installer)
    arch = installer.architecture()
    release = installer.CATALOG["gridcraft"]
    spec = {
        "app": "gridcraft",
        "version": release["version"],
        "arch": arch,
        "sha256": release["sha256"][arch],
    }
    print(json.dumps(installer.install(spec, args.destination)))
