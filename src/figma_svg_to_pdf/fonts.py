"""Explicit font fallback and opt-in, checksum-verified font installation."""

from dataclasses import dataclass
from functools import cached_property
from hashlib import sha256
from importlib.resources import files
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.request import urlopen

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
import uharfbuzz as hb
import vl_convert as vlc

from .model import ConversionError

# vl-convert keeps a process-wide font database containing file paths. Keep
# registered temporary fonts alive for the same lifetime, including repeated
# calls to the Python API after a failed conversion.
_RENDER_FONTS = tempfile.TemporaryDirectory(prefix="figma-pdf-fonts-")


def default_cache():
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/figma-svg-to-pdf/fonts"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "figma-svg-to-pdf/fonts"
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home()/".cache"))) / "figma-svg-to-pdf/fonts"


def _download(url, digest):
    with urlopen(url, timeout=60) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024 or sha256(data).hexdigest() != digest:
        raise ConversionError("Font download checksum mismatch; no font was installed")
    return data


def install_fonts(directory, log=print):
    """Network access exists only in this explicitly invoked setup function."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(files(__package__).joinpath("font_manifest.json").read_text())
    for item in manifest:
        family = item["family"]
        log(f"Installing {family} …")
        original = directory / item["filename"]
        if not original.exists() or sha256(original.read_bytes()).hexdigest() != item["sha256"]:
            original.write_bytes(_download(item["url"], item["sha256"]))
        license_path = directory / (family.replace(" ", "") + "-OFL.txt")
        if not license_path.exists() or sha256(license_path.read_bytes()).hexdigest() != item["license_sha256"]:
            license_path.write_bytes(_download(item["license_url"], item["license_sha256"]))
        variable = TTFont(original)
        if "fvar" not in variable:
            variable.close()
            continue
        # Static faces avoid renderer-dependent variable-font axis defaults.
        for weight, style in [(400, "Regular"), (500, "Medium"), (700, "Bold")]:
            destination = directory / (family.replace(" ", "") + f"-{style}.ttf")
            if destination.exists():
                continue
            axes = {axis.axisTag: (weight if axis.axisTag == "wght" else axis.defaultValue)
                    for axis in variable["fvar"].axes}
            font = instantiateVariableFont(variable, axes, inplace=False)
            names = {1: family, 2: style, 4: family+" "+style,
                     6: family.replace(" ", "")+"-"+style, 16: family, 17: style}
            for record in list(font["name"].names):
                if record.nameID in names:
                    font["name"].setName(names[record.nameID], record.nameID,
                                         record.platformID, record.platEncID, record.langID)
            font.recalcTimestamp = False
            font.save(destination)
            font.close()
        variable.close()
    log(f"Fonts ready: {directory}")


@dataclass
class Face:
    path: Path
    family: str
    weight: int
    font: TTFont

    @cached_property
    def cmap(self):
        return self.font.getBestCmap() or {}

    @cached_property
    def units(self):
        return self.font["head"].unitsPerEm

    def advance(self, char):
        if char in "\n\r":
            return 0
        glyph = self.cmap.get(ord(char))
        return self.font["hmtx"][glyph][0]/self.units if glyph else 0

    @cached_property
    def shaper(self):
        return hb.Font(hb.Face(hb.Blob.from_file_path(str(self.path))))

    def text_width(self, content, letter_spacing):
        if not content:
            return 0
        if self.family == "Apple Color Emoji":
            return sum(self.advance(c) for c in content)
        buffer = hb.Buffer()
        buffer.add_str(content)
        buffer.guess_segment_properties()
        hb.shape(self.shaper, buffer, {"kern": True, "liga": letter_spacing == 0,
                                      "clig": letter_spacing == 0})
        return sum(p.x_advance for p in buffer.glyph_positions)/self.units

    @cached_property
    def digest(self):
        return sha256(self.path.read_bytes()).hexdigest()


class FontBook:
    def __init__(self, cache, directories=(), color_emoji=True):
        self.faces = {}
        self.used = {}
        self.substitutions = set()
        self.color_emoji = color_emoji
        self._choices = {}
        self.aliases = {}
        self._render_fonts = _RENDER_FONTS
        # Explicit directories take precedence over the pinned font cache.
        for directory in [*map(Path, directories), Path(cache)]:
            if not directory.is_dir():
                if directory != Path(cache):
                    raise ConversionError(f"Font directory does not exist: {directory}")
                continue
            for path in sorted(directory.rglob("*")):
                if path.suffix.lower() in {".ttf", ".otf"}:
                    self._add(path)
        apple = Path("/System/Library/Fonts/Apple Color Emoji.ttc")
        if color_emoji and apple.exists():
            self._add(apple, fontNumber=0)

    def _add(self, path, **kwargs):
        try:
            font = TTFont(path, lazy=True, **kwargs)
            if "fvar" in font:
                font.close()
                return
            family = font["name"].getBestFamilyName()
            weight = int(font["OS/2"].usWeightClass)
            face = Face(path, family, weight, font)
            self.faces.setdefault((family.casefold(), weight), face)
        except Exception as exc:
            raise ConversionError(f"Cannot read font {path.name}: {exc}") from exc

    def face(self, family, weight):
        if family in self.aliases:
            return self.aliases[family]
        choices = [f for (name, _), f in self.faces.items() if name == family.casefold()]
        return min(choices, key=lambda f: abs(f.weight-weight)) if choices else None

    def renderer_family(self, face):
        if face.family == "Apple Color Emoji":
            return face.family
        alias = "FigmaPDF" + face.digest[:16]
        destination = Path(self._render_fonts.name) / (alias + face.path.suffix)
        if not destination.exists():
            font = TTFont(face.path)
            # Private temporary family names prevent system fonts or a variable
            # face with the same family name from overriding the selected file.
            for record in list(font["name"].names):
                if record.nameID in {1, 16, 4, 6}:
                    font["name"].setName(alias, record.nameID, record.platformID,
                                         record.platEncID, record.langID)
            font.recalcTimestamp = False
            font.save(destination)
            font.close()
        self.aliases[alias] = face
        return alias

    def register(self):
        vlc.register_font_directory(self._render_fonts.name)

    def choose(self, families, weight, char):
        key = (families, weight, char)
        if key in self._choices:
            return self._choices[key]
        requested = [f.strip().strip("\"'") for f in families.split(",")]
        if not any(self.face(f, weight) for f in requested):
            raise ConversionError(f"Font family {families!r} is not installed. "
                                  "Run 'figma-svg-to-pdf fonts', or add --font-dir.")
        # The link icon is the color glyph emitted by Figma sticky notes.
        candidates = (["Apple Color Emoji"] if self.color_emoji and char == "🔗" else [])
        candidates += requested + ["Noto Sans JP", "Inter", "Noto Sans Symbols 2", "Noto Emoji"]
        for family in candidates:
            face = self.face(family, weight)
            if face and (ord(char) in face.cmap or char.isspace()):
                self.used[str(face.path)] = face
                if family not in requested and not any(f in self.aliases for f in requested):
                    self.substitutions.add((families, face.family))
                self._choices[key] = face
                return face
        raise ConversionError(
            f"No font covers U+{ord(char):04X} in {families!r}. "
            "Run 'figma-svg-to-pdf fonts', or add --font-dir with the original fonts."
        )

    def report(self):
        return [{"file": face.path.name, "family": face.family,
                 "weight": face.weight, "sha256": face.digest}
                for face in self.used.values()]
