"""
city_db.py — ATS city coordinate database for nearest-city lookups.

Sources (in priority order):
  1. ZIP-based mod .scs files (some mods ship unencrypted ZIPs with def/city/*.sii)
  2. Hardcoded base-game city table — the game ships cities in SCS HashFS archives
     (proprietary format) so they can't be read at runtime without a HashFS library.

Coordinates are in ATS world-space units (same system as telemetry coordinateX/Z).
SII files store these as 'x' and 'y'; we store y as 'z' to match our naming.
"""

import logging
import os
import re
import zipfile

_log = logging.getLogger('city_db')

# ── State name normalisation ─────────────────────────────────────────────────

_STATE_NAMES: dict[str, str] = {
    'arizona': 'Arizona', 'california': 'California', 'colorado': 'Colorado',
    'idaho': 'Idaho', 'kansas': 'Kansas', 'montana': 'Montana',
    'nevada': 'Nevada', 'new_mexico': 'New Mexico', 'oregon': 'Oregon',
    'texas': 'Texas', 'utah': 'Utah', 'washington': 'Washington',
    'wyoming': 'Wyoming', 'nebraska': 'Nebraska', 'north_dakota': 'North Dakota',
    'south_dakota': 'South Dakota', 'minnesota': 'Minnesota',
    'iowa': 'Iowa', 'oklahoma': 'Oklahoma',
}

# ── Hardcoded ATS base-game city database ────────────────────────────────────
# Coordinates are in ATS world-space units matching telemetry coordinateX/Z.
# These were derived from community map data and are approximate (±5 000 units).

_ATS_BASE_CITIES: dict[str, dict] = {
    # ── California ──────────────────────────────────────────────────────────
    'los_angeles':     {'name': 'Los Angeles',     'state': 'California', 'x': -98000, 'z':  24000},
    'long_beach':      {'name': 'Long Beach',      'state': 'California', 'x': -95000, 'z':  27000},
    'san_diego':       {'name': 'San Diego',       'state': 'California', 'x': -89000, 'z':  32000},
    'bakersfield':     {'name': 'Bakersfield',     'state': 'California', 'x': -93000, 'z':  17000},
    'fresno':          {'name': 'Fresno',          'state': 'California', 'x': -99000, 'z':   7000},
    'visalia':         {'name': 'Visalia',         'state': 'California', 'x': -98000, 'z':  11000},
    'stockton':        {'name': 'Stockton',        'state': 'California', 'x': -103500,'z':   1000},
    'sacramento':      {'name': 'Sacramento',      'state': 'California', 'x': -102500,'z':  -1000},
    'san_francisco':   {'name': 'San Francisco',   'state': 'California', 'x': -106700,'z':  -8000},
    'oakland':         {'name': 'Oakland',         'state': 'California', 'x': -107000,'z':  -9000},
    'san_jose':        {'name': 'San Jose',        'state': 'California', 'x': -108000,'z':  -3000},
    'santa_cruz':      {'name': 'Santa Cruz',      'state': 'California', 'x': -109000,'z':   1000},
    'monterey':        {'name': 'Monterey',        'state': 'California', 'x': -111000,'z':   4500},
    'eureka':          {'name': 'Eureka',          'state': 'California', 'x': -113500,'z': -25000},
    'redding':         {'name': 'Redding',         'state': 'California', 'x': -108000,'z': -17000},
    'oxnard':          {'name': 'Oxnard',          'state': 'California', 'x': -101500,'z':  20000},
    'merced':          {'name': 'Merced',          'state': 'California', 'x': -101000,'z':   4500},
    'modesto':         {'name': 'Modesto',         'state': 'California', 'x': -102500,'z':   2000},
    'riverside':       {'name': 'Riverside',       'state': 'California', 'x': -92500, 'z':  27500},
    # ── Nevada ──────────────────────────────────────────────────────────────
    'las_vegas':       {'name': 'Las Vegas',       'state': 'Nevada',     'x': -68600, 'z':  17500},
    'henderson':       {'name': 'Henderson',       'state': 'Nevada',     'x': -67500, 'z':  19500},
    'reno':            {'name': 'Reno',            'state': 'Nevada',     'x': -88600, 'z':  -8000},
    'elko':            {'name': 'Elko',            'state': 'Nevada',     'x': -70500, 'z': -19500},
    'carson_city':     {'name': 'Carson City',     'state': 'Nevada',     'x': -89500, 'z':  -7000},
    'winnemucca':      {'name': 'Winnemucca',      'state': 'Nevada',     'x': -81000, 'z': -14000},
    'hawthorne':       {'name': 'Hawthorne',       'state': 'Nevada',     'x': -82500, 'z':   2000},
    'ely':             {'name': 'Ely',             'state': 'Nevada',     'x': -72000, 'z':  -6000},
    # ── Arizona ─────────────────────────────────────────────────────────────
    'phoenix':         {'name': 'Phoenix',         'state': 'Arizona',    'x': -42900, 'z':  40000},
    'tucson':          {'name': 'Tucson',          'state': 'Arizona',    'x': -43200, 'z':  49200},
    'flagstaff':       {'name': 'Flagstaff',       'state': 'Arizona',    'x': -53700, 'z':  32300},
    'kingman':         {'name': 'Kingman',         'state': 'Arizona',    'x': -60900, 'z':  27100},
    'yuma':            {'name': 'Yuma',            'state': 'Arizona',    'x': -72000, 'z':  50500},
    'show_low':        {'name': 'Show Low',        'state': 'Arizona',    'x': -38000, 'z':  37000},
    'prescott':        {'name': 'Prescott',        'state': 'Arizona',    'x': -52000, 'z':  36000},
    'globe':           {'name': 'Globe',           'state': 'Arizona',    'x': -41500, 'z':  42000},
    'wickenburg':      {'name': 'Wickenburg',      'state': 'Arizona',    'x': -50000, 'z':  40500},
    'douglas':         {'name': 'Douglas',         'state': 'Arizona',    'x': -37000, 'z':  53000},
    # ── Utah ────────────────────────────────────────────────────────────────
    'salt_lake_city':  {'name': 'Salt Lake City',  'state': 'Utah',       'x': -50900, 'z': -14600},
    'provo':           {'name': 'Provo',           'state': 'Utah',       'x': -49800, 'z': -10700},
    'ogden':           {'name': 'Ogden',           'state': 'Utah',       'x': -50700, 'z': -18800},
    'price':           {'name': 'Price',           'state': 'Utah',       'x': -41500, 'z': -14000},
    'moab':            {'name': 'Moab',            'state': 'Utah',       'x': -36200, 'z':  -6100},
    'st_george':       {'name': 'St. George',      'state': 'Utah',       'x': -60500, 'z':  -1000},
    'cedar_city':      {'name': 'Cedar City',      'state': 'Utah',       'x': -58500, 'z':  -5500},
    'vernal':          {'name': 'Vernal',          'state': 'Utah',       'x': -33000, 'z': -17000},
    # ── Colorado ────────────────────────────────────────────────────────────
    'grand_junction':  {'name': 'Grand Junction',  'state': 'Colorado',   'x': -23000, 'z':  -4000},
    'denver':          {'name': 'Denver',          'state': 'Colorado',   'x': -17600, 'z':  -7700},
    'colorado_springs':{'name': 'Colorado Springs','state': 'Colorado',   'x': -16500, 'z':  -1000},
    'pueblo':          {'name': 'Pueblo',          'state': 'Colorado',   'x': -14700, 'z':   4200},
    'durango':         {'name': 'Durango',         'state': 'Colorado',   'x': -29500, 'z':   3000},
    'salida':          {'name': 'Salida',          'state': 'Colorado',   'x': -21500, 'z':  -2000},
    'glenwood_springs':{'name': 'Glenwood Springs','state': 'Colorado',   'x': -22000, 'z':  -5000},
    # ── New Mexico ──────────────────────────────────────────────────────────
    'albuquerque':     {'name': 'Albuquerque',     'state': 'New Mexico', 'x': -19200, 'z':  28900},
    'santa_fe':        {'name': 'Santa Fe',        'state': 'New Mexico', 'x': -17000, 'z':  23000},
    'farmington':      {'name': 'Farmington',      'state': 'New Mexico', 'x': -33500, 'z':  17500},
    'roswell':         {'name': 'Roswell',         'state': 'New Mexico', 'x':  -6500, 'z':  36000},
    'carlsbad':        {'name': 'Carlsbad',        'state': 'New Mexico', 'x':  -3500, 'z':  40000},
    'gallup':          {'name': 'Gallup',          'state': 'New Mexico', 'x': -35500, 'z':  27000},
    'las_cruces':      {'name': 'Las Cruces',      'state': 'New Mexico', 'x': -12000, 'z':  50000},
    'santa_rosa':      {'name': 'Santa Rosa',      'state': 'New Mexico', 'x': -10000, 'z':  24000},
    'raton':           {'name': 'Raton',           'state': 'New Mexico', 'x': -11000, 'z':  13500},
    'alamogordo':      {'name': 'Alamogordo',      'state': 'New Mexico', 'x': -12500, 'z':  46000},
    # ── Texas ───────────────────────────────────────────────────────────────
    'el_paso':         {'name': 'El Paso',         'state': 'Texas',      'x':  -8700, 'z':  51200},
    'midland':         {'name': 'Midland',         'state': 'Texas',      'x':  20000, 'z':  31000},
    'lubbock':         {'name': 'Lubbock',         'state': 'Texas',      'x':  23000, 'z':  22000},
    'amarillo':        {'name': 'Amarillo',        'state': 'Texas',      'x':   9800, 'z':  14600},
    'odessa':          {'name': 'Odessa',          'state': 'Texas',      'x':  18500, 'z':  33000},
    'pecos':           {'name': 'Pecos',           'state': 'Texas',      'x':  10000, 'z':  43000},
    # ── Oregon ──────────────────────────────────────────────────────────────
    'portland':        {'name': 'Portland',        'state': 'Oregon',     'x': -120200,'z': -38300},
    'eugene':          {'name': 'Eugene',          'state': 'Oregon',     'x': -121000,'z': -29000},
    'medford':         {'name': 'Medford',         'state': 'Oregon',     'x': -121000,'z': -22000},
    'bend':            {'name': 'Bend',            'state': 'Oregon',     'x': -117000,'z': -27000},
    'salem':           {'name': 'Salem',           'state': 'Oregon',     'x': -121000,'z': -34500},
    'coos_bay':        {'name': 'Coos Bay',        'state': 'Oregon',     'x': -124000,'z': -27000},
    'astoria':         {'name': 'Astoria',         'state': 'Oregon',     'x': -122500,'z': -43000},
    'klamath_falls':   {'name': 'Klamath Falls',   'state': 'Oregon',     'x': -118000,'z': -21000},
    'the_dalles':      {'name': 'The Dalles',      'state': 'Oregon',     'x': -116500,'z': -37000},
    'pendleton':       {'name': 'Pendleton',       'state': 'Oregon',     'x': -109000,'z': -42000},
    # ── Washington ──────────────────────────────────────────────────────────
    'seattle':         {'name': 'Seattle',         'state': 'Washington', 'x': -114900,'z': -53000},
    'tacoma':          {'name': 'Tacoma',          'state': 'Washington', 'x': -116000,'z': -51000},
    'olympia':         {'name': 'Olympia',         'state': 'Washington', 'x': -117000,'z': -49500},
    'spokane':         {'name': 'Spokane',         'state': 'Washington', 'x': -100000,'z': -55000},
    'yakima':          {'name': 'Yakima',          'state': 'Washington', 'x': -111000,'z': -47000},
    'aberdeen':        {'name': 'Aberdeen',        'state': 'Washington', 'x': -120500,'z': -46500},
    'bellingham':      {'name': 'Bellingham',      'state': 'Washington', 'x': -114000,'z': -60000},
    'wenatchee':       {'name': 'Wenatchee',       'state': 'Washington', 'x': -110500,'z': -52000},
    'ellensburg':      {'name': 'Ellensburg',      'state': 'Washington', 'x': -111000,'z': -49500},
    'kennewick':       {'name': 'Kennewick',       'state': 'Washington', 'x': -106000,'z': -45000},
    # ── Idaho ───────────────────────────────────────────────────────────────
    'boise':           {'name': 'Boise',           'state': 'Idaho',      'x': -91200, 'z': -45500},
    'twin_falls':      {'name': 'Twin Falls',      'state': 'Idaho',      'x': -79500, 'z': -37000},
    'pocatello':       {'name': 'Pocatello',       'state': 'Idaho',      'x': -71000, 'z': -38700},
    'idaho_falls':     {'name': 'Idaho Falls',     'state': 'Idaho',      'x': -73500, 'z': -42500},
    'coeur_dalene':    {'name': "Coeur d'Alene",   'state': 'Idaho',      'x': -103000,'z': -55000},
    'lewiston':        {'name': 'Lewiston',        'state': 'Idaho',      'x': -101000,'z': -51000},
    'burley':          {'name': 'Burley',          'state': 'Idaho',      'x': -77000, 'z': -35500},
    # ── Montana ─────────────────────────────────────────────────────────────
    'billings':        {'name': 'Billings',        'state': 'Montana',    'x': -53500, 'z': -65000},
    'missoula':        {'name': 'Missoula',        'state': 'Montana',    'x': -87000, 'z': -65000},
    'great_falls':     {'name': 'Great Falls',     'state': 'Montana',    'x': -65000, 'z': -73000},
    'butte':           {'name': 'Butte',           'state': 'Montana',    'x': -79500, 'z': -64500},
    'havre':           {'name': 'Havre',           'state': 'Montana',    'x': -63000, 'z': -79000},
    'miles_city':      {'name': 'Miles City',      'state': 'Montana',    'x': -33000, 'z': -72000},
    'glendive':        {'name': 'Glendive',        'state': 'Montana',    'x': -25000, 'z': -73000},
    'hardin':          {'name': 'Hardin',          'state': 'Montana',    'x': -48000, 'z': -68000},
    'kalispell':       {'name': 'Kalispell',       'state': 'Montana',    'x': -91000, 'z': -70000},
    'lewistown':       {'name': 'Lewistown',       'state': 'Montana',    'x': -60000, 'z': -69000},
    'wolf_point':      {'name': 'Wolf Point',      'state': 'Montana',    'x': -47000, 'z': -80000},
    'glasgow':         {'name': 'Glasgow',         'state': 'Montana',    'x': -47500, 'z': -82000},
    'sidney':          {'name': 'Sidney',          'state': 'Montana',    'x': -28000, 'z': -78000},
    'polson':          {'name': 'Polson',          'state': 'Montana',    'x': -88000, 'z': -68000},
    # ── Wyoming ─────────────────────────────────────────────────────────────
    'cheyenne':        {'name': 'Cheyenne',        'state': 'Wyoming',    'x': -13000, 'z': -13000},
    'casper':          {'name': 'Casper',          'state': 'Wyoming',    'x': -25000, 'z': -23000},
    'rock_springs':    {'name': 'Rock Springs',    'state': 'Wyoming',    'x': -36000, 'z': -21000},
    'laramie':         {'name': 'Laramie',         'state': 'Wyoming',    'x': -16000, 'z': -12000},
    # ── Kansas ──────────────────────────────────────────────────────────────
    'wichita':         {'name': 'Wichita',         'state': 'Kansas',     'x':  22000, 'z':   7000},
    'dodge_city':      {'name': 'Dodge City',      'state': 'Kansas',     'x':  14000, 'z':   7000},
}


# ── SII city block parser ─────────────────────────────────────────────────────

_CITY_BLOCK_RE = re.compile(
    r'city_data\s*:\s*city\.(\w+)\s*\{(.*?)\}',
    re.DOTALL | re.IGNORECASE,
)


def _parse_city_block(city_id: str, block: str) -> dict | None:
    name_m    = re.search(r'city_name\s*:\s*"([^"]+)"', block)
    x_m       = re.search(r'\bx\s*:\s*(-?\d+)', block)
    y_m       = re.search(r'\by\s*:\s*(-?\d+)', block)
    country_m = re.search(r'country\s*:\s*(?:country\.)?(\w+)', block)

    if not name_m:
        return None

    x = int(x_m.group(1)) if x_m else None
    z = int(y_m.group(1)) if y_m else None
    raw_state = (country_m.group(1) if country_m else '').lower()
    state = _STATE_NAMES.get(raw_state, raw_state.replace('_', ' ').title())

    return {'name': name_m.group(1), 'state': state, 'x': x, 'z': z}


# ── ZIP-based mod scanner ────────────────────────────────────────────────────

def _scan_zip_mod(scs_path: str) -> dict:
    """
    Read unencrypted ZIP-format .scs mod files for def/city/*.sii entries.
    Returns {city_id: city_dict} for cities that have x/z coordinates.
    """
    found: dict = {}
    try:
        with zipfile.ZipFile(scs_path) as zf:
            city_files = [n for n in zf.namelist()
                          if n.startswith('def/city/') and n.endswith('.sii')]
            for name in city_files:
                try:
                    raw = zf.read(name).decode('utf-8', errors='replace')
                except (RuntimeError, Exception):
                    continue  # encrypted or unreadable
                for m in _CITY_BLOCK_RE.finditer(raw):
                    city_id = m.group(1).lower()
                    entry = _parse_city_block(city_id, m.group(2))
                    if entry and entry['x'] is not None and entry['z'] is not None:
                        found[city_id] = entry
    except (zipfile.BadZipFile, Exception):
        pass
    return found


# ── Public API ────────────────────────────────────────────────────────────────

def build_city_db(ats_install_path: str | None = None) -> dict:
    """
    Build and return the city database.

    ATS base-game cities come from a hardcoded table (game ships them in
    SCS HashFS archives that require a specialised reader).  ZIP-based mod
    .scs files are scanned for additional city definitions.

    Logs city counts on startup.
    """
    db: dict = dict(_ATS_BASE_CITIES)
    base_count = len(db)

    # ── Mod scan ─────────────────────────────────────────────────────────────
    docs = os.path.expandvars('%USERPROFILE%')
    mod_dirs = [
        os.path.join(docs, 'Documents', 'American Truck Simulator', 'mod'),
        os.path.join(docs, 'Documents', 'Euro Truck Simulator 2', 'mod'),
    ]
    mod_count = 0
    scanned_mods = 0

    for mod_dir in mod_dirs:
        if not os.path.isdir(mod_dir):
            continue
        for fname in os.listdir(mod_dir):
            if not fname.lower().endswith('.scs'):
                continue
            fpath = os.path.join(mod_dir, fname)
            try:
                with open(fpath, 'rb') as f:
                    magic = f.read(2)
                if magic != b'PK':
                    continue  # HashFS — skip (no CityHash reader available)
            except OSError:
                continue
            scanned_mods += 1
            cities = _scan_zip_mod(fpath)
            for city_id, entry in cities.items():
                if city_id not in db:
                    db[city_id] = entry
                    mod_count += 1

    _log.info(
        f"city_db: {base_count} base-game cities (hardcoded), "
        f"{mod_count} from mods ({scanned_mods} ZIP mods scanned), "
        f"{len(db)} total"
    )
    return db


def get_nearest_city(pos_x: float, pos_z: float, city_db: dict) -> str:
    """
    Return 'City Name, State' of the city closest to (pos_x, pos_z).
    Only considers cities that have valid x/z coordinates.
    Returns 'unknown' if city_db is empty or no city has coordinates.
    """
    if not city_db:
        return 'unknown'

    best_label = 'unknown'
    best_dist2 = float('inf')

    for city in city_db.values():
        cx = city.get('x')
        cz = city.get('z')
        if cx is None or cz is None:
            continue
        d2 = (pos_x - cx) ** 2 + (pos_z - cz) ** 2
        if d2 < best_dist2:
            best_dist2 = d2
            state = city.get('state', '')
            name  = city.get('name', '?')
            best_label = f"{name}, {state}" if state else name

    return best_label
