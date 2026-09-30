"""Read the static Figma subset and make text/font geometry explicit."""

from collections import Counter
from dataclasses import dataclass
from itertools import groupby
import re
import xml.etree.ElementTree as ET

from defusedxml import ElementTree as SafeET
from svgpathtools import parse_path

from .model import (Box, ConversionError, IDENTITY, XLINK, local, multiply,
                    number, numbers, tag, transform)

INHERITED = {"font-family", "font-size", "font-weight", "font-style", "letter-spacing",
             "fill", "fill-opacity", "stroke", "stroke-width", "stroke-linejoin",
             "stroke-miterlimit", "text-anchor", "visibility", "direction", "writing-mode"}
DEFAULTS = {"font-family": "Inter", "font-size": "16", "font-weight": "400",
            "fill": "black", "fill-opacity": "1", "text-anchor": "start"}


def text_inventory(root):
    return Counter(c for e in root.iter(tag("text")) for c in "".join(e.itertext())
                   if not c.isspace())


def load_svg(data):
    try:
        root = SafeET.fromstring(data)
    except Exception as exc:
        raise ConversionError(f"Cannot parse SVG: {exc}") from exc
    if root.tag != tag("svg"):
        raise ConversionError("The input must be an SVG document with the SVG namespace")
    ids = set()
    for e in root.iter():
        name = local(e)
        if name in {"script", "foreignObject", "style", "animate", "animateTransform", "set",
                    "textPath", "switch", "symbol", "use"}:
            raise ConversionError(f"Unsupported SVG element <{name}>; use a static Figma export")
        if e is not root and name == "svg":
            raise ConversionError("Nested SVG viewports are not supported")
        if e.get("id"):
            if e.get("id") in ids:
                raise ConversionError("Duplicate SVG id")
            ids.add(e.get("id"))
        for declaration in e.get("style", "").split(";"):
            if not declaration.strip():
                continue
            key, sep, value = declaration.partition(":")
            if not sep or "!important" in value:
                raise ConversionError("Unsupported inline SVG style")
            e.set(key.strip(), value.strip())
        # Preserve inline CSS as well; white-space:pre is used by Figma.
        for key, value in e.attrib.items():
            if key in {"marker-start", "marker-mid", "marker-end"}:
                raise ConversionError("SVG markers need conversion to paths in Figma")
            if key.rsplit("}", 1)[-1].lower().startswith("on"):
                raise ConversionError("Event handlers are not supported")
            if key.endswith("href") and name != "a":
                if not value.startswith("#") and not (
                    name == "image" and re.match(r"data:image/(?:png|jpeg|jpg|webp);base64,", value)
                ):
                    raise ConversionError("Embed image assets in the SVG; external resources are disabled")
            for ref in re.findall(r"url\(\s*['\"]?([^)'\"]+)", value):
                if not ref.startswith("#"):
                    raise ConversionError("External SVG resources are disabled")
        if name == "a":
            href = e.get("href") or e.get(f"{{{XLINK}}}href", "")
            if not re.match(r"^(https?://|mailto:)", href, re.I):
                raise ConversionError("Only http, https and mailto hyperlinks are supported")
    return root


def properties(e, inherited):
    result = inherited.copy()
    result.update({k: v for k, v in e.attrib.items() if k in INHERITED})
    return result


def weight(props):
    value = props.get("font-weight", "400")
    return {"normal": 400, "bold": 700}.get(value) or int(number(value))


def normalize_fonts(root, fonts):
    """Use actual glyph coverage instead of renderer-dependent fallback lists."""
    def make_runs(content, props):
        runs = []
        for _, chars in groupby(content, lambda c: (
            fonts.choose(props["font-family"], weight(props), c).family,
            # Keep color glyphs individually positioned for the overlay.
            c if c == "🔗" else "",
        )):
            text = "".join(chars)
            face = fonts.choose(props["font-family"], weight(props), text[0])
            span = ET.Element(tag("tspan"), {"font-family": fonts.renderer_family(face)})
            span.text = text
            runs.append(span)
        return runs

    def visit(e, inherited, in_text=False):
        props = properties(e, inherited)
        in_text = in_text or local(e) == "text"
        original_children = list(e)
        for child in original_children:
            visit(child, props, in_text)
        if not in_text:
            return
        children = []
        if e.text:
            children.extend(make_runs(e.text, props))
            e.text = None
        for child in original_children:
            children.append(child)
            if child.tail:
                children.extend(make_runs(child.tail, props))
                child.tail = None
        e[:] = children
    visit(root, DEFAULTS)
    fonts.register()


@dataclass
class Run:
    text: str
    x: float
    y: float
    size: float
    width: float
    spacing: float
    face: object
    matrix: tuple
    alpha: float
    link: object = None

    @property
    def box(self):
        # Enclose ascenders/descenders conservatively, including color emoji.
        ascent = max(1.2, self.face.font["hhea"].ascent/self.face.units)
        descent = max(.3, -self.face.font["hhea"].descent/self.face.units)
        return Box(self.x, self.y-self.size*ascent,
                   self.x+max(0, self.width), self.y+self.size*descent)


def layout_text(element, fonts, inherited=None, matrix=IDENTITY, alpha=1, position_runs=False):
    runs = []
    cursor = [0., 0.]

    def visit(e, props, link, opacity):
        props = properties(e, props)
        opacity *= number(e.get("opacity"), 1)
        if props.get("text-anchor", "start") != "start":
            raise ConversionError("Text anchors other than 'start' need conversion to outlines in Figma")
        if props.get("direction", "ltr") != "ltr" or props.get("writing-mode", "horizontal-tb") not in {"horizontal-tb", "lr-tb"}:
            raise ConversionError("Only horizontal, left-to-right text is supported")
        if props.get("font-style", "normal") != "normal":
            raise ConversionError("Italic text is not supported by the font fallback pass")
        if e.get("textLength") or e.get("rotate") or (e is not element and e.get("transform")):
            raise ConversionError("Unsupported per-character text positioning")
        if e.get("x") is not None:
            cursor[0] = number(e.get("x"))
        if e.get("y") is not None:
            cursor[1] = number(e.get("y"))
        cursor[0] += number(e.get("dx"))
        cursor[1] += number(e.get("dy"))
        if local(e) == "a":
            link = e
        if e.text:
            size = number(props.get("font-size"), 16)
            raw_spacing = props.get("letter-spacing", "0")
            spacing = (number(raw_spacing[:-2])*size if raw_spacing.endswith("em")
                       else number(raw_spacing))
            face = fonts.choose(props["font-family"], weight(props), e.text[0])
            content = e.text.replace("\n", "").replace("\r", "")
            width = face.text_width(content, spacing)*size + spacing*len(content)
            if content:
                if position_runs:
                    # A new absolute-positioned chunk makes usvg resolve the
                    # selected font against this run, not the mixed-script line.
                    e.set("x", f"{cursor[0]:.10g}")
                    e.set("y", f"{cursor[1]:.10g}")
                runs.append(Run(content, *cursor, size, width, spacing, face, matrix,
                                opacity*number(props.get("fill-opacity"), 1), link))
            cursor[0] += width
        for child in e:
            visit(child, props, link, opacity)
    visit(element, inherited or DEFAULTS, None, alpha)
    return runs


def shape_box(e):
    name = local(e)
    if name in {"rect", "image"}:
        return Box.xywh(number(e.get("x")), number(e.get("y")),
                        number(e.get("width")), number(e.get("height")))
    if name in {"circle", "ellipse"}:
        x, y = number(e.get("cx")), number(e.get("cy"))
        rx = number(e.get("r") if name == "circle" else e.get("rx"))
        ry = number(e.get("r") if name == "circle" else e.get("ry"))
        return Box(x-rx, y-ry, x+rx, y+ry)
    if name == "line":
        x1, x2 = number(e.get("x1")), number(e.get("x2"))
        y1, y2 = number(e.get("y1")), number(e.get("y2"))
        return Box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    if name in {"polygon", "polyline"}:
        v = numbers(e.get("points", ""))
        if len(v) < 2 or len(v) % 2:
            raise ConversionError("Invalid polygon/polyline points")
        return Box(min(v[::2]), min(v[1::2]), max(v[::2]), max(v[1::2]))
    if name == "path" and e.get("d"):
        path = parse_path(e.get("d"))
        if path:
            x0, x1, y0, y1 = path.bbox()
            return Box(x0, y0, x1, y1)
    return None


def document_geometry(root, fonts):
    ids = {e.get("id"): e for e in root.iter() if e.get("id")}
    all_runs = []
    bounds = None

    def add(box):
        nonlocal bounds
        if box is not None:
            bounds = box if bounds is None else bounds.union(box)

    def visit(e, props, matrix, opacity):
        if local(e) in {"defs", "metadata", "title", "desc"} or e.get("display") == "none":
            return
        current = properties(e, props)
        if current.get("visibility") in {"hidden", "collapse"}:
            return
        matrix = multiply(matrix, transform(e.get("transform")))
        if local(e) == "text":
            runs = layout_text(e, fonts, props, matrix, opacity, position_runs=True)
            all_runs.extend(runs)
            for run in runs:
                add(run.box.transformed(matrix))
            return
        opacity *= number(e.get("opacity"), 1)
        box = shape_box(e)
        if box:
            if current.get("stroke", "none") != "none":
                width = number(current.get("stroke-width"), 1)
                join = current.get("stroke-linejoin", "miter")
                amount = width/2 * (number(current.get("stroke-miterlimit"), 4) if join == "miter" else 1)
                box = box.pad(amount)
            add(box.transformed(matrix))
        if e.get("filter"):
            f = ids.get(e.get("filter")[5:-1])
            if f is not None and f.get("filterUnits") == "userSpaceOnUse":
                fb = Box.xywh(*(number(f.get(k)) for k in ("x", "y", "width", "height")))
                add(fb.transformed(matrix))
        for child in e:
            visit(child, current, matrix, opacity)
    visit(root, DEFAULTS, IDENTITY, 1)
    return bounds, all_runs


def viewport(root):
    if root.get("viewBox"):
        values = numbers(root.get("viewBox"))
        if len(values) != 4:
            raise ConversionError("viewBox must have four numbers")
        box = Box.xywh(*values)
    else:
        box = Box.xywh(0, 0, number(root.get("width")), number(root.get("height")))
    if box.width <= 0 or box.height <= 0:
        raise ConversionError("SVG must have a positive width/height or viewBox")
    return box
