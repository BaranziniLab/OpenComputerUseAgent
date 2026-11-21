"""The wrapper never calls a TCC protected API: its responsible process is Biorouter, so any such
call would be checked against Biorouter's grants. Only the helper app does that work."""
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src/opencomputeruseagent"
FORBIDDEN = re.compile(r"ApplicationServices|ScreenCaptureKit|CoreGraphics|AXUIElement|AXIsProcessTrusted|"
                       r"CGWindowListCreateImage|CGDisplayCreateImage|CGPreflightScreenCaptureAccess|"
                       r"CGRequestScreenCaptureAccess|Quartz|AppKit|pyobjc|objc\.")


def test_no_tcc_protected_calls_in_the_wrapper():
    for path in sorted(SOURCE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert not FORBIDDEN.search(text), f"{path.name} names a TCC protected API"


def test_the_only_native_macos_call_is_secure_input():
    loads = []
    for path in sorted(SOURCE.rglob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "CDLL(" in line or "cdll." in line:
                loads.append((path.name, line.strip()))
    assert loads == [("guard.py", 'carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")')]
    guard = (SOURCE / "guard.py").read_text(encoding="utf-8")
    assert "carbon.IsSecureEventInputEnabled" in guard
    assert re.findall(r"carbon\.(\w+)", guard) == ["IsSecureEventInputEnabled"]


def test_the_proxy_disable_flag_is_never_set_by_the_wrapper():
    for path in sorted(SOURCE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "DISABLE_APP_AGENT_PROXY\"] =" not in text and "DISABLE_APP_AGENT_PROXY'] =" not in text
