"""Regenerate application branding from the supplied, unmodified PNG.

Run from any directory: uv run --no-sync python scripts/generate_branding.py
Pillow is already an Octop dependency. Generated SVGs embed the original raster;
they do not trace or redraw the supplied artwork.
"""

from __future__ import annotations

import base64
import html
import io
import math
import os
import re
import tempfile
from pathlib import Path

from generate_branding_wallpaper import generate_wallpaper
from PIL import Image, ImageCms, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "branding/musae-logo.png"


def tile(image: Image.Image, edge: int, content: int | None = None) -> Image.Image:
    canvas = Image.new("RGBA", (edge, edge))
    scale = (content or edge) / max(image.size)
    artwork = image.resize(
        (round(image.width * scale), round(image.height * scale)),
        Image.Resampling.LANCZOS,
    )
    canvas.alpha_composite(artwork, ((edge - artwork.width) // 2, (edge - artwork.height) // 2))
    return canvas


def write_bytes(destination: Path, payload: bytes) -> None:
    if destination.is_file() and destination.read_bytes() == payload:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as fp:
        temporary = Path(fp.name)
        fp.write(payload)
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def save(image: Image.Image, relative: str, **options: object) -> None:
    destination = ROOT / relative
    if destination.suffix == ".webp":
        options.update(lossless=True, exact=True)
    output = io.BytesIO()
    image.save(output, format=Image.registered_extensions()[destination.suffix.lower()], **options)
    write_bytes(destination, output.getvalue())


def screenshots(image: Image.Image) -> None:
    """Replace only the known brand rectangles in the bundled guide screenshots."""
    headers = (
        "model-01-custom.png",
        "model-02-manage.png",
        "readme-chat-zh.png",
        "readme-chat.png",
        "use-02-chat.png",
        "use-03-agent.png",
        "use-04-channels.png",
        "use-05-cron.png",
        "use-06-settings.png",
        "use-07-remote-desktop.png",
        "use-08-browser-ai.png",
    )
    regions: dict[str, list[tuple[int, int, int, int]]] = {
        name: [(18, 48, 132, 83)] for name in headers
    }
    regions.update(
        {
            "setup-01-steps.png": [(55, 39, 145, 67)],
            "setup-02-admin.png": [(93, 61, 182, 91)],
            "setup-03-model.png": [(45, 26, 135, 56)],
            "use-01-login.png": [(614, 313, 745, 353)],
        }
    )
    # Welcome-page mascots are application branding as well.
    regions["readme-chat-zh.png"].append((786, 218, 955, 350))
    regions["readme-chat.png"].append((789, 191, 958, 323))
    regions["use-02-chat.png"].append((786, 218, 955, 350))
    for name, boxes in regions.items():
        path = ROOT / "docs/assets" / name
        screenshot = Image.open(path).convert("RGBA")
        profile = screenshot.info.get("icc_profile")
        if profile:
            screenshot = ImageCms.profileToProfile(
                screenshot,
                ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                ImageCms.createProfile("sRGB"),
                outputMode="RGBA",
            )
        # PNG without an ICC override uses sRGB. Avoid embedding a newly created
        # profile's timestamp, so regeneration remains byte-for-byte stable.
        screenshot.info.pop("icc_profile", None)
        for left, top, right, bottom in boxes:
            background = screenshot.getpixel((left, top))
            ImageDraw.Draw(screenshot).rectangle(
                (left, top, right - 1, bottom - 1), fill=background
            )
            edge = min(right - left, bottom - top)
            artwork = tile(image, edge)
            screenshot.alpha_composite(artwork, (left + (right - left - edge) // 2, top))
        save(screenshot.convert("RGB"), path.relative_to(ROOT).as_posix())


def guide_images() -> None:
    """Refresh standalone HTML embeds from the Markdown guide's current images."""
    document = ROOT / "docs/user-guide.html"
    markdown = (ROOT / "docs/user-guide.md").read_text(encoding="utf-8")
    sources = dict(re.findall(r"!\[([^\]]*)\]\((assets/[^)]+)\)", markdown))

    def refresh(match: re.Match[str]) -> str:
        tag = match.group()
        alt = re.search(r'alt="([^"]*)"', tag)
        source = sources.get(html.unescape(alt[1])) if alt else None
        if source is None:
            return tag
        data = base64.b64encode((ROOT / "docs" / source).read_bytes()).decode("ascii")
        return re.sub(r'src="[^"]*"', f'src="data:image/png;base64,{data}"', tag, count=1)

    text = document.read_bytes().decode("utf-8")
    write_bytes(document, re.sub(r"<img\b[^>]*>", refresh, text).encode("utf-8"))


def main() -> None:
    source_bytes = SOURCE.read_bytes()
    image = Image.open(SOURCE).convert("RGBA")
    write_bytes(ROOT / "dashboard/public/musae-logo.png", source_bytes)

    sizes = {
        "dashboard/public/pwa-192.png": 192,
        "dashboard/public/pwa-512.png": 512,
        "dashboard/public/apple-touch-icon.png": 180,
        "dashboard/public/logo_horizontal_dark.png": 512,
        "dashboard/public/logo_horizontal_white.png": 512,
        "dashboard/public/octop-mascot-empty.png": 512,
        "dashboard/public/octop-mascot-tasks.png": 512,
        "dashboard/public/octop-mascot-peek.webp": 216,
        "dashboard/public/octop-mascot-type.webp": 216,
        "desktop/src/assets/musae-logo.png": 216,
        "desktop/src/assets/tray-icon.png": 96,
        "desktop/src/build/appicon.png": 512,
        "desktop/src/build/linux/icon.png": 512,
        "src/octop/infra/desktop/scripts/linux/v1.0/start-menu.png": 192,
        "docs/assets/musae-logo.png": 108,
        "docs/assets/readme-banner.png": 216,
        "docs/assets/readme-banner-zh.png": 216,
        "docs/assets/octop-banner.png": 216,
        "docs/assets/overview.png": 216,
    }
    for kind in ("native", "docker"):
        for name, size in (
            ("ICON.PNG", 512),
            ("ICON_256.PNG", 256),
            ("app/ui/images/icon_256.png", 256),
            ("app/ui/images/icon_64.png", 64),
        ):
            sizes[f"fnos/{kind}/{name}"] = size
    for name, size in sizes.items():
        save(tile(image, size), name)

    mac = tile(image, 1024, 824)
    save(mac, "desktop/src/build/appicon-macos.png")
    # Template icons use only alpha; macOS supplies the menu-bar foreground color.
    template = tile(image, 88, 64)
    black = Image.new("RGBA", template.size)
    black.putalpha(template.getchannel("A"))
    save(black, "desktop/src/assets/tray-icon-template.png")

    maskable = Image.new("RGBA", (512, 512), "white")
    radius = max(
        math.hypot(x + 0.5 - image.width / 2, y + 0.5 - image.height / 2)
        for y in range(image.height)
        for x in range(image.width)
        if image.getpixel((x, y))[3] > 0
    )
    # The maskable safe zone is a circle of radius 40% of the icon edge.
    safe_content = math.floor(image.width * (512 * 0.4 - 2) / radius)
    maskable.alpha_composite(tile(image, 512, safe_content))
    save(maskable, "dashboard/public/pwa-maskable-512.png")

    data = base64.b64encode(source_bytes).decode("ascii")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 108 108">\n'
        f'  <image width="108" height="108" href="data:image/png;base64,{data}"/>\n'
        "</svg>\n"
    )
    for name in (
        "dashboard/public/favico.svg",
        "dashboard/public/logo_vertical_dark.svg",
        "dashboard/public/logo_vertical_white.svg",
        "dashboard/src/assets/channels/octop.svg",
        "docs/assets/octop-logo.svg",
    ):
        write_bytes(ROOT / name, svg.encode("utf-8"))

    save(
        tile(image, 256),
        "desktop/src/build/windows/icon.ico",
        sizes=[(edge, edge) for edge in (16, 32, 48, 64, 128, 256)],
    )
    save(mac, "desktop/src/build/darwin/icons.icns")
    generate_wallpaper(SOURCE, ROOT / "src/octop/infra/desktop/scripts/linux/v1.0/wallpaper.png")
    screenshots(image)
    guide_images()
    print(f"Regenerated application branding from {SOURCE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
