import argparse
import math
from pathlib import Path
import sys

from . import __version__
from .convert import Options, convert
from .fonts import default_cache, install_fonts
from .model import ConversionError


def scale_argument(value):
    if value == "auto":
        return None
    try:
        scale = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("scale must be 'auto' or a positive number") from None
    if not math.isfinite(scale) or scale <= 0:
        raise argparse.ArgumentTypeError("scale must be 'auto' or a finite positive number")
    return scale


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "fonts":
        parser = argparse.ArgumentParser(prog="figma-svg-to-pdf fonts", description="Download pinned open fonts (one-time setup).")
        parser.add_argument("--directory", type=Path, default=default_cache())
        args = parser.parse_args(argv[1:])
        try:
            install_fonts(args.directory)
            return 0
        except (OSError, ConversionError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description="Convert a large static Figma SVG to a verified single-page PDF locally.")
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--font-cache", type=Path, default=default_cache())
    parser.add_argument("--font-dir", type=Path, action="append", default=[], help="additional static TTF/OTF font directory (repeatable)")
    parser.add_argument("--scale", type=scale_argument, metavar="auto|NUMBER",
                        help="default: auto, fit the complete SVG within --max-page-size; "
                             "a number overrides this with PDF points per SVG unit")
    parser.add_argument("--max-page-size", type=float, default=10000,
                        help="maximum page side in PDF points for automatic scaling only (default: 10000)")
    parser.add_argument("--padding", type=float, default=2, help="extra SVG units around the complete bounds")
    parser.add_argument("--monochrome-emoji", action="store_true", help="use portable monochrome Noto Emoji instead of the macOS color link icon")
    parser.add_argument("--keep-filters", action="store_true", help="disable shadow optimization; filtered text may be rasterized")
    parser.add_argument("--shadow-density", type=float, default=2, help="shadow raster pixels per SVG unit")
    parser.add_argument("--allow-text-differences", action="store_true", help="explicitly accept text inventory differences; always recorded in the audit")
    parser.add_argument("--audit", type=Path, help="audit JSON path (default: beside the PDF)")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    options = Options(font_cache=args.font_cache, font_dirs=tuple(args.font_dir), scale=args.scale,
        max_page_size=args.max_page_size, padding=args.padding, color_emoji=not args.monochrome_emoji,
        keep_filters=args.keep_filters, shadow_density=args.shadow_density,
        allow_text_differences=args.allow_text_differences, overwrite=args.overwrite)
    try:
        convert(args.input, args.output or args.input.with_suffix(".pdf"), options, args.audit,
                log=lambda message: print(message, flush=True))
        return 0
    except (OSError, ConversionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
