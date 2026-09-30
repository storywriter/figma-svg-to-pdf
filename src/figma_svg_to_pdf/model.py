"""SVG numbers, affine transforms, and conservative bounding boxes."""

from dataclasses import dataclass
import math
import re
import xml.etree.ElementTree as ET

SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG)
ET.register_namespace("xlink", XLINK)
NUMBER = r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?"
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


class ConversionError(ValueError):
    """A document cannot be converted without an unverified change."""


def tag(name):
    return f"{{{SVG}}}{name}"


def local(element):
    return element.tag.rsplit("}", 1)[-1]


def number(value, default=0.0):
    if value is None:
        return default
    value = str(value).strip()
    if not re.fullmatch(NUMBER + r"(?:px)?", value):
        raise ConversionError(f"Expected a single SVG number or px value: {value!r}")
    result = float(value.removesuffix("px"))
    if not math.isfinite(result):
        raise ConversionError("Non-finite SVG coordinate")
    return result


def numbers(value):
    if re.sub(NUMBER, "", value).strip(" ,\t\r\n"):
        raise ConversionError(f"Invalid numeric list: {value!r}")
    return [number(v) for v in re.findall(NUMBER, value)]


def multiply(a, b):
    aa, ab, ac, ad, ae, af = a
    ba, bb, bc, bd, be, bf = b
    return (aa*ba + ac*bb, ab*ba + ad*bb, aa*bc + ac*bd,
            ab*bc + ad*bd, aa*be + ac*bf + ae, ab*be + ad*bf + af)


def point(matrix, x, y):
    a, b, c, d, e, f = matrix
    return a*x + c*y + e, b*x + d*y + f


def transform(value):
    result = IDENTITY
    if not value:
        return result
    pattern = r"([A-Za-z]+)\s*\(([^)]*)\)"
    if re.sub(pattern, "", value).strip(" ,\t\r\n"):
        raise ConversionError(f"Invalid transform: {value!r}")
    for name, args in re.findall(pattern, value):
        v = numbers(args)
        if name == "matrix" and len(v) == 6:
            m = tuple(v)
        elif name == "translate" and len(v) in (1, 2):
            m = (1, 0, 0, 1, v[0], v[1] if len(v) == 2 else 0)
        elif name == "scale" and len(v) in (1, 2):
            m = (v[0], 0, 0, v[-1], 0, 0)
        elif name == "rotate" and len(v) in (1, 3):
            c, s = math.cos(math.radians(v[0])), math.sin(math.radians(v[0]))
            m = (c, s, -s, c, 0, 0)
            if len(v) == 3:
                m = multiply(multiply((1, 0, 0, 1, v[1], v[2]), m),
                             (1, 0, 0, 1, -v[1], -v[2]))
        elif name in ("skewX", "skewY") and len(v) == 1:
            t = math.tan(math.radians(v[0]))
            m = (1, 0, t, 1, 0, 0) if name == "skewX" else (1, t, 0, 1, 0, 0)
        else:
            raise ConversionError(f"Unsupported transform: {name}({args})")
        result = multiply(result, m)
    return result


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def xywh(cls, x, y, w, h):
        return cls(x, y, x + w, y + h)

    @property
    def width(self):
        return self.x1 - self.x0

    @property
    def height(self):
        return self.y1 - self.y0

    def union(self, other):
        if other is None:
            return self
        return Box(min(self.x0, other.x0), min(self.y0, other.y0),
                   max(self.x1, other.x1), max(self.y1, other.y1))

    def pad(self, amount):
        return Box(self.x0-amount, self.y0-amount, self.x1+amount, self.y1+amount)

    def transformed(self, matrix):
        pts = [point(matrix, x, y) for x in (self.x0, self.x1)
               for y in (self.y0, self.y1)]
        return Box(min(p[0] for p in pts), min(p[1] for p in pts),
                   max(p[0] for p in pts), max(p[1] for p in pts))

    def contains(self, other, tolerance=0):
        return (self.x0-tolerance <= other.x0 and self.y0-tolerance <= other.y0
                and self.x1+tolerance >= other.x1 and self.y1+tolerance >= other.y1)

    def viewbox(self):
        return [self.x0, self.y0, self.width, self.height]
