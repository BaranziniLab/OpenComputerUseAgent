"""L0 checks that need no build: manifest rules, version lockstep, licences, brand spelling."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_passes_the_strict_desktop_rules():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    for key in ("name", "display_name", "description", "version", "entry_point", "repository"):
        assert isinstance(manifest.get(key), str) and manifest[key].strip(), key
    assert isinstance(manifest["env_vars"], list)
    assert manifest["name"] == "opencomputeruseagent" and manifest["name"] != "computercontroller"
    assert manifest["tools_count"] == 12
    assert (ROOT / manifest["entry_point"]).is_file()
    assert "copilot" not in manifest["display_name"].lower()


def test_versions_agree():
    assert load("version").check() == []


def test_licences_and_notices_are_present():
    assert "Apache License" in (ROOT / "LICENSE").read_text()
    notice = (ROOT / "NOTICE").read_text()
    assert "Open Computer Use 1.1.5" in notice and "Cua Driver" in notice and "yabai" in notice
    assert "MIT License" in (ROOT / "vendor/open-computer-use/LICENSE").read_text()
    vendor_notice = (ROOT / "vendor/open-computer-use/NOTICE").read_text()
    assert "Cua AI" in vendor_notice and "Vikane" in vendor_notice


def test_brand_spelling():
    assert load("check_brand").check_repo() == []


def test_pin_records_the_current_upstream_repository():
    pin = json.loads((ROOT / "vendor/open-computer-use/pin.json").read_text())
    assert pin["repository"] == "https://github.com/iFurySt/open-computer-use.git"
    assert pin["bundle_identifier"] == "edu.ucsf.baranzinilab.opencomputeruseagent"
    for prefix in ("org.biorouter.computer-use", "edu.ucsf.biorouter.", "com.electron.biorouter."):
        assert not pin["bundle_identifier"].startswith(prefix)
    assert not pin["app_name"].endswith("Copilot")
