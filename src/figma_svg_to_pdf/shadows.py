"""Cache Figma card shadows without rasterizing the text on the card."""

import base64
import copy
from hashlib import sha256
import re
import xml.etree.ElementTree as ET

import vl_convert as vlc

from .model import Box, ConversionError, local, number, tag, transform
from .svg import DEFAULTS, layout_text, properties, shape_box


def _opaque_card(group, fonts, inherited):
    if not len(group) or local(group[0]) != "rect":
        return None
    rect = group[0]
    if number(inherited.get("fill-opacity"), 1) != 1 or inherited.get("stroke", "none") != "none":
        return None
    if set(group.attrib) - {"filter", "id"}:
        return None
    if set(rect.attrib) - {"width", "height", "x", "y", "transform", "fill", "id"}:
        return None
    if not re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", rect.get("fill", "")):
        return None
    matrix = transform(rect.get("transform"))
    if matrix[:4] != (1, 0, 0, 1):
        return None
    box = shape_box(rect).transformed(matrix)
    for child in list(group)[1:]:
        if local(child) != "text" or child.get("transform") or child.get("filter"):
            return None
        for run in layout_text(child, fonts, inherited):
            if not box.contains(run.box, tolerance=0.001):
                return None
    return box


def optimize_shadows(root, fonts, density=2):
    defs = root.find(tag("defs"))
    if defs is None:
        return {"optimized_cards": 0, "shadow_tiles": 0, "unoptimized_filters": 0}
    filters = {f.get("id"): f for f in defs if local(f) == "filter"}
    ids = {e.get("id") for e in root.iter() if e.get("id")}
    prefix = "pdf_tool_"
    while any(i.startswith(prefix) for i in ids):
        prefix += "x_"
    cache = {}
    contexts = {}
    def context(e, props):
        contexts[e] = props
        for child in e:
            context(child, properties(e, props))
    context(root, DEFAULTS)
    converted = 0
    skipped = 0
    for group in list(root.iter()):
        if not group.get("filter"):
            continue
        f = filters.get(group.get("filter")[5:-1])
        card = _opaque_card(group, fonts, contexts[group]) if local(group) == "g" else None
        if (f is None or card is None or f.get("filterUnits") != "userSpaceOnUse"
                or len(f) < 2 or local(f[-1]) != "feBlend"
                or f[-1].get("mode", "normal") != "normal"
                or f[-1].get("in") != "SourceGraphic"
                or f[-1].get("in2") != f[-2].get("result")
                or any(c.get("in") == "SourceGraphic" or c.get("in2") == "SourceGraphic"
                       for c in list(f)[:-1])):
            skipped += 1
            continue
        region = Box.xywh(*(number(f.get(k)) for k in ("x", "y", "width", "height")))
        if region.width <= 0 or region.height <= 0:
            raise ConversionError("Invalid shadow filter region")
        if region.width * region.height * density**2 > 16_000_000:
            raise ConversionError("A card shadow exceeds the 16-megapixel tile limit")
        sf = copy.deepcopy(f)
        sf.remove(sf[-1])
        sf.set("id", "shadow")
        sf.set("x", "0")
        sf.set("y", "0")
        for node in sf.iter():
            if node.text is not None and not node.text.strip():
                node.text = None
            if node.tail is not None and not node.tail.strip():
                node.tail = None
        # Filter-local result names have no visual meaning; normalize them so
        # geometrically identical shadows share one image even across exports.
        result_names = {e.get("result"): f"r{i}" for i, e in enumerate(sf) if e.get("result")}
        for e in sf:
            for attr in ("result", "in", "in2"):
                if e.get(attr) in result_names:
                    e.set(attr, result_names[e.get(attr)])
        tile = ET.Element(tag("svg"), {"width": str(region.width*density),
            "height": str(region.height*density),
            "viewBox": f"0 0 {region.width} {region.height}", "fill": "none"})
        ET.SubElement(tile, tag("defs")).append(sf)
        ET.SubElement(tile, tag("rect"), {
            "x": f"{card.x0-region.x0:.6f}", "y": f"{card.y0-region.y0:.6f}",
            "width": f"{card.width:g}", "height": f"{card.height:g}",
            "fill": "black", "filter": "url(#shadow)",
        })
        data = ET.tostring(tile, encoding="unicode")
        digest = sha256(data.encode()).hexdigest()
        if digest not in cache:
            ident = prefix + f"shadow_{len(cache)}"
            cache[digest] = ident
            png = vlc.svg_to_png(data)
            ET.SubElement(defs, tag("image"), {
                "id": ident, "width": str(region.width), "height": str(region.height),
                "preserveAspectRatio": "none",
                "href": "data:image/png;base64,"+base64.b64encode(png).decode(),
            })
        clip_id = prefix + f"clip_{converted}"
        clip = ET.SubElement(defs, tag("clipPath"), {"id": clip_id, "clipPathUnits": "userSpaceOnUse"})
        ET.SubElement(clip, tag("rect"), {key: f.get(key) for key in ("x", "y", "width", "height")})
        foreground = ET.Element(tag("g"), {"clip-path": f"url(#{clip_id})"})
        foreground[:] = list(group)
        group[:] = [ET.Element(tag("use"), {"href": "#"+cache[digest],
                     "x": str(region.x0), "y": str(region.y0)}), foreground]
        group.attrib.pop("filter")
        converted += 1
    remaining = {e.get("filter") for e in root.iter() if e.get("filter")}
    for ident, f in filters.items():
        if f"url(#{ident})" not in remaining:
            defs.remove(f)
    return {"optimized_cards": converted, "shadow_tiles": len(cache),
            "unoptimized_filters": skipped}
