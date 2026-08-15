#!/usr/bin/env python3
"""Fetch assets the app needs but does not carry in source: subtitle fonts, the local semantic
model, and an FFmpeg that can draw text.

Two things here are not obvious and are the reason this script exists rather than a README
paragraph telling someone to download three files.

The first is FFmpeg. Subtitles are burned in by libass, and a great many FFmpeg builds are
compiled without it — including the one Homebrew currently ships, which has neither libass nor
libfreetype and so cannot draw text at all. An FFmpeg without libass does not fail loudly when
asked for subtitles; behaviour differs by build, and "the export finished and the text is
missing" is the worst of the possibilities. So the binary is pinned and fetched rather than
found.

The second is font weight. Google ships Noto Sans SC and Noto Serif SC as variable fonts whose
default instance is the lightest weight in the family — Thin and ExtraLight respectively.
Rendered over footage at subtitle size, they are close to illegible. libass does not instance
variable fonts, so it takes that default. Each is therefore pinned to a static weight here,
which is a modification, which is why the licences are checked below before it happens.

Run:  python scripts/prepare_assets.py [--platform darwin-x86_64|win64|linux64] [--force]
      python scripts/prepare_assets.py --semantic-only
"""

from __future__ import annotations

import argparse
import hashlib
import io
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "backend" / "src" / "automated_video_editing_backend" / "assets" / "fonts"
# Deliberately not inside FONT_DIR. libass's `fontsdir` reads every file in the directory it is
# given and complains about each one it cannot parse as a font, so licence text kept alongside
# the fonts produces an error per licence per render.
LICENSE_DIR = ROOT / "backend" / "src" / "automated_video_editing_backend" / "assets" / "font_licenses"
# Also outside FONT_DIR, for the same reason: these are WOFF2, which libass cannot parse.
PREVIEW_DIR = ROOT / "backend" / "src" / "automated_video_editing_backend" / "assets" / "font_previews"
VENDOR_DIR = ROOT / "backend" / "vendor" / "ffmpeg"
SEMANTIC_DIR = (
    ROOT / "backend" / "src" / "automated_video_editing_backend" / "assets"
    / "semantic" / "bge-small-zh-v1.5"
)

# Weight to pin a variable font to. 700 is the usual subtitle weight: heavy enough to hold an
# outline against moving footage without the counters filling in at small sizes.
SUBTITLE_WEIGHT = 700

# Characters the font picker needs to draw. A name in a list tells an operator nothing about
# what the font looks like, so the picker renders each font's name in that font and shows a
# sample line — which means the renderer needs the actual typeface.
#
# It cannot have the real one. These are CJK fonts of 10–27 MB each, and loading 50 MB into an
# Electron window to preview three labels is not a trade anyone would make. So each font is cut
# down to just these glyphs, which brings a 27 MB face to roughly 10 KB.
PREVIEW_TEXT = (
    "思源黑体得意宋"          # the three font names
    "机器人从大厅出发缓缓驶过展区"  # a sample line, like a real subtitle
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789，。！？"
)


@dataclass
class FontSpec:
    """One bundled font, and what has to be true of it before the app will use it."""

    key: str
    url: str
    filename: str
    # The family name the ASS style has to ask for. libass matches on this, not on the filename,
    # and a name that does not match is not an error — libass quietly substitutes a system font.
    # So this is asserted after preparation rather than assumed.
    family: str
    license_url: str
    license_name: str
    # Set when the download is a variable font that must be pinned to `SUBTITLE_WEIGHT`.
    instance_weight: int | None = None
    # Rename the family while instancing. Only ever set for fonts whose licence permits it; see
    # `reserved_names`.
    rename_family: str | None = None
    # Names the OFL reserves for this font. A Modified Version may not carry one. Checked
    # against the licence text at fetch time rather than trusted from this table.
    reserved_names: tuple[str, ...] = ()
    # Extracted from a zip rather than downloaded directly.
    zip_member: str | None = None
    notes: str = ""


FONTS: list[FontSpec] = [
    FontSpec(
        key="noto_sans_sc",
        url="https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf",
        filename="NotoSansSC.ttf",
        family="Noto Sans SC",
        license_url="https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/OFL.txt",
        license_name="LICENSE-NotoSansSC.txt",
        instance_weight=SUBTITLE_WEIGHT,
        rename_family="Noto Sans SC",
        # The reserved name is 'Source' — this descends from Source Han Sans. "Noto Sans SC" is
        # not reserved, so a pinned instance may keep it.
        reserved_names=("Source",),
        notes="Variable; ships as Thin by default.",
    ),
    FontSpec(
        key="noto_serif_sc",
        url="https://raw.githubusercontent.com/google/fonts/main/ofl/notoserifsc/NotoSerifSC%5Bwght%5D.ttf",
        filename="NotoSerifSC.ttf",
        family="Noto Serif SC",
        license_url="https://raw.githubusercontent.com/google/fonts/main/ofl/notoserifsc/OFL.txt",
        license_name="LICENSE-NotoSerifSC.txt",
        instance_weight=SUBTITLE_WEIGHT,
        rename_family="Noto Serif SC",
        reserved_names=(),
        notes="Variable; ships as ExtraLight by default.",
    ),
    FontSpec(
        key="smiley_sans",
        url="https://github.com/atelier-anchor/smiley-sans/releases/download/v2.0.1/smiley-sans-v2.0.1.zip",
        zip_member="SmileySans-Oblique.ttf",
        filename="SmileySans.ttf",
        # Kept as the font's own family name. The OFL reserves "Smiley", so this one is shipped
        # exactly as published — not instanced, not renamed. It is already a static, heavy face,
        # so it needs neither.
        family="Smiley Sans Oblique",
        license_url="https://raw.githubusercontent.com/atelier-anchor/smiley-sans/main/LICENSE",
        license_name="LICENSE-SmileySans.txt",
        reserved_names=("Smiley", "得意黑"),
        notes="Static, already heavy. Shipped unmodified because its name is reserved.",
    ),
]

# Builds that carry libass. Homebrew's does not, so "whatever is on PATH" is not good enough.
FFMPEG_BUILDS = {
    "darwin-x86_64": {
        "url": "https://evermeet.cx/ffmpeg/ffmpeg-9.0.zip",
        "member": "ffmpeg",
    },
    "win64": {
        # Immutable upstream snapshot: FFmpeg n8.1.2-34-g9b6c8969e0. Do not use the BtbN
        # `latest` tag here; its asset is replaced daily under the same URL.
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-08-11-13-11/ffmpeg-n8.1.2-34-g9b6c8969e0-win64-gpl-8.1.zip",
        "sha256": "05eedc113542be39af5d0f78f0b1093bafb89c98cecf25b77e8644670293107f",
        "member": "ffmpeg.exe",
        "probe_member": "ffprobe.exe",
    },
    "linux64": {
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz",
        "member": "ffmpeg",
    },
}

# CPU-only semantic matching. This is the MIT-licensed BAAI model converted to ONNX by the
# Hugging Face Transformers.js maintainer and pinned to one verified revision. INT8 is enough
# for short point descriptions and keeps the installed footprint below 25 MB.
SEMANTIC_ASSETS = {
    "model_int8.onnx": {
        "url": "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/75c43b0/onnx/model_int8.onnx?download=true",
        "sha256": "b9837c19ce154ff0726d398ee77abbc03a7faf0476c6f93016c84e531be7ebb5",
    },
    "tokenizer.json": {
        "url": "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/75c43b0/tokenizer.json?download=true",
        "sha256": "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    },
    "LICENSE-BAAI-BGE.txt": {
        "url": "https://raw.githubusercontent.com/FlagOpen/FlagEmbedding/master/LICENSE",
        "sha256": "587a673933425dbc36ec61268d3b954051b2d3ef3c9b322ede357976055ffdd5",
        # Kept with the model and checked at fetch time so a licensing change cannot enter a
        # release silently.
        "contains": "MIT License",
    },
}


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "prepare-assets/1.0"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def check_license(text: str, spec: FontSpec) -> None:
    """Refuse to modify a font whose licence reserves the name it would keep.

    The OFL lets anyone modify and redistribute, with one condition that bites here: a Modified
    Version may not use a Reserved Font Name. Pinning a variable font to one weight makes a
    Modified Version, so a font that reserves the name we intend to keep cannot be instanced —
    it has to ship as published.
    """
    if "SIL Open Font License" not in text:
        raise SystemExit(f"{spec.key}: licence does not look like the OFL; refusing to bundle it")
    declared = [name for name in spec.reserved_names if name in text]
    missing = [name for name in spec.reserved_names if name not in text]
    if missing:
        print(f"    note: expected reserved name(s) {missing} not found in licence text")
    keeping = spec.rename_family or spec.family
    if spec.instance_weight and any(name in keeping for name in declared):
        raise SystemExit(
            f"{spec.key}: licence reserves {declared}, so the instanced font may not be called "
            f"{keeping!r}. Ship it unmodified or pick a different name."
        )


def set_family(font, family: str, weight: int) -> None:
    """Give an instanced font one unambiguous name, and say it is bold.

    Left alone, `instantiateVariableFont` keeps the variable font's own naming, so a font pinned
    to 700 still calls itself "Noto Sans SC Thin". libass would then need to be asked for that
    name to get a bold face, which is the sort of thing that is wrong for a year without anyone
    noticing. The typographic names (16/17) are dropped so that family/subfamily are the only
    answer to what this font is called.
    """
    name_table = font["name"]
    postscript = family.replace(" ", "") + "-Bold"
    for name_id, value in (
        (1, family),
        (2, "Bold"),
        (3, f"{family} Bold; pinned wght={weight}"),
        (4, f"{family} Bold"),
        (6, postscript),
    ):
        name_table.setName(value, name_id, 3, 1, 0x409)
        name_table.setName(value, name_id, 1, 0, 0)
    for name_id in (16, 17, 21, 22):
        name_table.removeNames(nameID=name_id)

    font["OS/2"].usWeightClass = weight
    # Both bold bits, or the two halves of the stack disagree about what this face is: fontconfig
    # reads fsSelection, some rasterisers read macStyle, and a face that claims bold in one and
    # regular in the other can be synthetically emboldened on top of glyphs that are already bold.
    font["OS/2"].fsSelection = (font["OS/2"].fsSelection & ~0b1000000) | 0b100000
    font["head"].macStyle |= 0b1


def prepare_font(spec: FontSpec, force: bool) -> None:
    target = FONT_DIR / spec.filename
    license_target = LICENSE_DIR / spec.license_name
    if target.exists() and license_target.exists() and not force:
        # The font itself is here, but the picker's preview may predate this step or have been
        # cleaned away. Rebuilding it is cheap and needs no network, so it is not worth making
        # someone re-download 50 MB of fonts to get one back.
        if not (PREVIEW_DIR / f"{spec.key}.woff2").exists():
            print(f"  {spec.key}: present; rebuilding preview")
            make_preview(target, spec)
        else:
            print(f"  {spec.key}: present, skipping (use --force to refetch)")
        return

    print(f"  {spec.key}: fetching")
    license_text = fetch(spec.license_url).decode("utf-8", errors="replace")
    check_license(license_text, spec)
    LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    license_target.write_text(license_text, encoding="utf-8")

    payload = fetch(spec.url)
    if spec.zip_member:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            member = next(
                (n for n in archive.namelist() if n.endswith(spec.zip_member)), None
            )
            if member is None:
                raise SystemExit(f"{spec.key}: {spec.zip_member} not found in the archive")
            payload = archive.read(member)

    FONT_DIR.mkdir(parents=True, exist_ok=True)
    if spec.instance_weight:
        from fontTools import ttLib
        from fontTools.varLib import instancer

        font = ttLib.TTFont(io.BytesIO(payload))
        if "fvar" not in font:
            raise SystemExit(f"{spec.key}: expected a variable font, got a static one")
        print(f"    pinning wght={spec.instance_weight}")
        instancer.instantiateVariableFont(
            font, {"wght": spec.instance_weight}, inplace=True, updateFontNames=False
        )
        set_family(font, spec.rename_family or spec.family, spec.instance_weight)
        font.save(target)
    else:
        target.write_bytes(payload)

    verify_family(target, spec)
    make_preview(target, spec)


def make_preview(source: Path, spec: FontSpec) -> None:
    """Cut a font down to the few glyphs the picker draws, as WOFF2.

    Subsetting is done here rather than in the app because it needs fontTools and brotli, and
    neither has any business being a runtime dependency of a video editor. The result is a file
    small enough to hand to the renderer inline.
    """
    from fontTools import subset
    from fontTools.ttLib import TTFont

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    target = PREVIEW_DIR / f"{spec.key}.woff2"
    font = TTFont(source)
    options = subset.Options()
    options.flavor = "woff2"
    # Layout features are dropped: at a single size, in a picker, none of them change what the
    # operator sees, and they carry tables far larger than the glyphs themselves.
    options.layout_features = []
    options.desubroutinize = True
    options.notdef_outline = False
    options.drop_tables += ["DSIG"]
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(text=PREVIEW_TEXT)
    subsetter.subset(font)
    font.flavor = "woff2"
    font.save(target)

    before = source.stat().st_size / 1e6
    after = target.stat().st_size / 1e3
    print(f"    preview: {after:.1f} KB (from {before:.1f} MB)")


def verify_family(path: Path, spec: FontSpec) -> None:
    """Confirm the font calls itself what the app will ask libass for.

    This is the check that matters. A style naming a family no installed font provides is not an
    error in libass — it substitutes something else and renders happily, so the export succeeds
    with the wrong typeface. Catching it here is the difference between a build failure and a
    hundred videos in the wrong font.
    """
    from fontTools import ttLib

    font = ttLib.TTFont(path, fontNumber=0, lazy=True)
    # Every name the font answers to, not one preferred name. A font may carry several: Smiley
    # Sans is "Smiley Sans Oblique" in nameID 1 and "得意黑" in nameID 16, and libass will match
    # on either. Picking one and comparing against it failed a font that works.
    families = {
        record.toUnicode()
        for record in font["name"].names
        if record.nameID in (1, 16)
    }
    if spec.family not in families:
        raise SystemExit(
            f"{spec.key}: font answers to {sorted(families)} but the app will ask for "
            f"{spec.family!r}. libass would silently substitute a system font."
        )
    size_mb = path.stat().st_size / 1e6
    print(f"    ok: {spec.family!r} ({size_mb:.1f} MB)")


def prepare_ffmpeg(platform_key: str, force: bool) -> Path | None:
    build = FFMPEG_BUILDS.get(platform_key)
    if build is None:
        raise SystemExit(f"no pinned FFmpeg for {platform_key!r}; known: {list(FFMPEG_BUILDS)}")
    target_dir = VENDOR_DIR / platform_key
    target = target_dir / build["member"]
    member_names = [build["member"]]
    if build.get("probe_member"):
        member_names.append(build["probe_member"])
    targets = [target_dir / member_name for member_name in member_names]
    if all(path.exists() for path in targets) and not force:
        print(f"  ffmpeg ({platform_key}): present, skipping")
        return target

    print(f"  ffmpeg ({platform_key}): fetching")
    payload = fetch(build["url"])
    expected_digest = build.get("sha256")
    if expected_digest:
        actual_digest = hashlib.sha256(payload).hexdigest()
        if actual_digest != expected_digest:
            raise SystemExit(
                f"FFmpeg archive checksum mismatch: expected {expected_digest}, got {actual_digest}"
            )
    target_dir.mkdir(parents=True, exist_ok=True)
    if build["url"].endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member_name, member_target in zip(member_names, targets):
                member = next(
                    (n for n in archive.namelist() if n.endswith("/" + member_name)
                     or n == member_name),
                    None,
                )
                if member is None:
                    raise SystemExit(f"{member_name} not found in the archive")
                member_target.write_bytes(archive.read(member))
    else:
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:xz") as archive:
            for member_name, member_target in zip(member_names, targets):
                member = next(
                    (m for m in archive.getmembers() if m.name.endswith("/" + member_name)), None
                )
                if member is None:
                    raise SystemExit(f"{member_name} not found in the archive")
                extracted = archive.extractfile(member)
                member_target.write_bytes(extracted.read() if extracted else b"")
    for member_target in targets:
        member_target.chmod(0o755)
    return target


def verify_ffmpeg(path: Path) -> None:
    """Confirm the fetched binary can actually draw text."""
    result = subprocess.run(
        [str(path), "-hide_banner", "-version"], capture_output=True, text=True, timeout=60, check=False
    )
    if "--enable-libass" not in result.stdout:
        raise SystemExit(f"{path} was built without libass and cannot burn in subtitles")
    print(f"    ok: {result.stdout.splitlines()[0][:60]} (libass present)")


def prepare_semantic(force: bool) -> None:
    """Fetch and verify the offline Chinese embedding model and its tokenizer."""
    SEMANTIC_DIR.mkdir(parents=True, exist_ok=True)
    for filename, spec in SEMANTIC_ASSETS.items():
        target = SEMANTIC_DIR / filename
        if target.exists() and not force:
            payload = target.read_bytes()
        else:
            print(f"  semantic/{filename}: fetching")
            payload = fetch(spec["url"])
            target.write_bytes(payload)
        digest = spec.get("sha256")
        if digest and hashlib.sha256(payload).hexdigest() != digest:
            target.unlink(missing_ok=True)
            raise SystemExit(f"semantic/{filename}: checksum mismatch")
        marker = spec.get("contains")
        if marker and marker not in payload.decode("utf-8", errors="replace"):
            target.unlink(missing_ok=True)
            raise SystemExit(f"semantic/{filename}: expected licence marker is missing")
        print(f"    ok: {filename} ({len(payload) / 1e6:.1f} MB)")


def current_platform() -> str:
    if sys.platform == "darwin":
        return "darwin-x86_64"
    if sys.platform.startswith("win"):
        return "win64"
    return "linux64"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default=current_platform(), choices=list(FFMPEG_BUILDS))
    parser.add_argument("--force", action="store_true", help="refetch even when present")
    parser.add_argument("--fonts-only", action="store_true")
    parser.add_argument("--ffmpeg-only", action="store_true")
    parser.add_argument("--semantic-only", action="store_true")
    args = parser.parse_args()

    selected = args.fonts_only or args.ffmpeg_only or args.semantic_only
    if not selected or args.fonts_only:
        print("Fonts:")
        for spec in FONTS:
            prepare_font(spec, args.force)
    if not selected or args.ffmpeg_only:
        print("FFmpeg:")
        binary = prepare_ffmpeg(args.platform, args.force)
        if binary and args.platform == current_platform():
            verify_ffmpeg(binary)
        elif binary:
            print(f"    fetched for {args.platform}; not verifiable from {current_platform()}")
    if not selected or args.semantic_only:
        print("Semantic model:")
        prepare_semantic(args.force)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
