"""
static/css/tailwind.css is prebuilt from the classes in templates/ and app.js.
A class added without rebuilding would silently render unstyled, so the
committed file must equal a fresh build.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "node_modules" / ".bin" / "tailwindcss"
CSS = ROOT / "static" / "css" / "tailwind.css"


def test_halaman_memakai_css_hasil_build_bukan_runtime():
    for name in ("index.html", "login.html"):
        html = (ROOT / "templates" / name).read_text()
        assert "/static/css/tailwind.css" in html
        assert "tailwind.js" not in html and "tailwind.config" not in html
    assert not (ROOT / "static" / "vendor" / "tailwind.js").exists()


@pytest.mark.skipif(not CLI.exists() or not shutil.which("node"),
                    reason="Tailwind CLI belum terpasang: jalankan `npm install` untuk memeriksa tailwind.css")
def test_tailwind_css_sesuai_template(tmp_dir):
    fresh = tmp_dir / "tailwind.css"
    subprocess.run(
        [str(CLI), "-c", "tailwind.config.js", "-i", "static/css/tailwind.input.css", "-o", str(fresh), "--minify"],
        cwd=ROOT, check=True, capture_output=True, timeout=120,
    )
    assert fresh.read_bytes() == CSS.read_bytes(), (
        "static/css/tailwind.css sudah usang: jalankan `npm run build:css` lalu commit hasilnya."
    )
