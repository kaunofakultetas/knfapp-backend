# -----------------------------------------------------------
#  [*] fetch_commons_memes — curate openly-licensed reaction GIFs
#
#  Runs ON THE HOST (plain stdlib, network): asks the Wikimedia
#  Commons API for animated reaction GIFs, keeps only files
#  whose per-file license permits reuse (public domain, CC0,
#  CC BY / BY-SA — author and license are recorded in the tags,
#  which satisfies the attribution licenses), downloads them
#  into the drop folder and writes manifest.json beside them.
#  The import half then runs in the container:
#
#    python3 backend/scripts/fetch_commons_memes.py
#    docker exec -i -e MEME_IMPORT_DIR=/data/meme-drop knfapp-backend \
#      python3 - < backend/scripts/meme_library.py
#
#  Commons is the one large source that genuinely allows
#  downloading and rehosting — the big GIF platforms forbid it.
#  Polite client: one identifying User-Agent, sequential
#  requests, a small overall cap.
#
#  Used by:
#    - the operator, to grow the library from open sources
# -----------------------------------------------------------

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request

API = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent": "KNF-app-gif-curation/1.0 (VU Kaunas faculty app; admin@knf.vu.lt)"}
DROP = os.path.join(os.path.dirname(__file__), "..", "..", "_DATA", "backend", "meme-drop")

MIN_BYTES = 40 * 1024
MAX_BYTES = 6 * 1024 * 1024
# An oversized ORIGINAL up to this still comes home — the import
# half shrinks it (downscale + frame stride) inside the container
SHRINK_MAX_BYTES = 30 * 1024 * 1024

OK_LICENSES = ("public domain", "pd", "cc0", "cc by", "cc-by", "no restrictions", "copyrighted free use")

# HAND-CURATED exact Commons file titles → the reaction title the
# library shows. Silent-era clips are the original reaction-GIF
# aesthetic and public domain; each file's license is still
# verified per file before download. Blind keyword search mostly
# surfaces emoticon icons — curation by eye beats it.
CURATED = [
    ('File:GIF of Buster Keaton in "Steamboat Bill Jr" 1928.gif', "Kai viskas griūva", "griuva chaosas laikausi"),
    ("File:The Haunted House by Buster Keaton.gif", "Bėgam iš čia", "begam panika"),
    ("File:Buster Keaton as a bellboy in the March 18 1918 movie The Bell Boy.gif", "Tuoj ateinu", "tuoj darbo minute"),
    ("File:Great Stone Face.GIF", "Rimtas veidas", "rimtas pokerio veidas"),
    ("File:Charlie Chaplin.gif", "Einu namo", "einu namo iki"),
    ("File:Dog's Life.gif", "Su draugu", "draugas suo"),
    ("File:Stan Laurel, Dr Pyckle and Mr Pride.gif", "Kai gauni užduotį", "uzduotis eksperimentas laboras"),
    ("File:The Crowd - Office desks.gif", "Pirmadienis auditorijoje", "pirmadienis paskaita biuras"),
    ("File:Facepalm.gif", "Facepalm", "facepalm ai geda"),
    ("File:Fantômas - Le Faux Magistrat.gif", "Įtartina", "itartina detektyvas"),
    ("File:Fantômas - Le Mort qui tue.gif", "Kas čia vyksta?", "kas vyksta paslaptis"),
    ("File:LaughingOutLoad.gif", "Juokas", "juokas lol haha"),
]


def call(params):
    # Polite: one identifying UA, and a patient backoff when the
    # API answers 429 — curation is never urgent
    query = urllib.parse.urlencode({**params, "format": "json"})
    req = urllib.request.Request(f"{API}?{query}", headers=UA)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code == 429 and attempt < 3:
                time.sleep(20 * (attempt + 1))
                continue
            raise


def file_info(titles):
    # url + size + mime + the per-file license and author
    data = call({
        "action": "query",
        "titles": "|".join(titles),
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        # MediaWiki renders ANIMATED gif thumbnails — the door for
        # originals whose full size is way past our cap
        "iiurlwidth": 360,
    })
    out = []
    for page in data.get("query", {}).get("pages", {}).values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        meta = info.get("extmetadata") or {}
        license_name = (meta.get("LicenseShortName", {}).get("value") or "").strip()
        artist = (meta.get("Artist", {}).get("value") or "").strip()
        # Artist arrives as HTML — keep the bare text
        import re
        artist = re.sub(r"<[^>]+>", "", artist).strip()
        out.append({
            "title": page.get("title", ""),
            "url": info.get("url"),
            "thumburl": info.get("thumburl"),
            "size": info.get("size") or 0,
            "mime": info.get("mime") or "",
            "license": license_name,
            "artist": artist[:60],
        })
    return out


def license_ok(name):
    low = name.lower()
    return any(mark in low for mark in OK_LICENSES)


def main():
    os.makedirs(DROP, exist_ok=True)
    manifest_path = os.path.join(DROP, "manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        with open(manifest_path) as handle:
            manifest = json.load(handle)

    # ONE metadata call for the whole curated list, then slow
    # sequential downloads
    infos = {info["title"]: info for info in file_info([title for title, _t, _g in CURATED])}

    taken = 0
    for commons_title, lt_title, tags in CURATED:
        info = infos.get(commons_title)
        if not info:
            print(f"  missing on Commons: {commons_title!r}")
            continue
        if info["mime"] != "image/gif" or not info["url"]:
            print(f"  skipped {lt_title!r}: not served as a gif")
            continue
        if not license_ok(info["license"]):
            print(f"  skipped {lt_title!r}: license {info['license']!r} not in the allowlist")
            continue
        # The original when it fits; an oversized original within
        # the shrink bound comes home whole for the in-container
        # downscale (Commons does not thumbnail big GIFs animated)
        shrink = False
        if MIN_BYTES <= info["size"] <= MAX_BYTES:
            fetch_url = info["url"]
        elif MAX_BYTES < info["size"] <= SHRINK_MAX_BYTES:
            fetch_url = info["url"]
            shrink = True
        else:
            print(f"  skipped {lt_title!r}: {info['size'] // 1024} KB out of every bound")
            continue

        filename = f"commons-{hashlib.md5(info['url'].encode()).hexdigest()[:10]}.gif"
        if filename in manifest:
            continue
        try:
            req = urllib.request.Request(fetch_url, headers=UA)
            with urllib.request.urlopen(req, timeout=300) as resp, open(os.path.join(DROP, filename), "wb") as handle:
                blob = resp.read()
                if len(blob) > (SHRINK_MAX_BYTES if shrink else MAX_BYTES) or len(blob) < 2048:
                    raise ValueError(f"came back {len(blob) // 1024} KB")
                handle.write(blob)
        except Exception as err:
            print(f"  download failed {lt_title!r}: {err}")
            try:
                os.unlink(os.path.join(DROP, filename))
            except OSError:
                pass
            continue

        credit = info["license"] + (f", {info['artist']}" if info["artist"] else "")
        manifest[filename] = {
            "title": lt_title,
            "tags": f"{tags} reakcija commons-pack {credit.lower()}"[:200],
            **({"shrink": True} if shrink else {}),
        }
        taken += 1
        print(f"  took {lt_title!r} ({info['size'] // 1024} KB, {info['license']})")
        time.sleep(1.5)

    with open(manifest_path, "w") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=1)
    print(f"downloaded {taken} GIFs into {DROP} (+ manifest.json)")


if __name__ == "__main__":
    main()
