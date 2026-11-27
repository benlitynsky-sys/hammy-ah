"""wowhead_src.py - where Forever recipe items come from, per Wowhead's WoW Forever database (wowhead_recipe_src.txt).
load() -> {recipe item id: (kind, text)}  kind: drop / vendor / quest / item / alliance"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ZONES = {1: "Dun Morogh", 3: "Badlands", 4: "Blasted Lands", 8: "Swamp of Sorrows", 10: "Duskwood", 15: "Dustwallow Marsh",
         16: "Azshara", 28: "Western Plaguelands", 33: "Stranglethorn Vale", 36: "Alterac Mountains", 41: "Deadwind Pass",
         44: "Redridge Mountains", 45: "Arathi Highlands", 46: "Burning Steppes", 47: "The Hinterlands", 51: "Searing Gorge",
         85: "Tirisfal Glades", 130: "Silverpine Forest", 139: "Eastern Plaguelands", 215: "Mulgore", 267: "Hillsbrad Foothills",
         331: "Ashenvale", 357: "Feralas", 361: "Felwood", 400: "Thousand Needles", 405: "Desolace", 406: "Stonetalon Mountains",
         440: "Tanaris", 490: "Un'Goro Crater", 616: "Mount Hyjal", 618: "Winterspring", 721: "Gnomeregan", 796: "Scarlet Monastery",
         1337: "Uldaman", 1377: "Silithus", 1497: "Undercity", 1519: "Stormwind City", 1537: "Ironforge", 1583: "Blackrock Spire",
         1584: "Blackrock Depths", 1637: "Orgrimmar", 2017: "Stratholme", 2057: "Scholomance", 2557: "Dire Maul"}
ALLIANCE_TOWNS = {1519, 1537, 1657}


def load():
    out = {}
    try:
        lines = open(os.path.join(HERE, "wowhead_recipe_src.txt"), encoding="utf-8").read().splitlines()
    except OSError:
        return out
    for ln in lines:
        if not ln.strip() or ln.startswith("#"):
            continue
        ids, kind, rest = ln.split("|", 2)
        parts, alliance = [], False
        for k, src in enumerate(rest.split(";")):
            k_kind = kind
            if k and " " in src.split("@")[0] and src.split(" ", 1)[0] in ("quest", "vendor", "drop"):
                k_kind, src = src.split(" ", 1)
            name, _, z = src.rpartition("@")
            zone = ZONES.get(int(z)) if z.lstrip("-").isdigit() else None
            if k_kind == "vendor" and z.isdigit() and int(z) in ALLIANCE_TOWNS:
                alliance = True
            where = f" ({zone})" if zone else ""
            if k_kind == "drop":
                parts.append(f"Drops from: {name}{where}" if name else f"Drops from mobs{' in ' + zone if zone else ''}")
            elif k_kind == "vendor":
                parts.append(f"Vendor: {name}{where}" if name else f"Vendor{' in ' + zone if zone else ''}")
            elif k_kind == "quest":
                parts.append(f"Quest reward: {name}{where}" if name else f"Quest reward{' in ' + zone if zone else ''}")
            else:
                parts.append(f"Comes from {name}{where}")
        text = "; ".join(parts)
        if alliance:
            kind, text = "alliance", "Alliance only - buy it on the AH. " + text
        for p in ids.split(","):
            lo, _, hi = p.partition("-")
            for i in range(int(lo), int(hi or lo) + 1):
                out[i] = (kind, text)
    return out
