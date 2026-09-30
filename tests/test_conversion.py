from pathlib import Path
import json
import os

from pypdf import PdfReader
import pytest

from figma_svg_to_pdf.cli import main
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


@pytest.mark.parametrize("width,height,scale", [
    (50000, 92000, .1),
    (92000, 50000, .1),
    (20000, 32000, .25),
    (1200, 800, 1),
])
def test_automatic_scale_preserves_complete_page_and_links(tmp_path, font_cache, width, height, scale):
    source = write_svg(tmp_path, f'<rect width="{width}" height="{height}" fill="white"/>'
                       f'<text font-family="Inter" font-size="12" x="40" y="{height-40}">'
                       '<a href="https://example.com/end">End</a></text>',
                       f'viewBox="0 0 {width} {height}"')
    report = convert(source, tmp_path/"out.pdf", Options(font_cache=font_cache), log=lambda _: None)
    assert report["scale"] == scale
    assert report["scale_mode"] == "auto"
    assert report["max_page_size"] == 10000
    assert report["page_points"] == pytest.approx([0, 0, (width+4)*scale, (height+4)*scale])
    assert max(report["page_points"]) <= 10000
    page = PdfReader(tmp_path/"out.pdf").pages[0]
    assert page.extract_text().strip() == "End"
    assert report["source_links"] == report["pdf_links"] == 1
    rect = page["/Annots"][0].get_object()["/Rect"]
    assert 0 <= rect[0] < rect[2] <= page.mediabox.width
    assert 0 <= rect[1] < rect[3] <= page.mediabox.height


@pytest.mark.parametrize("flags,scale,mode,maximum", [
    ([], .25, "auto", 10000),
    (["--scale", "auto"], .25, "auto", 10000),
    (["--max-page-size", "5000"], 1/7, "auto", 5000),
    (["--scale", "0.1"], .1, "manual", 10000),
    (["--scale", "0.1", "--max-page-size", "1000"], .1, "manual", 1000),
])
def test_cli_reports_scale_and_uses_viewbox_units(tmp_path, font_cache, capsys, flags, scale, mode, maximum):
    source = write_svg(tmp_path, '<text font-family="Inter" x="10" y="30">Scale</text>',
                       'width="200" height="320" viewBox="-100 -200 20000 32000"')
    output = tmp_path/"out.pdf"
    assert main([str(source), "-o", str(output), "--font-cache", str(font_cache),
                 "--padding", "0", *flags]) == 0
    report = json.loads(output.with_suffix(".audit.json").read_text())
    assert report["status"] == "passed"
    assert report["scale"] == scale
    assert report["scale_mode"] == mode
    assert report["max_page_size"] == maximum
    assert report["source_viewbox"] == report["output_viewbox"] == [-100, -200, 20000, 32000]
    assert report["page_points"] == pytest.approx([0, 0, 20000*scale, 32000*scale])
    message = capsys.readouterr().out
    assert "SVG viewport: 20000 × 32000 units" in message
    assert "complete bounds with padding: 20000 × 32000 units" in message
    assert f"Scale: {scale:g} ({mode}" in message
    assert "Page:" in message


def test_automatic_scale_includes_content_outside_original_frame(tmp_path, font_cache):
    source = write_svg(tmp_path, '<g transform="translate(-100 50000)">'
                       '<rect width="400" height="1000"/></g>')
    report = convert(source, tmp_path/"out.pdf", Options(font_cache=font_cache), log=lambda _: None)
    assert report["source_viewbox"] == [0, 0, 100, 100]
    assert report["output_viewbox"] == [-102, -2, 404, 51004]
    assert report["scale"] == 1/6
    assert report["page_points"] == pytest.approx([0, 0, 404/6, 51004/6])


@pytest.mark.parametrize("padding,scale", [(0, 1), (2, .5)])
def test_automatic_scale_accounts_for_padding_at_page_limit(tmp_path, font_cache, padding, scale):
    source = write_svg(tmp_path, '<rect width="100" height="10000"/>',
                       'width="100" height="10000"')
    report = convert(source, tmp_path/"out.pdf", Options(font_cache=font_cache, padding=padding),
                     log=lambda _: None)
    assert report["scale"] == scale
    assert max(report["page_points"]) <= 10000


@pytest.mark.parametrize("value", ["not-a-scale", "nan", "inf", "0", "-1"])
def test_cli_invalid_scale_has_actionable_error(tmp_path, capsys, value):
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path/"source.svg"), "--scale", value])
    assert exc.value.code == 2
    assert "scale must be 'auto' or" in capsys.readouterr().err


@pytest.mark.parametrize("scale,error", [(1, "automatic sizing"), (.0000001, "increase the scale")])
def test_manual_scale_rejects_incompatible_page_size(tmp_path, font_cache, scale, error):
    source = write_svg(tmp_path, "", 'viewBox="0 0 20000 32000"')
    output = tmp_path/"out.pdf"
    with pytest.raises(ConversionError, match=error):
        convert(source, output, Options(font_cache=font_cache, scale=scale), log=lambda _: None)
    assert not output.exists()


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
