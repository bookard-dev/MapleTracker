"""Fill in NPC sell prices (and a map's monsters) from the MapleClassic Wiki.

Run it by hand; MapleTracker itself never goes online:

    python tools/fetch_prices.py                         # prices for your saved monsters' drops
    python tools/fetch_prices.py --map "The Murky Swamp II"   # + which monsters live on a map
    python tools/fetch_prices.py --dry-run               # only show what would be looked up

Data: MapleClassic Wiki (https://mapleclassic.wiki), CC BY-NC-SA 4.0. What this saves keeps that
license and credits the page it came from. MapleTracker must stay free and ad-free while it
uses this data.

Only items your saved monsters drop are looked up (50 pages per request, a few seconds apart),
and answers are kept, so running it again only asks about new items. A price from an item page
you saved from NiaDB always wins: those items are skipped, and saving the page later replaces
what this wrote.
"""
import glob
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(HERE, "meowdb_cache")
WIKI = "https://mapleclassic.wiki"
API = WIKI + "/api.php"
DELAY = 3.0                       # seconds between requests
BATCH = 50                        # pages per request (the wiki's limit)
SOURCE = "MapleClassic Wiki (CC BY-NC-SA 4.0)"
_last = [0.0]


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def page_url(title):
    return f"{WIKI}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"


def sources(titles):
    """Wiki source text of pages, asked for 50 at a time: {asked title: (page title, text)}."""
    out = {}
    titles = list(dict.fromkeys(titles))
    for i in range(0, len(titles), BATCH):
        chunk = titles[i:i + BATCH]
        wait = _last[0] + DELAY - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        q = urllib.parse.urlencode({"action": "query", "prop": "revisions", "rvprop": "content",
                                    "rvslots": "main", "titles": "|".join(chunk), "redirects": 1,
                                    "format": "json", "formatversion": 2})
        req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": "MapleTracker-price-tool"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8")).get("query", {})
        # asked title -> final page title (the wiki normalises and follows redirects)
        alias = {t: t for t in chunk}
        for step in ("normalized", "redirects"):
            for n in data.get(step, []):
                for a, b in alias.items():
                    if b == n["from"]:
                        alias[a] = n["to"]
        pages = {p["title"]: p for p in data.get("pages", [])}
        for asked, final in alias.items():
            p = pages.get(final)
            if p and not p.get("missing") and p.get("revisions"):
                out[asked] = (final, p["revisions"][0]["slots"]["main"]["content"])
    return out


def field(text, name):
    """A template field, written on its own line or inline (| a = 1 | b = 2)."""
    m = re.search(r"\|\s*" + name + r"\s*=\s*([^|\n}]*)", text)
    return m.group(1).strip() if m and m.group(1).strip() else None


def is_item(text):
    """An item's page: "Infobox item", or one of its kinds ("Infobox throwable", ...), not a
    monster, map or NPC."""
    m = re.search(r"\{\{\s*Infobox (\w+)", text)
    return bool(m) and m.group(1).lower() not in ("monster", "map", "npc", "quest", "skill")


def item_pages(names):
    """{item name: (page title, source)}; a name that is also something else on the wiki
    ("Sword") has its item at "Sword (item)"."""
    found = {n: v for n, v in sources(names).items() if is_item(v[1])}
    retry = [n for n in names if n not in found]
    if retry:
        alt = sources(f"{n} (item)" for n in retry)
        for n in retry:
            v = alt.get(f"{n} (item)")
            if v and is_item(v[1]):
                found[n] = v
    return found


def wanted():
    """Every drop of every saved monster without an NPC price yet: {NiaDB item page: drop}."""
    out = {}
    for p in glob.glob(os.path.join(CACHE, "mob_*.json")):
        for dr in load(p, []) or []:
            if not isinstance(dr, dict) or not dr.get("page") or not dr.get("name"):
                continue
            have = load(os.path.join(CACHE, f"item_{int(dr['page'])}.json"))
            if have and have.get("npc") is not None:
                continue
            out[int(dr["page"])] = dr
    return out


def fetch_prices(dry):
    todo = wanted()
    if not todo:
        print("Every saved monster's drop already has a price.")
        return
    print(f"{len(todo)} drops have no price yet.")
    if dry:
        for dr in sorted(todo.values(), key=lambda d: d["name"]):
            print("   would look up", dr["name"])
        return
    found = item_pages(list(dict.fromkeys(dr["name"] for dr in todo.values())))
    done, missed = 0, []
    for page, dr in sorted(todo.items(), key=lambda kv: kv[1]["name"]):
        title, text = found.get(dr["name"], (None, ""))
        price = field(text, "sell_price")
        if not title or not price or not re.fullmatch(r"\d+", price.replace(",", "")):
            missed.append(dr["name"])
            continue
        wid = field(text, "id")
        iid = int(wid) if wid and wid.isdigit() else (int(dr["id"]) if dr.get("id") else None)
        if dr.get("id") and iid and int(dr["id"]) != iid:
            missed.append(f"{dr['name']} (the wiki's item has another ID)")
            continue
        cat = (field(text, "category") or "").lower()
        save(os.path.join(CACHE, f"item_{page}.json"),
             {"name": dr["name"], "npc": int(price.replace(",", "")), "id": iid,
              "equip": cat == "equip" or (bool(iid) and iid < 2000000),
              "category": dr.get("cat") or cat, "untradeable": False,
              "source": SOURCE, "source_url": page_url(title)})
        done += 1
        print(f"   {dr['name']}: {int(price.replace(',', '')):,} mesos")
    print(f"\n{done} prices saved. In MapleTracker, open MOBS (or press RELOAD) to see them.")
    if missed:
        print(f"\nNot on the wiki (or no price there) - {len(missed)}. Save their NiaDB page or set "
              "a price on the Loot tab:")
        for n in missed:
            print("   " + n)


def fetch_map(name, dry):
    """Which monsters live on a map, from its wiki page -> the same cache file a saved NiaDB map
    page would make (only if there isn't one yet)."""
    found = sources([name])
    if name not in found:
        print(f'No wiki page called "{name}". Check the spelling (it has to match the page title).')
        return
    title, text = found[name]
    mid = field(text, "id")
    spawns = re.findall(r"\{\{Map spawn\|monster_id=([^|}]+)\|spawn_count=(\d+)", text)
    if not mid or not spawns:
        print(f"{title}: the wiki lists no monsters for it.")
        return
    key = f"{int(mid):09d}"
    path = os.path.join(CACHE, f"map_{key}.json")
    marker = os.path.join(CACHE, f"mapfrommobs_{key}.json")   # same rule as MapleTracker's importer
    print(f"{title}: " + ", ".join(f"{m} x{n}" for m, n in spawns))
    if os.path.exists(path) and not os.path.exists(marker):
        print("   (from a saved NiaDB map page - kept as it is)")
        return
    if dry:
        return
    old = {m["id"]: m for m in (load(path, []) or [])}       # monsters known from their own pages
    mobs = sources(m for m, _ in spawns)
    added = []
    for m, n in spawns:
        mob_id = field(mobs.get(m, (None, ""))[1], "id")
        if mob_id and mob_id.isdigit() and int(mob_id) not in old:
            old[int(mob_id)] = {"id": int(mob_id), "name": field(mobs[m][1], "name_en") or m,
                                "count": int(n)}
            added.append(m)
    if added:
        save(path, sorted(old.values(), key=lambda x: -x.get("count", 0)))
        save(marker, {"from": "monster pages + MapleClassic Wiki"})
        print("   added " + ", ".join(added) + ". Save their NiaDB pages for their drops.")
    else:
        print("   nothing new.")


def main():
    dry = "--dry-run" in sys.argv
    args = sys.argv[1:]
    if "--map" in args:
        i = args.index("--map")
        if i + 1 < len(args):
            fetch_map(args[i + 1], dry)
    fetch_prices(dry)
    print("\nData: MapleClassic Wiki, https://mapleclassic.wiki (CC BY-NC-SA 4.0)")


if __name__ == "__main__":
    main()
