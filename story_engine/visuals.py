"""Deterministic illustrated placeholder cards, requiring no API or account."""
from __future__ import annotations

import os
from pathlib import Path
import textwrap

from PIL import Image, ImageDraw, ImageFont

from .providers import VisualAsset


def font(size: int, bold=True):
    candidates = [
        os.getenv("STORY_FONT", ""),
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


INK = "#182437"
PAPER = "#FFF5E4"


def character(draw, prop, x, y, accent, mood=0, scale=1):
    # Draw on a transparent layer to keep future assets independent of layout.
    layer = Image.new("RGBA", (400, 440))
    d = ImageDraw.Draw(layer)
    if prop == "cat":
        d.ellipse((82, 120, 322, 408), fill=accent, outline=INK, width=7)
        d.polygon([(78, 147), (74, 18), (166, 85)], fill=accent, outline=INK, width=7)
        d.polygon([(233, 85), (330, 18), (322, 147)], fill=accent, outline=INK, width=7)
        d.ellipse((70, 56, 330, 280), fill=accent, outline=INK, width=7)
        d.arc((10, 249, 133, 410), 40, 290, fill=INK, width=15)
    elif prop == "fridge":
        d.rounded_rectangle((90, 12, 312, 420), 22, fill=accent, outline=INK, width=7)
        d.line((96, 180, 304, 180), fill=INK, width=6)
        d.line((280, 210, 280, 286), fill=INK, width=9)
    else:
        d.rounded_rectangle((35, 134, 364, 363), 40, fill=accent, outline=INK, width=7)
        d.rounded_rectangle((81, 98, 312, 153), 24, fill=INK)
        d.rounded_rectangle((100, 28, 292, 133), 29, fill="#E2A564", outline=INK, width=6)
        d.rounded_rectangle((115, 43, 277, 116), 23, fill=PAPER)
        d.line((361, 186, 385, 186, 385, 261), fill=INK, width=10)
        d.rounded_rectangle((77, 361, 135, 389), 6, fill=INK)
        d.rounded_rectangle((265, 361, 323, 389), 6, fill=INK)
    ey = 176 if prop == "cat" else 228
    for ex in (152, 244):
        d.ellipse((ex - 10, ey - 12, ex + 10, ey + 12), fill=INK)
    if mood % 3 == 0:
        d.arc((161, ey + 14, 240, ey + 60), 0, 180, fill=INK, width=6)
    elif mood % 3 == 1:
        d.line((170, ey + 48, 231, ey + 40), fill=INK, width=6)
    else:
        d.ellipse((188, ey + 31, 211, ey + 53), fill=INK)
    if prop != "cat":
        d.polygon([(189, 303), (211, 303), (224, 345), (201, 366), (177, 345)], fill=INK)
    layer = layer.resize((int(400 * scale), int(440 * scale)), Image.Resampling.LANCZOS)
    draw.alpha_composite(layer, (int(x), int(y)))


def prop_icon(image, kind, x, y, accent):
    d = ImageDraw.Draw(image)
    if kind == "cheese":
        d.polygon([(x, y+120), (x+180, y+120), (x+150, y+15)], fill="#FFC95E", outline=INK, width=5)
        for dx, dy in [(62, 82), (125, 58), (143, 101)]:
            d.ellipse((x+dx-8, y+dy-8, x+dx+8, y+dy+8), fill="#D88435")
    elif kind in {"moon", "sun"}:
        d.ellipse((x, y, x+170, y+170), fill=accent, outline=INK, width=5)
        if kind == "moon":
            for dx, dy in [(45, 35), (113, 72), (73, 120)]:
                d.ellipse((x+dx-13, y+dy-13, x+dx+13, y+dy+13), fill="#6E68AC")
        else:
            for i in range(8):
                import math
                angle = i * math.pi / 4
                d.line((x+85+98*math.cos(angle), y+85+98*math.sin(angle), x+85+125*math.cos(angle), y+85+125*math.sin(angle)), fill=accent, width=7)
    elif kind == "bagel":
        d.ellipse((x, y, x+168, y+168), fill="#E2A564", outline=INK, width=6)
        d.ellipse((x+58, y+58, x+110, y+110), fill=PAPER, outline=INK, width=4)
    else:
        d.rounded_rectangle((x, y, x+177, y+137), 14, fill=PAPER, outline=INK, width=5)
        if kind == "case":
            d.rounded_rectangle((x+55, y-25, x+120, y+10), 8, outline=INK, width=7)
            d.line((x, y+56, x+177, y+56), fill=INK, width=5)
            d.rectangle((x+80, y+48, x+99, y+68), fill=accent, outline=INK, width=3)
        else:
            for offset in range(35, 111, 25):
                d.line((x+24, y+offset, x+151, y+offset), fill=INK, width=4)
            d.ellipse((x+125, y+86, x+172, y+132), fill=accent, outline=INK, width=3)


class DevelopmentVisualProvider:
    def create(self, story: dict, scene: dict, directory: Path) -> VisualAsset:
        if "dialogue" in story:
            return dialogue_card(story, scene, directory)
        directory.mkdir(parents=True, exist_ok=True)
        number = scene["scene_number"]
        art = story.get("art_direction", {})
        accent = art.get("accent", "#FFBA52")
        image = Image.new("RGBA", (720, 1280), PAPER)
        d = ImageDraw.Draw(image)
        # Large illustrated stage, subtle print pattern, and generous mobile margins.
        for y in range(0, 1280, 28):
            for x in range(0, 720, 28):
                d.ellipse((x, y, x+2, y+2), fill="#E9DECF")
        d.rounded_rectangle((38, 65, 682, 1210), 35, fill=PAPER, outline=INK, width=3)
        d.rounded_rectangle((68, 95, 302, 132), 16, fill=INK)
        d.text((185, 112), "ALMOST NORMAL", font=font(19), fill=PAPER, anchor="mm")
        d.text((644, 114), f"{number:02} / {len(story['scenes']):02}", font=font(18), fill=INK, anchor="rm")
        headline = scene.get("headline") or story["title"]
        lines = textwrap.wrap(headline, width=19)
        title_font = font(46 if len(lines) <= 2 else 38)
        d.multiline_text((360, 225), "\n".join(lines), font=title_font, fill=INK, anchor="mm", align="center", spacing=6)
        d.ellipse((88, 364, 638, 914), fill=accent)
        d.ellipse((104, 380, 622, 898), fill=PAPER)
        d.ellipse((131, 794, 572, 857), fill="#DAD0C0")
        shift = [-18, 5, 22, -4][(number-1) % 4]
        character(image, art.get("prop", "toaster"), 158+shift, 407, accent, mood=number)
        visual = scene["visual_description"].lower()
        icon = "case"
        for keyword, value in [("cheese", "cheese"), ("bagel", "bagel"), ("moon", "moon"), ("sun ", "sun"), ("invoice", "paper"), ("receipt", "paper"), ("envelope", "paper"), ("confirmation", "paper")]:
            if keyword in visual:
                icon = value
                break
        prop_icon(image, icon, 414 if number % 2 else 75, 705 if number % 2 else 714, accent)
        d = ImageDraw.Draw(image)
        d.rounded_rectangle((65, 937, 655, 1095), 24, fill=INK)
        d.line((81, 1140, 639, 1140), fill="#DED3C1", width=2)
        d.text((82, 1170), "CLIP RADAR", font=font(19), fill=INK, anchor="lm")
        d.text((638, 1170), "ORIGINAL FICTION", font=font(14, False), fill="#536074", anchor="rm")
        path = directory / f"scene_{number:02}.png"
        image.convert("RGB").save(path)
        return VisualAsset(path, "image", "deterministic-pillow-storyboard")


def dialogue_card(story, scene, directory):
    """Clearly labeled cast-neutral blocking cards, not generated production art."""
    directory.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (720, 1280), PAPER)
    d = ImageDraw.Draw(image)
    d.text((50, 75), "CLIP RADAR  /  DIALOGUE STUDY", font=font(24), fill=INK)
    title = scene.get("headline") or story["title"]
    d.multiline_text((360, 215), "\n".join(textwrap.wrap(title, 23)), font=font(36), fill=INK, anchor="mm", align="center")
    present = [c for c in story["characters"] if c["character_id"] in scene["characters_present"]]
    colors = ("#74D8CF", "#FFC766", "#A8B7FF", "#F3A5C0", "#B8DEAC")
    for index, c in enumerate(present):
        x = int((index + .5) * 620 / len(present) + 50)
        y = 540 + (scene["scene_number"] % 3 - 1) * 24 * (-1 if index % 2 else 1)
        accent = colors[index % len(colors)]
        d.rounded_rectangle((x-73, y-95, x+73, y+135), 32, fill=accent, outline=INK, width=5)
        for ex in (-27, 27):
            d.ellipse((x+ex-6, y-35, x+ex+6, y-19), fill=INK)
        d.line((x-22, y+23, x+22, y+23+(scene["scene_number"] % 3)*5), fill=INK, width=5)
        d.text((x, y+174), c["name"][:15], font=font(22), fill=INK, anchor="mm")
    action = scene["action_direction"]
    d.multiline_text((360, 810), "\n".join(textwrap.wrap(action, 46)[:3]), font=font(23, False), fill=INK, anchor="mm", align="center")
    d.rounded_rectangle((55, 937, 665, 1105), 24, fill=INK)
    d.text((50, 1180), f"SCENE {scene['scene_number']:02}   •   PLACEHOLDER BLOCKING", font=font(20), fill=INK)
    path = directory / f"scene_{scene['scene_number']:02}.png"
    image.save(path)
    return VisualAsset(path, "image", "deterministic-pillow-storyboard")
