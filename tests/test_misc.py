"""CLI, translations and packaging checks — stdlib only."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from .fake_config import build

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "ha_audit_cli.py"
COMP = ROOT / "custom_components" / "ha_audit"


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, cwd=cwd, check=False)


def test_cli_writes_reports_outside_www(tmp_path):
    build(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    p = run_cli("--config", str(tmp_path), "--no-db", "--md", str(out / "r.md"), "--json", str(out / "r.json"),
                "--state-file", str(out / "s.json"))
    assert p.returncode == 0, p.stderr
    assert "HA Audit Report" in (out / "r.md").read_text()
    assert json.loads((out / "r.json").read_text())["summary"]["total"] > 0
    assert "new=0" in run_cli("--config", str(tmp_path), "--no-db", "--state-file", str(out / "s.json")).stderr


@pytest.mark.parametrize("flag", ["--md", "--json", "--state-file"])
def test_cli_refuses_to_write_into_www(tmp_path, flag):
    build(tmp_path)
    p = run_cli("--config", str(tmp_path), "--no-db", flag, str(tmp_path / "www" / "leak.txt"))
    assert p.returncode == 2 and "publicly readable" in p.stderr
    assert not (tmp_path / "www" / "leak.txt").exists()


def test_cli_refuses_www_via_relative_paths(tmp_path):
    build(tmp_path)
    p = run_cli("--config", str(tmp_path), "--no-db", "--md", "www/leak.md", cwd=tmp_path)
    assert p.returncode == 2


def test_cli_stdout_works_when_cwd_is_inside_www(tmp_path):
    """Regression: printing to stdout was wrongly refused when the cwd happened to be <config>/www."""
    build(tmp_path)
    p = run_cli("--config", str(tmp_path), "--no-db", cwd=tmp_path / "www")
    assert p.returncode == 0 and "HA Audit Report" in p.stdout


def _keys(d, prefix=""):
    out = set()
    for k, v in d.items():
        out.add(prefix + k)
        if isinstance(v, dict):
            out |= _keys(v, prefix + k + ".")
    return out


@pytest.mark.parametrize("lang", ["en", "el"])
def test_translations_match_strings(lang):
    base = json.loads((COMP / "strings.json").read_text(encoding="utf-8"))
    tr = json.loads((COMP / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
    assert _keys(base) == _keys(tr)


def test_brand_assets_present_and_sized():
    from struct import unpack
    brand = COMP / "brand"
    sizes = {}
    for f in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png", "dark_logo.png", "dark_logo@2x.png"):
        data = (brand / f).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", f
        sizes[f] = unpack(">II", data[16:24])
    assert sizes["icon.png"] == (256, 256) and sizes["icon@2x.png"] == (512, 512)
    assert min(sizes["logo.png"]) == 256 and min(sizes["logo@2x.png"]) == 512


def test_manifest_has_required_keys():
    m = json.loads((COMP / "manifest.json").read_text())
    for k in ("domain", "documentation", "issue_tracker", "codeowners", "name", "version"):
        assert m.get(k), k
    assert "YOUR_GITHUB_USER" not in json.dumps(m)


def test_readme_images_exist_and_point_to_this_repository():
    """HACS renders the README outside GitHub, so images must be absolute raw URLs to files that exist."""
    import re
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    manifest = json.loads((COMP / "manifest.json").read_text())
    owner_repo = re.search(r"github\.com/([^/]+/[^/]+?)/?$", manifest["documentation"]).group(1)

    urls = re.findall(r"https://raw\.githubusercontent\.com/([^/]+/[^/]+)/([^/]+)/([^\s)\"']+)", readme)
    assert len(urls) >= 5, "expected icon + screenshots in README"
    for repo, branch, path in urls:
        assert repo == owner_repo, f"{repo} != {owner_repo}"
        assert branch == "main"
        assert (ROOT / path).is_file(), f"README references a missing file: {path}"
    assert not re.search(r"!\[[^\]]*\]\((?!https://)", readme), "relative image paths break inside HACS"


def test_no_changelog_and_gitignore_covers_generated_files():
    assert not (ROOT / "CHANGELOG.md").exists()
    ignore = (ROOT / ".gitignore").read_text()
    for pattern in ("__pycache__", ".pytest_cache", "ha_audit_report.*"):
        assert pattern in ignore
