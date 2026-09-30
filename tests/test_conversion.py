from pathlib import Path
import os

from pypdf import PdfReader
import pytest

from figma_svg_to_pdf.convert import Options, convert
from figma_svg_to_pdf.fonts import default_cache
from figma_svg_to_pdf.model import Box, ConversionError, point, transform
from figma_svg_to_pdf.svg import load_svg

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def font_cache():
    cache = Path(os.environ.get("FIGMA_PDF_TEST_FONTS", default_cache()))
    if not (cache/"Inter-Medium.ttf").exists():
        pytest.fail("Install test fonts first: figma-svg-to-pdf fonts")
    return cache


def write_svg(tmp_path, content, attrs='width="100" height="100"'):
    path = tmp_path / "source.svg"
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}>{content}</svg>')
    return path


def test_demo_preserves_text_links_and_shared_shadows(tmp_path, font_cache):
    output = tmp_path / "demo.pdf"
    report = convert(ROOT/"examples/demo.svg", output,
                     Options(font_cache=font_cache, color_emoji=False), log=lambda _: None)
    assert report["status"] == "passed"
    assert report["text_inventory_match"]
    assert report["source_links"] == report["pdf_links"] == 2
    assert report["link_targets_match"]
    assert report["optimized_cards"] == 2
    assert report["shadow_tiles"] == 1
    assert report["unoptimized_filters"] == 0
    assert report["output_viewbox"][3] > 810  # original viewport was only 720 high
    pdf = PdfReader(output)
    assert len(pdf.pages) == 1
    assert "日本語サンプル" in pdf.pages[0].extract_text().replace("\n", "")
    assert pdf.trailer["/Root"]["/OpenAction"][1] == "/Fit"
    assert output.read_bytes().startswith(b"%PDF-1.7")
    used_fonts = set()
    def collect_fonts(text, cm, tm, font, size):
        if text.strip() and font:
            used_fonts.add(str(font.get("/BaseFont")))
    pdf.pages[0].extract_text(visitor_text=collect_fonts)
    assert all("FigmaPDF" in name for name in used_fonts), used_fonts


def test_transformed_content_outside_viewport_is_included(tmp_path, font_cache):
    source = write_svg(tmp_path, '<g transform="translate(-40 180) rotate(15)">'
                       '<rect width="60" height="40" fill="red"/></g>')
    report = convert(source, tmp_path/"out.pdf", Options(font_cache=font_cache), log=lambda _: None)
    x, y, w, h = report["output_viewbox"]
    assert x < -40 and y == -2 and x+w >= 100 and y+h > 230


def test_large_page_is_scaled_to_compatible_dimensions(tmp_path, font_cache):
    source = write_svg(tmp_path, '<rect width="50000" height="92000" fill="white"/>',
                       'viewBox="0 0 50000 92000"')
    report = convert(source, tmp_path/"out.pdf", Options(font_cache=font_cache), log=lambda _: None)
    assert report["scale"] == .1
    assert max(report["page_points"]) < 10000


def test_missing_text_prevents_publishing(tmp_path, font_cache):
    source = write_svg(tmp_path, '<text font-family="Inter" font-size="12" display="none" x="1" y="20">Hidden text</text>')
    output = tmp_path/"out.pdf"
    with pytest.raises(ConversionError, match="inventory differs"):
        convert(source, output, Options(font_cache=font_cache), log=lambda _: None)
    assert not output.exists()
    assert output.with_suffix(".audit.json").exists()


def test_existing_output_is_not_replaced(tmp_path, font_cache):
    source = write_svg(tmp_path, '<rect width="50" height="50"/>')
    output = tmp_path/"out.pdf"
    output.write_bytes(b"original")
    with pytest.raises(ConversionError, match="already exists"):
        convert(source, output, Options(font_cache=font_cache))
    assert output.read_bytes() == b"original"


def test_formatted_svg_with_blank_text_nodes(tmp_path, font_cache):
    source = write_svg(tmp_path, '<text font-family="Inter" font-size="12">\n'
                       '<tspan x="10" y="30">ABC 日本語</tspan>\n</text>')
    report = convert(source, tmp_path/"out.pdf", Options(font_cache=font_cache), log=lambda _: None)
    assert report["text_inventory_match"]


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_scale_is_rejected(tmp_path, value):
    source = write_svg(tmp_path, "")
    with pytest.raises(ConversionError, match="scale"):
        convert(source, tmp_path/"out.pdf", Options(scale=value))


@pytest.mark.parametrize("body", [
    '<image href="https://example.com/private.png"/>',
    '<image href="file:///tmp/private.png"/>',
    '<style>@import "https://example.com/fonts.css";</style>',
    '<script>alert(1)</script>',
    '<rect fill="url(https://example.com/paint.svg)"/>',
    '<text onclick="alert(1)">x</text>',
    '<use href="#unknown"/>',
])
def test_unsupported_or_external_resources_are_not_loaded(body):
    data = f'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">{body}</svg>'
    with pytest.raises(ConversionError):
        load_svg(data.encode())


def test_xml_entities_are_rejected():
    with pytest.raises(ConversionError, match="parse"):
        load_svg(b'<!DOCTYPE svg [<!ENTITY x "data">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>')


def test_transform_order_and_stroke_bounds():
    matrix = transform("translate(10,20) scale(2) rotate(90)")
    assert point(matrix, 3, 4) == pytest.approx((2, 26))
    box = Box(0, 0, 10, 20).transformed(transform("translate(-10,2)"))
    assert box == Box(-10, 2, 0, 22)


@pytest.mark.skipif(not Path('/System/Library/Fonts/Apple Color Emoji.ttc').exists(), reason="macOS color font only")
def test_macos_link_emoji_retains_searchable_text(tmp_path, font_cache):
    report = convert(ROOT/"examples/demo.svg", tmp_path/"color.pdf",
                     Options(font_cache=font_cache), log=lambda _: None)
    assert report["color_link_icons"] == 2
    assert report["text_inventory_match"]
