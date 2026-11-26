"""tooltips.py - in-game style tooltips (armor, weapon damage, stats, Use/Equip abilities, requirements) for weapons
and armor, from Blizzard's item data (Forever static namespace). Cached in tooltip_cache.json so each item is fetched once.
Cache: {id: [quality, [[text, cls], ...]]}  cls: '' white, 'g' green (equip bonus / ability), 'm' grey."""
import json
import os
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "tooltip_cache.json")


def load():
    try:
        return json.load(open(CACHE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def lines(p):
    """preview_item -> [quality, [[text, cls], ...]]"""
    out = []
    add = lambda t, c="": t and out.append([t, c])
    add((p.get("binding") or {}).get("name"))
    if p.get("unique_equipped"):
        add(p["unique_equipped"] if isinstance(p["unique_equipped"], str) else "Unique")
    slot = (p.get("inventory_type") or {}).get("name") or ""
    sub = (p.get("item_subclass") or {}).get("name") or ""
    add(f"{slot} - {sub}" if slot and sub and sub != "Miscellaneous" else slot or sub)
    w = p.get("weapon") or {}
    if w:
        add(f'{(w.get("damage") or {}).get("display_string", "")}   {(w.get("attack_speed") or {}).get("display_string", "")}'.strip())
        add((w.get("dps") or {}).get("display_string"))
    add(((p.get("armor") or {}).get("display") or {}).get("display_string"))
    for s in p.get("stats") or []:
        add((s.get("display") or {}).get("display_string"), "g" if s.get("is_equip_bonus") else "")
    for r in p.get("resistances") or []:
        add((r.get("display") or {}).get("display_string"))
    add((p.get("durability") or {}).get("display_string"), "m")
    req = p.get("requirements") or {}
    add((req.get("level") or {}).get("display_string"))
    add((req.get("skill") or {}).get("display_string"))
    add((req.get("playable_classes") or {}).get("display_string"))
    add((req.get("reputation") or {}).get("display_string"))
    for s in p.get("spells") or []:
        add(s.get("description"), "g")
    st = p.get("set") or {}
    if st:
        add((st.get("display_string") or (st.get("item_set") or {}).get("name") or ""), "m")
        for e in st.get("effects") or []:
            add(e.get("display_string"), "m")
    add(p.get("description") and f'"{p["description"]}"', "y")
    return [(p.get("quality") or {}).get("type", "COMMON"), out]


def fetch(ids, budget=400):
    """Make sure up to `budget` of these ids are cached (missing ones fetched in parallel). Returns the cache."""
    c = load()
    todo = [i for i in dict.fromkeys(int(x) for x in ids) if str(i) not in c][:budget]
    if todo:
        import forever_ah as f
        api = f.Api(f.load_config())

        def one(i):
            try:
                d, _ = api.get(f"/data/wow/item/{i}", "static-forever-us")
                return i, lines(d.get("preview_item") or {})
            except Exception as e:
                return i, None if "404" not in str(e) else ["COMMON", []]
        with ThreadPoolExecutor(8) as ex:
            for i, v in ex.map(one, todo):
                if v is not None:
                    c[str(i)] = v
        json.dump(c, open(CACHE, "w", encoding="utf-8"), separators=(",", ":"))
    return c
