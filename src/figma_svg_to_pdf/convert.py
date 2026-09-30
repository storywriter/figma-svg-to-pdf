"""Conversion, link restoration, and verification before publishing an output."""

from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import json
import math
from pathlib import Path
import tempfile
import time
import xml.etree.ElementTree as ET

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import ArrayObject, NameObject
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
import vl_convert as vlc

from . import __version__
from .fonts import FontBook, default_cache
from .model import ConversionError, XLINK, multiply, tag
from .shadows import optimize_shadows
from .svg import document_geometry, load_svg, normalize_fonts, text_inventory, viewport


@dataclass
class Options:
    font_cache: Path = field(default_factory=default_cache)
    font_dirs: tuple = ()
    scale: float | None = None
    max_page_size: float = 10000
    padding: float = 2
    color_emoji: bool = True
    keep_filters: bool = False
    shadow_density: float = 2
    allow_text_differences: bool = False
    overwrite: bool = False


def _color_overlay(runs, page_size, page_box, scale):
    color_runs = [run for run in runs if run.face.family == "Apple Color Emoji"]
    if not color_runs:
        return None, 0
    stream = BytesIO()
    drawing = canvas.Canvas(stream, pagesize=page_size, pageCompression=1)
    images = {}
    count = 0
    # SVG user coordinates point down, while PDF coordinates point up.
    to_pdf = (scale, 0, 0, -scale, -page_box.x0*scale, page_box.y1*scale)
    for run in color_runs:
        offset = 0
        for char in run.text:
            if char not in images:
                tile = ET.Element(tag("svg"), {"width": "512", "height": "384",
                                              "viewBox": "-64 -128 256 192"})
                t = ET.SubElement(tile, tag("text"), {"x": "0", "y": "0",
                    "font-family": "Apple Color Emoji", "font-size": "64"})
                t.text = char
                images[char] = ImageReader(BytesIO(vlc.svg_to_png(ET.tostring(tile, encoding="unicode"))))
            # Make the tile upright after entering the downward-pointing SVG space.
            drawing.saveState()
            drawing.transform(*multiply(to_pdf, run.matrix))
            drawing.translate(run.x+offset-run.size, run.y+run.size)
            drawing.scale(1, -1)
            drawing.setFillAlpha(run.alpha)
            drawing.drawImage(images[char], 0, 0, width=4*run.size, height=3*run.size, mask="auto")
            drawing.restoreState()
            offset += run.face.advance(char)*run.size + run.spacing
            count += 1
    drawing.showPage()
    drawing.save()
    stream.seek(0)
    return PdfReader(stream).pages[0], count


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def convert(source, destination, options=None, audit_path=None, log=print):
    options = options or Options()
    source, destination = Path(source), Path(destination)
    audit_path = Path(audit_path) if audit_path else destination.with_suffix(".audit.json")
    for value, label in [(options.max_page_size, "max page size"),
                         (options.shadow_density, "shadow density")]:
        if not math.isfinite(value) or value <= 0:
            raise ConversionError(f"{label} must be finite and positive")
    if options.max_page_size > 14400:
        raise ConversionError("max page size must not exceed 14400 PDF points")
    if not math.isfinite(options.padding) or options.padding < 0:
        raise ConversionError("padding must be finite and nonnegative")
    if options.scale is not None and (not math.isfinite(options.scale) or options.scale <= 0):
        raise ConversionError("scale must be finite and positive")
    paths = [source.resolve(), destination.resolve(), audit_path.resolve()]
    if len(set(paths)) != 3:
        raise ConversionError("Input, output and audit paths must be different")
    if not options.overwrite and (destination.exists() or audit_path.exists()):
        raise ConversionError("Output or audit already exists; choose another name or use --overwrite")
    if destination.suffix.lower() != ".pdf":
        raise ConversionError("Output filename must end in .pdf")
    started = time.monotonic()
    data = source.read_bytes()
    root = load_svg(data)
    expected = text_inventory(root)
    source_viewport = viewport(root)
    fonts = FontBook(options.font_cache, options.font_dirs, options.color_emoji)
    normalize_fonts(root, fonts)
    if text_inventory(root) != expected:
        raise ConversionError("Internal error: font normalization changed the source text")
    bounds, runs = document_geometry(root, fonts)
    page_box = source_viewport.union(bounds).pad(options.padding)
    if options.scale is None:
        # Use the complete padded bounds, not just the original Figma frame.
        # Choose the largest 1/N that fits the target, without enlarging small SVGs.
        divisor = max(1, math.ceil(max(page_box.width, page_box.height)/options.max_page_size))
        scale = 1/divisor
        scale_mode = "auto"
        scale_detail = f"auto, 1/{divisor}; maximum side {options.max_page_size:g} pt"
    else:
        scale = options.scale
        scale_mode = "manual"
        scale_detail = "manual override"
    page_size = (page_box.width*scale, page_box.height*scale)
    if max(page_size) > 14400:
        raise ConversionError("PDF dimensions exceed 14400 points; omit --scale for automatic sizing "
                              "or choose a smaller scale")
    if min(page_size) < 1:
        raise ConversionError("A PDF side would be smaller than 1 point; increase the scale or padding "
                              "while keeping both sides at most 14400 points")
    root.set("viewBox", " ".join(f"{v:.10g}" for v in page_box.viewbox()))
    root.set("width", f"{page_size[0]:.10g}")
    root.set("height", f"{page_size[1]:.10g}")
    root.set("preserveAspectRatio", "xMinYMin meet")
    anchors = list(root.iter(tag("a")))
    link_boxes = {}
    for run in runs:
        if run.link is not None:
            box = run.box.transformed(run.matrix)
            link_boxes[run.link] = link_boxes[run.link].union(box) if run.link in link_boxes else box
    if set(anchors) != set(link_boxes):
        raise ConversionError("Each hyperlink must contain visible, positioned text")
    log(f"SVG viewport: {source_viewport.width:g} × {source_viewport.height:g} units; "
        f"complete bounds with padding: {page_box.width:g} × {page_box.height:g} units")
    log(f"Scale: {scale:g} ({scale_detail})")
    log(f"Page: {page_size[0]:.2f} × {page_size[1]:.2f} pt; "
        f"text {sum(expected.values()):,} characters; links {len(anchors):,}")
    stats = {"optimized_cards": 0, "shadow_tiles": 0,
             "unoptimized_filters": sum(bool(e.get("filter")) for e in root.iter())}
    if not options.keep_filters:
        stats = optimize_shadows(root, fonts, options.shadow_density)
    log(f"Shadows: {stats['optimized_cards']:,} cards, {stats['shadow_tiles']} shared tiles; "
        f"{stats['unoptimized_filters']} filters left to the renderer")
    if text_inventory(root) != expected:
        raise ConversionError("Internal error: shadow optimization changed the source text")
    log("Rendering vector PDF (large exports can take several minutes) …")
    pdf = vlc.svg_to_pdf(ET.tostring(root, encoding="unicode"))
    reader = PdfReader(BytesIO(pdf))
    if len(reader.pages) != 1:
        raise ConversionError("Renderer did not produce exactly one page")
    actual_size = (float(reader.pages[0].mediabox.width), float(reader.pages[0].mediabox.height))
    if any(abs(a-b) > .01 for a, b in zip(page_size, actual_size)):
        raise ConversionError("Renderer changed the requested page dimensions")
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.pdf_header = "%PDF-1.7"
    writer.add_metadata({"/Title": source.stem,
                         "/Creator": f"figma-svg-to-pdf {__version__}",
                         "/Subject": "Complete SVG on one page; local conversion"})
    writer.page_layout = "/SinglePage"
    for anchor, box in link_boxes.items():
        uri = anchor.get("href") or anchor.get(f"{{{XLINK}}}href")
        rect = ((box.x0-page_box.x0)*scale, (page_box.y1-box.y1)*scale,
                (box.x1-page_box.x0)*scale, (page_box.y1-box.y0)*scale)
        writer.add_annotation(0, Link(rect=rect, url=uri, border=[0, 0, 0]))
    overlay, icons = _color_overlay(runs, page_size, page_box, scale)
    if overlay is not None:
        writer.pages[0].merge_page(overlay, over=True)
    writer.pages[0].compress_content_streams(level=9)
    writer._root_object[NameObject("/OpenAction")] = ArrayObject([
        writer.pages[0].indirect_reference, NameObject("/Fit")])
    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    # A failed conversion never replaces a previously usable PDF.
    with tempfile.TemporaryDirectory(prefix=".svg-pdf-", dir=destination.parent) as tmp:
        candidate = Path(tmp) / "candidate.pdf"
        with candidate.open("wb") as stream:
            writer.write(stream)
        log("Verifying page, text inventory and hyperlinks …")
        check = PdfReader(candidate)
        page = check.pages[0]
        actual = Counter(c for c in page.extract_text() if not c.isspace())
        annotations = page.get("/Annots", [])
        expected_urls = Counter(a.get("href") or a.get(f"{{{XLINK}}}href") for a in anchors)
        actual_urls = Counter(str(a.get_object()["/A"]["/URI"]) for a in annotations)
        text_ok = expected == actual
        page_ok = list(page.cropbox) == list(page.mediabox)
        source_ok = source.read_bytes() == data
        links_ok = expected_urls == actual_urls
        report = {
            "tool_version": __version__, "source_sha256": sha256(data).hexdigest(),
            "pdf_sha256": sha256(candidate.read_bytes()).hexdigest(),
            "pages": len(check.pages), "page_points": list(page.mediabox),
            "source_viewbox": source_viewport.viewbox(), "output_viewbox": page_box.viewbox(),
            "scale": scale, "scale_mode": scale_mode, "max_page_size": options.max_page_size,
            "source_nonspace_characters": sum(expected.values()),
            "pdf_nonspace_characters": sum(actual.values()), "text_inventory_match": text_ok,
            "missing_codepoints": {f"U+{ord(c):04X}": n for c, n in (expected-actual).items()},
            "extra_codepoints": {f"U+{ord(c):04X}": n for c, n in (actual-expected).items()},
            "source_links": len(anchors), "pdf_links": len(annotations),
            "link_targets_match": links_ok, "page_bounds_match": page_ok,
            "source_unchanged": source_ok, "color_link_icons": icons,
            **stats, "fonts": fonts.report(),
            "font_fallbacks": sorted([list(p) for p in fonts.substitutions]),
            "bytes": candidate.stat().st_size, "elapsed_seconds": round(time.monotonic()-started, 2),
        }
        report["status"] = "failed"
        if page_ok and source_ok and links_ok:
            if text_ok:
                report["status"] = "passed"
            elif options.allow_text_differences:
                report["status"] = "text-differences-accepted"
        _write_json(audit_path, report)
        if not links_ok or not page_ok:
            raise ConversionError("PDF links or page bounds failed verification")
        if not text_ok and not options.allow_text_differences:
            raise ConversionError(f"PDF text inventory differs from the SVG. See {audit_path}. "
                                  "PDF was not published. Check fonts, hidden text or unsupported filters.")
        if not source_ok:
            raise ConversionError("Source changed during conversion; rerun with a stable input")
        candidate.replace(destination)
    log(f"Saved: {destination} ({report['bytes']/1_000_000:.2f} MB); audit: {audit_path}")
    return report
