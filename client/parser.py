import re

# ── Trailer body-type helpers ─────────────────────────────────────────────────

# Keywords to look for in a job's trailer_definition string to match a body_type.
# Values are lists; any match counts.
_BODY_TYPE_KEYWORDS: dict[str, list[str]] = {
    'rogerspm2':    ['rogers.pm'],
    'rogersflip':   ['rogers.4axleflip', 'rogersflip', 'rogers.flip'],
    'lowboy':       ['lowboy'],
    'flatbed':      ['flatbed', 'fontaine', 'manac.flatbed'],
    'dropdeck':     ['dropdeck'],
    'log':          ['.log.', 'manac.log', '_log_'],
    'refrigerated': ['reefer', 'utility.2000', 'refriger'],
    'hopper':       ['hopper'],
    'horse':        ['horse'],
    'chaul':        ['chaul'],
}

# Human-readable label for each body_type (shown to Claude)
BODY_TYPE_LABELS: dict[str, str] = {
    'rogerspm2':    'Rogers PM heavy-haul',
    'rogersflip':   'Rogers flip-axle heavy-haul',
    'lowboy':       'Lowboy heavy-equipment',
    'flatbed':      'Flatbed',
    'dropdeck':     'Drop-deck',
    'log':          'Logging',
    'refrigerated': 'Reefer',
    'hopper':       'Hopper',
    'horse':        'Horse trailer',
    'chaul':        'Chassis hauler',
}


def _def_str_to_body_type(s: str) -> str | None:
    """Infer body_type from a named trailer_definition string."""
    d = s.lower()
    if 'rogers.pm' in d:                        return 'rogerspm2'
    if 'rogersflip' in d or 'rogers.4axle' in d: return 'rogersflip'
    if 'lowboy' in d:                            return 'lowboy'
    if 'dropdeck' in d:                          return 'dropdeck'
    if 'flatbed' in d or 'fontaine' in d or 'manac.flatbed' in d: return 'flatbed'
    if '.log.' in d or 'manac.log' in d:         return 'log'
    if 'reefer' in d or 'utility' in d or 'refriger' in d: return 'refrigerated'
    if 'hopper' in d:                            return 'hopper'
    if 'horse' in d:                             return 'horse'
    if 'chaul' in d:                             return 'chaul'
    return None


def _parse_def_body_types(content: str) -> dict[str, str]:
    """
    Build a map: nameless def ID → body_type string.
    Trailer def blocks are flat (no nested braces) so [^}] works fine.
    """
    result: dict[str, str] = {}
    pat = re.compile(r':\s*(_nameless\.\S+)\s*\{([^}]{0,800})\}', re.DOTALL)
    for m in pat.finditer(content):
        bt_m = re.search(r'\bbody_type\s*:\s*(\w+)', m.group(2))
        if bt_m:
            result[m.group(1)] = bt_m.group(1)
    return result


def job_matches_body_type(trailer_def_str: str, body_type: str) -> bool:
    """Return True if a job's trailer_definition string is compatible with body_type."""
    if not trailer_def_str or not body_type:
        return False
    d = trailer_def_str.lower()
    return any(kw in d for kw in _BODY_TYPE_KEYWORDS.get(body_type, [body_type.lower()]))


# ── Top-level parser ──────────────────────────────────────────────────────────

def parse_sii(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    owned_trailers = parse_owned_trailers(content)

    result = {
        'finances':       parse_finances(content),
        'player':         parse_player(content),
        'drivers':        parse_drivers(content),
        'jobs':           parse_jobs(content),         # completed job history
        'freight_market': [],                           # populated by client.py via parse_freight_market()
        'trailer':        parse_trailer(content, owned_trailers),  # currently attached trailer + body_type
        'owned_trailers': [{'body_type': t['body_type'],
                            'label':     BODY_TYPE_LABELS.get(t['body_type'], t['body_type'])}
                           for t in owned_trailers if t['body_type']],
    }

    return result


def parse_finances(content):
    import logging
    _log = logging.getLogger('parser')

    finances = {}

    money = re.search(r'money_account:\s*(-?\d+)', content)
    raw_money = money.group(1) if money else None
    _log.info(f"parse_finances: raw money_account={raw_money!r}")
    finances['money'] = int(raw_money) if raw_money is not None else 0

    loan_limit = re.search(r'loan_limit:\s*(\d+)', content)
    finances['loan_limit'] = int(loan_limit.group(1)) if loan_limit else 0

    loan_blocks = re.findall(
        r'bank_loan\s*:.*?\{(.*?)\}', content, re.DOTALL
    )
    loans = []
    for block in loan_blocks:
        amount = re.search(r'amount:\s*(\d+)', block)
        original = re.search(r'original_amount:\s*(\d+)', block)
        duration = re.search(r'duration:\s*(\d+)', block)
        if amount and original:
            loans.append({
                'amount': int(amount.group(1)),
                'original_amount': int(original.group(1)),
                'duration': int(duration.group(1)) if duration else 0,
            })
    finances['loans'] = loans
    finances['total_debt'] = sum(l['amount'] for l in loans)

    return finances


def parse_trailer(content, owned_trailers: list | None = None):
    """
    Extract current trailer info and body_type from the save file.

    owned_trailers — pre-parsed list from parse_owned_trailers(); if supplied,
    the current trailer's body_type is looked up from it.
    """
    trailer: dict = {}

    # Currently assigned trailer ID (from economy block)
    my_trailer_m = re.search(r'\b(?:my_trailer|assigned_trailer)\s*:\s*(\S+)', content)
    trailer['id'] = my_trailer_m.group(1) if my_trailer_m else None

    # Active job cargo / destination from job_info block
    job_info_m = re.search(r'job_info\s*:.*?\{([^}]*)\}', content, re.DOTALL)
    if job_info_m:
        block    = job_info_m.group(1)
        cargo_m  = re.search(r'\bcargo\s*:\s*"?([^"\n]+)"?', block)
        target_m = re.search(r'target(?:_company)?\s*:\s*"?([^"\n]+)"?', block)
        trailer['active_cargo']       = cargo_m.group(1).strip()  if cargo_m  else None
        trailer['active_destination'] = target_m.group(1).strip() if target_m else None

    # Resolve body_type from owned_trailers if available
    if owned_trailers and trailer.get('id'):
        cur_id = trailer['id']
        match  = next((t for t in owned_trailers if t['trailer_id'] == cur_id), None)
        trailer['body_type'] = match['body_type'] if match else None
    else:
        trailer['body_type'] = None

    return trailer


def parse_owned_trailers(content: str) -> list[dict]:
    """
    Return a list of {trailer_id, def_id, body_type} for every owned trailer.

    ATS save format:
        trailer : _nameless.xxx {
            trailer_definition: _nameless.yyy   ← points to a flat def block
            ...                                   (may have nested {}, so we only
        }                                          scan the first 500 chars)

    The def block (usually a flat nameless block) contains:
        body_type: lowboy   ← this is what we want

    For direct named def strings (e.g. trailer_def.scs.lowboy.triple_2_2_2.wood)
    we infer the body_type from the string itself.
    """
    import logging
    _log = logging.getLogger('parser')

    # Build map: nameless def ID → body_type (from flat def blocks)
    def_body_map = _parse_def_body_types(content)

    trailers: list[dict] = []
    header_pat = re.compile(r'^trailer\s*:\s*(_nameless\.\S+)\s*\{', re.M)

    for m in header_pat.finditer(content):
        trailer_id = m.group(1)
        # Scan only first 500 chars after the opening brace to get trailer_definition
        # before any nested sub-blocks appear
        snippet = content[m.end(): m.end() + 500]
        td_m = re.search(r'trailer_definition\s*:\s*(\S+)', snippet)
        if not td_m:
            continue
        def_id = td_m.group(1).strip()
        if def_id in ('null', 'nil', ''):
            continue

        if def_id.startswith('_nameless'):
            body_type = def_body_map.get(def_id)
        else:
            body_type = _def_str_to_body_type(def_id)

        trailers.append({
            'trailer_id': trailer_id,
            'def_id':     def_id,
            'body_type':  body_type,  # may be None if unknown type
        })

    known = [t for t in trailers if t['body_type']]
    _log.info(f"owned trailers: {len(trailers)} found, {len(known)} with known body_type "
              f"({[t['body_type'] for t in known]})")
    return trailers


def parse_player(content):
    player = {}

    import logging
    _log = logging.getLogger('parser')

    # Search globally for in_job — it lives inside the large economy block but
    # that block contains many nested sub-blocks so we don't try to bound it.
    # SII files store booleans as the strings 'true'/'false'; also handle 1/0.
    in_job_m = re.search(r'\bin_job\s*:\s*(true|false|1|0)\b', content, re.IGNORECASE)
    if in_job_m:
        player['in_job'] = in_job_m.group(1).lower() in ('true', '1')
    else:
        player['in_job'] = False  # field absent → not in job

    # Search globally for current_city — present in the economy block near the top
    city_m = re.search(r'\bcurrent_city\s*:\s*"?([^"\s\n]+)"?', content)
    player['current_city'] = city_m.group(1).strip() if city_m else None
    _log.info(f"parse_player: in_job={player['in_job']!r}, current_city={player['current_city']!r}")

    # Economy block — grab the stats fields we care about.
    # NOTE: the economy block is huge (contains nested blocks) so the regex may
    # only capture the first shallow fragment; that's fine for these scalar fields
    # which appear near the top of the block.
    economy_block = re.search(r'economy\s*:.*?\{(.*?)\n\}', content, re.DOTALL)
    if economy_block:
        block = economy_block.group(1)
        xp = re.search(r'experience_points:\s*(\d+)', block)
        dist = re.search(r'total_distance:\s*(\d+)', block)
        game_time = re.search(r'game_time:\s*(\d+)', block)
        player['experience_points'] = int(xp.group(1)) if xp else 0
        player['total_distance_km'] = int(dist.group(1)) if dist else 0
        player['game_time_minutes'] = int(game_time.group(1)) if game_time else 0

    # Also surface the job_info target so assistant.py can log it for diagnosis.
    # We intentionally do NOT use this for active-job detection — the field
    # persists after job completion and is therefore unreliable.
    job_info_m = re.search(r'job_info\s*:.*?\{([^}]*)\}', content, re.DOTALL)
    if job_info_m:
        jblock = job_info_m.group(1)
        target_m = re.search(r'target\s*:\s*"?([^"\n]+)"?', jblock)
        raw_target = target_m.group(1).strip() if target_m else None
        player['job_info_target'] = (
            raw_target if raw_target and raw_target not in ('null', 'nil', '""', '"')
            else None
        )
        # Grab every top-level scalar in the job_info block for full visibility
        for field in ('cargo', 'urgency', 'planned_distance_km', 'is_special_job',
                      'already_paid', 'started_at', 'end_time', 'pay'):
            fm = re.search(rf'\b{field}\s*:\s*([^\n]+)', jblock)
            if fm:
                val = fm.group(1).strip().strip('"')
                if val not in ('null', 'nil', '""'):
                    player[f'job_info_{field}'] = val
    else:
        player['job_info_target'] = None

    return player


def parse_drivers(content):
    drivers = []

    driver_blocks = re.findall(
        r'driver_ai\s*:\s*(driver\.\d+)\s*\{(.*?)\n\}',
        content, re.DOTALL
    )

    for driver_id, block in driver_blocks:
        driver = {'id': driver_id}

        for field in ['adr', 'long_dist', 'heavy', 'fragile', 'urgent', 'mechanical']:
            match = re.search(rf'{field}:\s*(\d+)', block)
            driver[field] = int(match.group(1)) if match else 0

        for field in ['hometown', 'current_city']:
            match = re.search(rf'{field}:\s*(\S+)', block)
            driver[field] = match.group(1) if match else ''

        xp = re.search(r'experience_points:\s*(\d+)', block)
        driver['experience_points'] = int(xp.group(1)) if xp else 0

        state = re.search(r'state:\s*(\d+)', block)
        driver['state'] = int(state.group(1)) if state else 0

        # Only include drivers that have a valid hometown (actually hired)
        hometown = driver.get('hometown', '')
        if hometown and hometown not in ('', '--', '""', '"'):
            drivers.append(driver)

    # Sort by XP descending by default
    drivers.sort(key=lambda d: d['experience_points'], reverse=True)

    return drivers


def parse_jobs(content):
    jobs = []

    job_blocks = re.findall(
        r'profit_log_entry\s*:.*?\{(.*?)\}', content, re.DOTALL
    )

    for block in job_blocks:
        revenue = re.search(r'revenue:\s*(-?\d+)', block)
        wage = re.search(r'wage:\s*(-?\d+)', block)
        fuel = re.search(r'fuel:\s*(-?\d+)', block)
        maintenance = re.search(r'maintenance:\s*(-?\d+)', block)
        distance = re.search(r'distance:\s*(-?\d+)', block)
        cargo = re.search(r'cargo:\s*"?([^"\n]+)"?', block)
        source_city = re.search(r'source_city:\s*"?([^"\n]+)"?', block)
        dest_city = re.search(r'destination_city:\s*"?([^"\n]+)"?', block)
        source_company = re.search(r'source_company:\s*"?([^"\n]+)"?', block)
        dest_company = re.search(r'destination_company:\s*"?([^"\n]+)"?', block)
        timestamp = re.search(r'timestamp_day:\s*(\d+)', block)

        revenue_val = int(revenue.group(1)) if revenue else 0
        if revenue_val == 0:
            continue

        market_m = re.search(r'market(?:_type)?\s*:\s*"?([^"\n]+)"?', block)
        market = market_m.group(1).strip() if market_m else 'cargo_market'

        jobs.append({
            'revenue':             revenue_val,
            'wage':                int(wage.group(1)) if wage else 0,
            'fuel':                int(fuel.group(1)) if fuel else 0,
            'maintenance':         int(maintenance.group(1)) if maintenance else 0,
            'distance_km':         int(distance.group(1)) if distance else 0,
            'cargo':               cargo.group(1).strip() if cargo else '',
            'source_city':         source_city.group(1).strip() if source_city else '',
            'destination_city':    dest_city.group(1).strip() if dest_city else '',
            'source_company':      source_company.group(1).strip() if source_company else '',
            'destination_company': dest_company.group(1).strip() if dest_company else '',
            'day':                 int(timestamp.group(1)) if timestamp else 0,
            'market':              market,
        })

    jobs.sort(key=lambda j: j['day'], reverse=True)
    return jobs


def parse_freight_market(content):
    """
    Parse currently available freight market job offers from ATS/ETS2 save files.

    Two-pass approach:
      Pass 1 — build a map of {job_offer_data_id: (src_company, src_city, discovered)}
               from company.volatile blocks.  Only discovered companies (ones the
               player has visited) are included; undiscovered ones have pre-seeded
               placeholder slots that are not meaningful for dispatch.
      Pass 2 — parse each job_offer_data block, cross-reference game_time from the
               economy block, and keep only offers whose expiration_time > game_time.

    Revenue is not stored in save files — estimated at ~$32/km (avg from profit logs,
    +20% for urgent jobs).
    """
    import logging
    _log = logging.getLogger('parser')

    # Extract current game time so we can filter out expired offers
    game_time_m = re.search(r'\bgame_time:\s*(\d+)', content)
    game_time = int(game_time_m.group(1)) if game_time_m else 0

    # Pass 1: map job_offer_data ID -> (src_company, src_city)
    # Only index offers from discovered companies — the game pre-fills offer slots for
    # every company on the map (4 000+), but only ~1 000 have been visited by the player.
    # Offers from undiscovered companies are not shown in the in-game freight market.
    source_map = {}
    company_pattern = re.compile(
        r'company\s*:\s*company\.volatile\.(\w+)\.(\w+)\s*\{([^}]*)\}',
        re.DOTALL
    )
    offer_ref_pattern = re.compile(r'job_offer\[\d+\]\s*:\s*(_nameless\.\S+)')

    for m in company_pattern.finditer(content):
        company_name, city_name, block = m.group(1), m.group(2), m.group(3)
        if 'discovered: true' not in block:
            continue
        for offer_id in offer_ref_pattern.findall(block):
            source_map[offer_id] = (company_name, city_name)

    _log.info(f"freight market: {len(source_map)} offer slots from discovered companies (game_time={game_time})")
    print(f'[Parser] Freight market: {len(source_map)} slots from discovered companies, game_time={game_time}')

    def _clean(s):
        return s.replace('_', ' ').title()

    # Pass 2: parse job_offer_data blocks — keep only non-expired current offers
    job_data_pattern = re.compile(
        r'job_offer_data\s*:\s*(_nameless\.\S+)\s*\{([^}]*)\}',
        re.DOTALL
    )

    jobs = []
    for m in job_data_pattern.finditer(content):
        offer_id = m.group(1)
        block = m.group(2)

        src = source_map.get(offer_id)
        if not src:
            continue

        src_company, src_city = src

        # Must have a real destination
        target_m = re.search(r'target\s*:\s*"([^"]+)"', block)
        if not target_m:
            continue
        target_raw = target_m.group(1).strip()
        if not target_raw or target_raw in ('null', 'nil'):
            continue

        dot_idx = target_raw.find('.')
        if dot_idx < 0:
            continue
        dest_company = target_raw[:dot_idx]
        dest_city = target_raw[dot_idx + 1:]

        # Must have a positive distance
        dist_m = re.search(r'shortest_distance_km\s*:\s*(\d+)', block)
        dist = int(dist_m.group(1)) if dist_m else 0
        if dist <= 0:
            continue

        # Must not have expired — nil means unset (skip), numeric must be > game_time
        expiry_m = re.search(r'expiration_time\s*:\s*(\S+)', block)
        if not expiry_m:
            continue
        expiry_raw = expiry_m.group(1).strip()
        if expiry_raw in ('nil', 'null', ''):
            continue
        expiry = int(expiry_raw)
        if game_time > 0 and expiry <= game_time:
            continue

        cargo_m = re.search(r'\bcargo\s*:\s*(\S+)', block)
        cargo_raw = cargo_m.group(1) if cargo_m else ''
        if cargo_raw in ('null', 'nil', '""'):
            cargo_raw = ''
        else:
            cargo_raw = re.sub(r'^cargo\.', '', cargo_raw)

        urgency_m = re.search(r'\burgency\s*:\s*(\d+)', block)
        urgency = int(urgency_m.group(1)) if urgency_m else 0

        revenue = int(dist * 32 * (1.2 if urgency > 0 else 1.0))

        # trailer_definition tells us what body type is needed for this job
        tdef_m = re.search(r'trailer_definition\s*:\s*(\S+)', block)
        tdef_str  = tdef_m.group(1) if tdef_m else ''
        body_type = _def_str_to_body_type(tdef_str) if tdef_str not in ('null', 'nil', '') else None

        jobs.append({
            'source_city':         _clean(src_city),
            'destination_city':    _clean(dest_city),
            'source_company':      _clean(src_company),
            'destination_company': _clean(dest_company),
            'cargo':               _clean(cargo_raw) if cargo_raw else '',
            'distance_km':         dist,
            'revenue':             revenue,
            'urgency':             urgency,
            'expiration_time':     expiry,
            'market':              'freight_market',
            'trailer_def':         tdef_str,    # raw def string for exact matching
            'body_type':           body_type,   # normalised body type for filtering
        })

    print(f'[Parser] Freight market: {len(jobs)} current offers (from discovered companies, not expired)')
    _log.info(f"freight market: {len(jobs)} current offers")
    jobs.sort(key=lambda j: j['revenue'], reverse=True)
    return jobs


if __name__ == '__main__':
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else 'game-decoded.sii'
    data = parse_sii(path)

    print(f"Money: ${data['finances']['money']:,}")
    print(f"Total debt: ${data['finances']['total_debt']:,}")
    print(f"Loans: {len(data['finances']['loans'])}")
    print(f"XP: {data['player']['experience_points']:,}")
    print(f"Distance: {data['player']['total_distance_km']:,} km")
    print(f"Drivers (hired only): {len(data['drivers'])}")
    print(f"Jobs: {len(data['jobs'])}")
    if data['jobs']:
        print(f"\nLast 3 jobs:")
        for job in data['jobs'][:3]:
            print(f"  {job['source_city']} → {job['destination_city']} | ${job['revenue']:,} | {job['cargo']}")
    print(f"Freight market: {len(data['freight_market'])} jobs available")
    if data['freight_market']:
        print(f"\nTop 3 market jobs:")
        for job in data['freight_market'][:3]:
            print(f"  {job['source_city']} → {job['destination_city']} | ${job['revenue']:,} | {job['cargo']} | {job['distance_km']} km")
