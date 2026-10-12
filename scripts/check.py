#!/usr/bin/env python3
"""One offline verification entrypoint, no dependency installation or network."""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def package_checks():
    for p in list((ROOT / "scripts").glob("*.py")) + list((ROOT / "tests").glob("*.py")):
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    for p in ROOT.rglob("*.md"):
        if any(s in p.parts for s in (".git", "runs", ".venv")):
            continue
        for target in re.findall(r"\]\(([^)]+)\)", p.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("#"):
                continue
            file = target.split("#")[0]
            if file and not (p.parent / file).exists():
                raise AssertionError(f"Broken relative link: {p.name}: {file}")
    manifest = json.loads((ROOT / "examples" / "checksums.json").read_text())
    for relative, expected in manifest.items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise AssertionError("Frozen evidence hash changed: " + relative)
    entry = (ROOT / "SKILL.md").read_text()
    if not entry.startswith("---\nname: wechat-breakout-finder\ndescription:") or "[TODO" in entry:
        raise AssertionError("Invalid or unfinished skill entrypoint")


if __name__ == "__main__":
    try:
        package_checks()
    except (AssertionError, SyntaxError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    with patch("socket.socket", side_effect=AssertionError("offline checks forbid sockets")), \
            patch("subprocess.run", side_effect=AssertionError("offline checks forbid external processes")):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(json.dumps({"tests_run": result.testsRun, "failures": len(result.failures),
                      "errors": len(result.errors), "package_checks": "passed", "network_requests": 0}))
    raise SystemExit(0 if result.wasSuccessful() else 1)
