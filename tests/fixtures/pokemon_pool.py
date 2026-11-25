"""An 18-card VGC-style pool used by offline tests (made-up sets, realistic shapes)."""

def _c(i, species, item, ability, nature, evs, moves):
    return {"card_id": f"c{i}", "species": species, "item": item, "ability": ability, "nature": nature,
            "level": 50, "evs": evs, "ivs": {}, "moves": moves}

PHYS = {"hp": 252, "atk": 252, "spe": 4}
SPEC = {"hp": 252, "spa": 252, "spe": 4}
FAST = {"atk": 252, "spa": 4, "spe": 252}
BULK = {"hp": 252, "def": 128, "spd": 128}

POOL = [
    _c(1, "Incineroar", "Sitrus Berry", "Intimidate", "Careful", BULK, ["Fake Out", "Flare Blitz", "Knock Off", "Parting Shot"]),
    _c(2, "Amoonguss", "Rocky Helmet", "Regenerator", "Relaxed", BULK, ["Spore", "Rage Powder", "Pollen Puff", "Protect"]),
    _c(3, "Garchomp", "Life Orb", "Rough Skin", "Jolly", FAST, ["Earthquake", "Dragon Claw", "Rock Slide", "Protect"]),
    _c(4, "Rillaboom", "Assault Vest", "Grassy Surge", "Adamant", PHYS, ["Fake Out", "Grassy Glide", "Wood Hammer", "U-turn"]),
    _c(5, "Flutter Mane", "Booster Energy", "Protosynthesis", "Timid", {"spa": 252, "spe": 252}, ["Moonblast", "Shadow Ball", "Dazzling Gleam", "Protect"]),
    _c(6, "Tornadus", "Covert Cloak", "Prankster", "Timid", {"hp": 252, "spe": 252}, ["Tailwind", "Bleakwind Storm", "Taunt", "Protect"]),
    _c(7, "Urshifu-Rapid-Strike", "Choice Scarf", "Unseen Fist", "Jolly", FAST, ["Surging Strikes", "Close Combat", "Aqua Jet", "U-turn"]),
    _c(8, "Indeedee-F", "Psychic Seed", "Psychic Surge", "Bold", BULK, ["Follow Me", "Trick Room", "Helping Hand", "Protect"]),
    _c(9, "Torkoal", "Charcoal", "Drought", "Quiet", SPEC, ["Eruption", "Heat Wave", "Earth Power", "Protect"]),
    _c(10, "Kingambit", "Black Glasses", "Defiant", "Adamant", PHYS, ["Kowtow Cleave", "Iron Head", "Sucker Punch", "Protect"]),
    _c(11, "Gholdengo", "Choice Specs", "Good as Gold", "Modest", SPEC, ["Make It Rain", "Shadow Ball", "Nasty Plot", "Protect"]),
    _c(12, "Dragonite", "Choice Band", "Multiscale", "Adamant", PHYS, ["Extreme Speed", "Dragon Claw", "Fire Punch", "Iron Head"]),
    _c(13, "Pelipper", "Damp Rock", "Drizzle", "Modest", SPEC, ["Hurricane", "Weather Ball", "Tailwind", "Protect"]),
    _c(14, "Iron Hands", "Booster Energy", "Quark Drive", "Adamant", PHYS, ["Fake Out", "Drain Punch", "Wild Charge", "Heavy Slam"]),
    _c(15, "Rotom-Wash", "Leftovers", "Levitate", "Modest", BULK, ["Hydro Pump", "Thunderbolt", "Will-O-Wisp", "Protect"]),
    _c(16, "Talonflame", "Focus Sash", "Gale Wings", "Jolly", FAST, ["Brave Bird", "Flare Blitz", "Tailwind", "Quick Guard"]),
    _c(17, "Sinistcha", "Mystic Water", "Hospitality", "Bold", BULK, ["Matcha Gotcha", "Rage Powder", "Trick Room", "Life Dew"]),
    _c(18, "Ursaluna", "Flame Orb", "Guts", "Brave", PHYS, ["Facade", "Headlong Rush", "Protect", "Earthquake"]),
]
