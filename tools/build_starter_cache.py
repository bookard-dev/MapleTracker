"""Pack meowdb_cache/ into starter_cache.zip, which ships with a release so a new install already
knows the maps, monsters, drops and prices looked up so far. On start, MapleTracker copies any
file from it that isn't in meowdb_cache/ yet (it never replaces what's there).

Left out: imported_pages.json (paths on this computer) and subfolders (unused leftovers).

    python tools/build_starter_cache.py
"""
import json
import os
import re
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "meowdb_cache")
OUT = os.path.join(ROOT, "starter_cache.zip")
SKIP = {"imported_pages.json"}


def main():
    names = sorted(f for f in os.listdir(CACHE)
                   if f.endswith(".json") and f not in SKIP and os.path.isfile(os.path.join(CACHE, f)))
    tmp = OUT + ".tmp"
    kept = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in names:
            try:
                with open(os.path.join(CACHE, f), encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                print("skipped (unreadable):", f)
                continue
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            if re.search(r"[A-Za-z]:\\\\Users\\\\", text):
                print("skipped (has a local path):", f)
                continue
            z.writestr(f, text)
            kept += 1
    os.replace(tmp, OUT)
    print(f"{kept} files -> {OUT} ({os.path.getsize(OUT) // 1024} KB)")


if __name__ == "__main__":
    main()
