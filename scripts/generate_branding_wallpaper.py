"""Render the Linux virtual desktop wallpaper from the shared brand source."""

from __future__ import annotations

import argparse
import math
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw


def generate_wallpaper(logo_path: Path, output_path: Path) -> None:
    """Rebuild the dark diagonal wallpaper without depending on old artwork."""
    width, height = 3840, 2160
    small_size = width // 4, height // 4
    pixels: list[tuple[int, int, int]] = []
    beam_pixels: list[int] = []
    for py in range(small_size[1]):
        y = py / (small_size[1] - 1)
        for px in range(small_size[0]):
            x = px / (small_size[0] - 1)
            red = math.exp(-(((x + 0.12) / 0.42) ** 2) - ((y - 0.95) / 0.60) ** 2)
            ambient = math.exp(-(((x - 0.85) / 0.65) ** 2) - ((y - 0.50) / 0.70) ** 2)
            distance = y - (1.28 - 1.03 * x)
            beam = math.exp(-((distance / 0.055) ** 2)) * (0.15 + 0.85 * x**4)
            halo = math.exp(-((distance / 0.15) ** 2)) * x**3
            sheen = math.exp(-(((x - 0.80) / 0.40) ** 2) - ((y - 1.15) / 0.45) ** 2)
            light = 210 * beam + 38 * halo
            pixels.append(
                (
                    min(255, round(3 + 38 * red + 6 * ambient + 18 * sheen + light * 0.94)),
                    min(255, round(5 + 5 * red + 9 * ambient + 18 * sheen + light * 0.98)),
                    min(255, round(8 + 7 * red + 12 * ambient + 21 * sheen + light)),
                )
            )
            beam_pixels.append(min(255, round(255 * (beam + halo * 0.25))))

    wallpaper = Image.new("RGB", small_size)
    wallpaper.putdata(pixels)
    wallpaper = wallpaper.resize((width, height), Image.Resampling.BICUBIC)
    beam_mask = Image.new("L", small_size)
    beam_mask.putdata(beam_pixels)
    beam_mask = beam_mask.resize((width, height), Image.Resampling.BICUBIC)

    texture = Image.new("L", (width, height))
    draw = ImageDraw.Draw(texture)
    for row, cy in enumerate(range(0, height + 16, 16)):
        for cx in range((row % 2) * 8, width + 16, 16):
            draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=180)
    texture = ImageChops.multiply(texture, beam_mask)
    wallpaper = Image.composite(Image.new("RGB", wallpaper.size, (3, 5, 8)), wallpaper, texture)

    logo = Image.open(logo_path).convert("RGBA")
    logo.thumbnail((256, 256), Image.Resampling.LANCZOS)
    if max(logo.size) < 256:
        scale = 256 / max(logo.size)
        logo = logo.resize(
            (round(logo.width * scale), round(logo.height * scale)), Image.Resampling.LANCZOS
        )
    wallpaper.paste(logo, (width - logo.width - 360, height - logo.height - 300), logo)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Replace the file atomically: Windows image viewers may map the current PNG,
    # which prevents opening that existing file with truncation (EINVAL).
    with tempfile.NamedTemporaryFile(dir=output_path.parent, suffix=".png", delete=False) as fp:
        temporary = Path(fp.name)
    try:
        wallpaper.save(temporary, optimize=True)
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logo", type=Path, default=root / "branding/musae-logo.png")
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "src/octop/infra/desktop/scripts/linux/v1.0/wallpaper.png",
    )
    args = parser.parse_args()
    generate_wallpaper(args.logo, args.output)
