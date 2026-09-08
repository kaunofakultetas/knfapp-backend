# -----------------------------------------------------------
#  [*] meme_library — seed and grow the shared meme library
#
#  Runs INSIDE the backend container (it needs Pillow and the
#  /data volume), piped over stdin because the image does not
#  carry this repo:
#
#    docker exec -i knfapp-backend python3 - < backend/scripts/meme_library.py
#    MEME_IMPORT_DIR=/data/meme-drop \
#      docker exec -i -e MEME_IMPORT_DIR=/data/meme-drop knfapp-backend \
#      python3 - < backend/scripts/meme_library.py
#
#  Two jobs, picked by environment:
#    default            — generate the KNF reaction pack (own
#                         content, animated, Lithuanian student
#                         phrases on the brand burgundy) and
#                         seed it idempotently (tag knf-pack)
#    MEME_IMPORT_DIR=…  — import every gif/jpg/png/webp there
#                         (drop files into ./_DATA/backend/… on
#                         the host, it is the /data volume)
#
#  Talks straight to sqlite (DB_PATH) and creates the memes
#  table when the running image predates migration v62 — the
#  same CREATE IF NOT EXISTS the migration runs, so the later
#  rebuild is a no-op.
#
#  Used by:
#    - the operator, whenever the library should grow
# -----------------------------------------------------------

import base64
import io
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

DB_PATH = os.environ.get("DB_PATH", "/data/knfapp.db")
MEMES_DIR = os.environ.get("MEMES_DIR", "/data/memes")
IMPORT_DIR = os.environ.get("MEME_IMPORT_DIR")

BURGUNDY = (123, 0, 63)
CREAM = (255, 246, 250)
SIZE = 240
FRAMES = 14
FRAME_MS = 80

# phrase, tags, style, inverted colours
PACK = [
    ("LABAS", "labas hello sveiki", "bounce", False),
    ("AČIŪ", "aciu thanks", "pulse", False),
    ("GERAI", "gerai ok sutinku", "pulse", True),
    ("TAIP!", "taip yes", "bounce", True),
    ("NE.", "ne no", "slide", False),
    ("KAVOS?", "kava kavos coffee", "bounce", False),
    ("MIEGU", "miegu miegas tired", "slide", True),
    ("SESIJA", "sesija egzaminai panika", "shake", False),
    ("IŠLAIKIAU!", "islaikiau egzaminas pergale", "bounce", True),
    ("PASKAITA ATŠAUKTA", "paskaita atsaukta laisve", "pulse", True),
    ("PIRMADIENIS...", "pirmadienis monday", "slide", False),
    ("PENKTADIENIS!", "penktadienis friday vakarelis", "shake", True),
    ("EINAM", "einam kviesk go", "bounce", False),
    ("VĖLUOJU", "veluoju atsiprasau late", "shake", False),
    ("SUPER", "super puiku fantastika", "pulse", False),
    ("KNF ♥", "knf fakultetas love", "pulse", True),
]


def ensure_table(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS memes (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            tags TEXT,
            added_by TEXT REFERENCES users(id),
            byte_size INTEGER NOT NULL DEFAULT 0,
            width INTEGER,
            height INTEGER,
            preview TEXT,
            search TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_memes_created ON memes(created_at DESC)")
    db.commit()


# Downscale + frame-stride an oversized GIF until it fits the
# library cap: 320px wide, every 2nd then 3rd then 4th frame,
# duration stretched to keep the clip's real pace
def shrink_gif(blob):
    cap = 3 * 1024 * 1024
    try:
        with Image.open(io.BytesIO(blob)) as img:
            total = getattr(img, "n_frames", 1)
            if total < 2:
                return None
            base_duration = img.info.get("duration", 80) or 80
            for stride in (2, 3, 4, 6):
                frames = []
                for index in range(0, total, stride):
                    img.seek(index)
                    frame = img.convert("RGB")
                    frame.thumbnail((320, 320))
                    frames.append(frame)
                if len(frames) < 2:
                    return None
                out = io.BytesIO()
                frames[0].save(
                    out,
                    format="GIF",
                    save_all=True,
                    append_images=frames[1:],
                    duration=min(400, base_duration * stride),
                    loop=0,
                    optimize=True,
                )
                if out.tell() <= cap:
                    return out.getvalue()
    except Exception:
        return None
    return None


def micro_preview(image):
    tiny = image.convert("RGB")
    tiny.thumbnail((14, 14))
    buf = io.BytesIO()
    tiny.save(buf, format="JPEG", quality=60)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# Pillow's bundled default face has NO Lithuanian diacritics —
# AČIŪ came out toothless. A real TTF rides the /data volume
# (host: cp /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
# ./_DATA/backend/fonts/); the default stays the last resort
FONT_PATHS = (
    "/data/fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def load_face(size):
    for path in FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def fit_font(draw, text, max_width):
    # Sized until the phrase fits with a margin; multi-word
    # phrases wrap on the middle space
    for size in range(64, 17, -2):
        font = load_face(size)
        if draw.textlength(text, font=font) <= max_width:
            return font, size
    return load_face(18), 18


def wrap_phrase(phrase):
    words = phrase.split(" ")
    if len(words) < 2 or len(phrase) <= 12:
        return [phrase]
    mid = len(words) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])]


def render_frame(phrase, style, inverted, t):
    # t runs 0..1 over the loop; every style is a small, calm
    # motion — the library should read playful, not seasick
    bg = CREAM if inverted else BURGUNDY
    ink = BURGUNDY if inverted else CREAM
    img = Image.new("RGB", (SIZE, SIZE), bg)
    draw = ImageDraw.Draw(img)

    import math
    lines = wrap_phrase(phrase)
    longest = max(lines, key=len)
    base_font, base_size = fit_font(draw, longest, SIZE - 48)

    dx = dy = 0
    size = base_size
    if style == "pulse":
        size = int(base_size * (1 + 0.10 * math.sin(t * 2 * math.pi)))
    elif style == "bounce":
        dy = int(-14 * abs(math.sin(t * 2 * math.pi)))
    elif style == "slide":
        dx = int(10 * math.sin(t * 2 * math.pi))
    elif style == "shake":
        dx = int(6 * math.sin(t * 6 * math.pi))
    font = load_face(max(14, size))

    line_heights = []
    for line in lines:
        box = draw.textbbox((0, 0), line, font=font)
        line_heights.append(box[3] - box[1])
    total = sum(line_heights) + (len(lines) - 1) * 8
    y = (SIZE - total) // 2 + dy
    for line, lh in zip(lines, line_heights):
        w = draw.textlength(line, font=font)
        draw.text(((SIZE - w) // 2 + dx, y), line, font=font, fill=ink)
        y += lh + 8

    # The brand corner dot ticks around the frame like a clock
    angle = t * 2 * math.pi
    cx = SIZE // 2 + int((SIZE // 2 - 14) * math.sin(angle))
    cy = 14 if math.cos(angle) > 0 else SIZE - 14
    draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=ink)
    return img


def make_gif(phrase, style, inverted):
    frames = [render_frame(phrase, style, inverted, i / FRAMES) for i in range(FRAMES)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=FRAME_MS, loop=0)
    return buf.getvalue(), frames[0]


# The same ASCII fold the list route searches with — both sides
# of the LIKE must agree on it
FOLD = str.maketrans("ąčęėįšųūžĄČĘĖĮŠŲŪŽ", "aceeisuuzaceeisuuz")


def store(db, blob, first_frame, title, tags, width, height, ext="gif"):
    os.makedirs(MEMES_DIR, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(MEMES_DIR, name), "wb") as handle:
        handle.write(blob)
    db.execute(
        """INSERT INTO memes (id, filename, title, tags, added_by, byte_size, width, height, preview, search, created_at)
           VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), name, title, tags, len(blob), width, height, micro_preview(first_frame),
         f"{title} {tags}".translate(FOLD).lower(),
         datetime.now(timezone.utc).replace(tzinfo=None).isoformat()),
    )


def seed_pack(db):
    # Idempotent by the pack tag — rerunning never doubles it
    have = db.execute("SELECT COUNT(*) AS n FROM memes WHERE tags LIKE '%knf-pack%'").fetchone()["n"]
    if have:
        print(f"pack already seeded ({have} rows) — nothing to do")
        return
    for phrase, tags, style, inverted in PACK:
        blob, first = make_gif(phrase, style, inverted)
        store(db, blob, first, phrase, f"{tags} knf-pack", SIZE, SIZE)
        print(f"  seeded {phrase!r} ({len(blob) // 1024} KB, {style})")
    db.commit()
    print(f"seeded {len(PACK)} pack GIFs into {MEMES_DIR}")


def import_dir(db, folder):
    # manifest.json beside the files (the Commons fetcher writes
    # one) names each file's title and tags — attribution included;
    # curated batches also demand real animation. Files without a
    # manifest entry import under their cleaned filename.
    manifest = {}
    manifest_path = os.path.join(folder, "manifest.json")
    if os.path.exists(manifest_path):
        import json
        with open(manifest_path) as handle:
            manifest = json.load(handle)

    count = 0
    for entry in sorted(os.listdir(folder)):
        lower = entry.lower()
        if not lower.endswith((".gif", ".jpg", ".jpeg", ".png", ".webp")):
            continue
        path = os.path.join(folder, entry)
        with open(path, "rb") as handle:
            blob = handle.read()
        is_gif = blob.startswith((b"GIF87a", b"GIF89a"))
        if lower.endswith(".gif") and not is_gif:
            print(f"  skipped {entry}: not a GIF by signature")
            continue
        ext = "gif"
        try:
            with Image.open(io.BytesIO(blob)) as img:
                if is_gif:
                    width, height = img.size
                    frames = getattr(img, "n_frames", 1)
                    first = img.convert("RGB")
                else:
                    # A static meme: normalized like an upload —
                    # EXIF gone, pixels capped, canonical JPEG
                    flat = img.convert("RGB")
                    flat.thumbnail((1600, 1600))
                    out = io.BytesIO()
                    flat.save(out, format="JPEG", quality=85)
                    blob = out.getvalue()
                    width, height = flat.size
                    frames = 1
                    first = flat
                    ext = "jpg"
        except Exception:
            print(f"  skipped {entry}: unreadable")
            continue

        spec = manifest.get(entry)
        if spec and is_gif and frames < 2:
            print(f"  skipped {entry}: a curated GIF must animate, file has one frame")
            continue
        if spec and spec.get("shrink"):
            # An oversized original: downscale to bubble width and
            # stride frames until it fits the library cap
            shrunk = shrink_gif(blob)
            if not shrunk:
                print(f"  skipped {entry}: would not shrink under the cap")
                continue
            blob = shrunk
            with Image.open(io.BytesIO(blob)) as img:
                width, height = img.size
                frames = getattr(img, "n_frames", 1)
                first = img.convert("RGB")
        title = (spec or {}).get("title") or entry.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip() or "GIF"
        tags = (spec or {}).get("tags") or "imported"
        if db.execute("SELECT 1 FROM memes WHERE title = ?", (title[:80],)).fetchone():
            print(f"  skipped {entry}: {title!r} already in the library")
            continue
        store(db, blob, first, title[:80], tags[:200], width, height, ext)
        count += 1
        print(f"  imported {entry} as {title!r} ({frames} frames)")
    db.commit()
    print(f"imported {count} memes from {folder}")


db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
try:
    ensure_table(db)
    if IMPORT_DIR:
        import_dir(db, IMPORT_DIR)
    else:
        seed_pack(db)
finally:
    db.close()
