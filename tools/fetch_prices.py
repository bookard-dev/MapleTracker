"""Fill in NPC sell prices (and a map's monsters) from the MapleClassic Wiki.

Run it by hand; MapleTracker itself never goes online:

    python tools/fetch_prices.py                         # prices for your saved monsters' drops
    python tools/fetch_prices.py --map "The Murky Swamp II"   # + which monsters live on a map
    python tools/fetch_prices.py --all                   # every map's monsters + every item's price
    python tools/fetch_prices.py --drops                 # what the monsters on your maps drop
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


def search_map(game_id):
    """The wiki page whose map id is this one (pages are sometimes named differently: "Mushroom
    Garden (Maple Island)"), or None."""
    wait = _last[0] + DELAY - time.time()
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    q = urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": f'insource:"{int(game_id)}"',
                                "srlimit": 5, "format": "json"})
    req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": "MapleTracker-price-tool"})
    with urllib.request.urlopen(req, timeout=60) as r:
        hits = json.loads(r.read().decode("utf-8")).get("query", {}).get("search", [])
    for h in hits:
        title, text = sources([h["title"]]).get(h["title"], (None, ""))
        if title and "{{Infobox map" in text and (field(text, "id") or "").isdigit()                 and int(field(text, "id")) == int(game_id):
            return title
    return None


def fetch_maps(names, dry, ids=None):
    """Which monsters live on each map, from the maps' wiki pages -> the same cache files a saved
    NiaDB map page would make. A map known from its own saved NiaDB page is left as it is; one
    pieced together from monster pages gets the monsters it's missing. Names with <...> are
    written (...) on the wiki; with the game's map ids (ids), a map missing under its own name is
    looked for by its id."""
    titled = {n: n.replace("<", "(").replace(">", ")") for n in names}
    got = sources(titled.values())
    found = {n: got[t] for n, t in titled.items() if t in got}
    for n in names:
        if n not in found and ids and ids.get(n) and not dry:
            title = search_map(ids[n])
            if title:
                found[n] = sources([title])[title]
    plan, missing = [], []
    for name in names:
        if name not in found:
            missing.append(name)
            continue
        title, text = found[name]
        mid = field(text, "id")
        spawns = re.findall(r"\{\{Map spawn\|monster_id=([^|}]+)\|spawn_count=(\d+)", text)
        if mid and mid.isdigit() and spawns:
            plan.append((title, int(mid), spawns))
    if len(names) == 1:
        if missing:
            print(f'No wiki page called "{names[0]}". Check the spelling (it has to match the page title).')
        elif not plan:
            print(f"{names[0]}: the wiki lists no monsters for it.")
    mobs = sources({m for _, _, sp in plan for m, _ in sp}) if plan and not dry else {}
    changed = 0
    for title, mid, spawns in plan:
        key = f"{mid:09d}"
        path = os.path.join(CACHE, f"map_{key}.json")
        marker = os.path.join(CACHE, f"mapfrommobs_{key}.json")   # same rule as MapleTracker's importer
        if len(names) == 1:
            print(f"{title}: " + ", ".join(f"{m} x{n}" for m, n in spawns))
        if os.path.exists(path) and not os.path.exists(marker) or dry:
            continue
        old = {m["id"]: m for m in (load(path, []) or [])}
        added = []
        for m, n in spawns:
            mtext = mobs.get(m, (None, ""))[1]
            mob_id = field(mtext, "id")
            if mob_id and mob_id.isdigit() and int(mob_id) not in old:
                mname = field(mtext, "name_en") or m
                old[int(mob_id)] = {"id": int(mob_id), "name": mname, "count": int(n)}
                added.append(m)
                npath = os.path.join(CACHE, f"mobname_{int(mob_id)}.json")
                if not os.path.exists(npath):
                    save(npath, {"name": mname})
        if added:
            save(path, sorted(old.values(), key=lambda x: -x.get("count", 0)))
            save(marker, {"from": "monster pages + MapleClassic Wiki"})
            changed += 1
            if len(names) == 1:
                print("   added " + ", ".join(added) + ". Save their NiaDB pages for their drops.")
    if len(names) > 1:
        print(f"maps: {len(plan)} have monsters on the wiki, {changed} got monsters added, "
              f"{len(missing)} have no wiki page under that name.")


def fetch_all_prices(dry):
    """The NPC sell price of every item MapleTracker knows the name of -> wiki_prices.json, used
    for any item with no price of its own (an item page you saved, or one you typed)."""
    names = sorted((load(os.path.join(CACHE, "item_index.json"), {}) or {}).keys())
    print(f"{len(names)} item names: asking the wiki in {len(names) // BATCH + 1} requests...")
    if dry:
        return
    found = item_pages(names)
    prices = {}
    for n, (title, text) in found.items():
        p = (field(text, "sell_price") or "").replace(",", "")
        if p.isdigit():
            prices[n] = int(p)
    save(os.path.join(CACHE, "wiki_prices.json"),
         {"source": SOURCE, "url": WIKI, "prices": prices})
    print(f"items: {len(prices)} prices saved, {len(names) - len(prices)} with no page or no price.")


def fetch_drops(dry):
    """What each monster on your saved maps drops, from the monsters' wiki pages ({{Drop|...}},
    with the rate players saw where the wiki has one) -> mob_<id>.json, the same file a saved NiaDB
    monster page makes. A monster with drops from a saved NiaDB page is left as it is; saving its
    page later replaces what this wrote."""
    mobs = {}
    for p in glob.glob(os.path.join(CACHE, "map_*.json")):
        for m in load(p, []) or []:
            if isinstance(m, dict) and m.get("id") and m.get("name"):
                mobs[int(m["id"])] = m["name"]
    todo = {}
    for mid, name in mobs.items():
        have = load(os.path.join(CACHE, f"mob_{mid}.json"))
        if have and not (isinstance(have[0], dict) and have[0].get("source") == SOURCE):
            continue                                   # from a saved NiaDB page: that wins
        todo[mid] = name
    print(f"{len(todo)} monsters on your maps have no saved drops: asking the wiki...")
    if dry or not todo:
        return
    pages = sources(todo.values())
    drop_re = re.compile(r"\{\{\s*Drop\s*\|([^}]*)\}\}")
    per_mob, names = {}, set()
    for mid, name in todo.items():
        title, text = pages.get(name, (None, ""))
        if not title or (field(text, "id") or "").strip() != str(mid):
            continue                                   # no page, or a different monster
        drops = []
        for body in drop_re.findall(text):
            f = dict(kv.split("=", 1) for kv in body.split("|") if "=" in kv)
            f = {k.strip(): v.strip() for k, v in f.items()}
            item, cat = f.get("item_id", ""), f.get("category", "").lower()
            if not item or cat in ("meso", "nothing"):
                continue
            rate = f.get("drop_rate", "")
            drops.append({"name": item, "cat": "Quest" if cat == "quest" else cat.title(),
                          "rate": float(rate) if re.fullmatch(r"[\d.]+", rate) else None})
            names.add(item)
        if drops:
            per_mob[mid] = (title, drops)
    index = load(os.path.join(CACHE, "item_index.json"), {}) or {}
    items = item_pages(sorted(names)) if names else {}
    saved = 0
    for mid, (title, drops) in per_mob.items():
        out, seen = [], set()
        for dr in sorted(drops, key=lambda d: -(d["rate"] or -1)):
            if dr["name"] in seen:
                continue
            seen.add(dr["name"])
            wid = field(items.get(dr["name"], (None, ""))[1], "id") or ""
            if not wid.isdigit():
                continue                               # no item page: no id, no icon
            out.append({"page": index.get(dr["name"]), "id": int(wid), "name": dr["name"], "cat": dr["cat"],
                        "chance": f"~{dr['rate']:.2g}%" if dr["rate"] else "",
                        "source": SOURCE, "source_url": page_url(title)})
        if out:
            save(os.path.join(CACHE, f"mob_{mid}.json"), out)
            saved += 1
    print(f"drops: {saved} monsters saved, {len(todo) - saved} with no drops on the wiki.")


def fetch_icons(dry):
    """Icons for the drops in your saved drop lists that have none yet (icons/<id>.png), from the
    item pictures on the wiki. Only those few, one picture a second. The icons are Nexon's art; they
    are kept on your PC only (icons/ isn't shared)."""
    icons = os.path.join(HERE, "icons")
    want = {}
    for p in glob.glob(os.path.join(CACHE, "mob_*.json")):
        for dr in load(p, []) or []:
            if isinstance(dr, dict) and dr.get("id") and dr.get("name")                     and not os.path.exists(os.path.join(icons, f"{int(dr['id'])}.png")):
                want[int(dr["id"])] = dr["name"]
    print(f"{len(want)} drops have no icon yet.")
    if dry or not want:
        return
    files = {f"File:{n}.png": i for i, n in want.items()}
    urls = {}
    names = list(files)
    for k in range(0, len(names), BATCH):
        wait = _last[0] + DELAY - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        q = urllib.parse.urlencode({"action": "query", "prop": "imageinfo", "iiprop": "url",
                                    "titles": "|".join(names[k:k + BATCH]), "format": "json", "formatversion": 2})
        req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": "MapleTracker-price-tool"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8")).get("query", {})
        alias = {n["to"]: n["from"] for n in data.get("normalized", [])}
        for pg in data.get("pages", []):
            info = pg.get("imageinfo")
            asked = alias.get(pg["title"], pg["title"])
            if info and asked in files:
                urls[files[asked]] = info[0]["url"]
    os.makedirs(icons, exist_ok=True)
    got = 0
    for iid, url in urls.items():
        time.sleep(1.0)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MapleTracker-price-tool"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
        except OSError as ex:
            print(f"   {want[iid]}: {ex}")
            continue
        if data[1:4] == b"PNG":
            with open(os.path.join(icons, f"{iid}.png"), "wb") as f:
                f.write(data)
            got += 1
    print(f"icons: {got} saved, {len(want) - got} not on the wiki.")


def main():
    dry = "--dry-run" in sys.argv
    args = sys.argv[1:]
    if "--all" in args or "--drops" in args:
        fetch_drops(dry)
        fetch_icons(dry)
    if "--all" in args:
        maps = load(os.path.join(CACHE, "maps.json"), []) or []
        todo = [m for m in maps if not m.get("town")
                and not os.path.exists(os.path.join(CACHE, f"map_{m['id']}.json"))]
        fetch_maps([m["name"] for m in todo], dry, {m["name"]: m["id"] for m in todo})
        if "--maps-only" not in args:
            fetch_all_prices(dry)
    if "--map" in args:
        i = args.index("--map")
        if i + 1 < len(args):
            fetch_maps([args[i + 1]], dry)
    fetch_prices(dry)
    print("\nData: MapleClassic Wiki, https://mapleclassic.wiki (CC BY-NC-SA 4.0)")


if __name__ == "__main__":
    main()
