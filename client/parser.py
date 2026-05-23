import re

def parse_sii(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    result = {
        'finances':       parse_finances(content),
        'player':         parse_player(content),
        'drivers':        parse_drivers(content),
        'jobs':           parse_jobs(content),
        'freight_market': [],
        'trailer':        parse_trailer(content),
    }

    return result


def parse_finances(content):
    finances = {}

    money = re.search(r'money_account:\s*(-?\d+)', content)
    finances['money'] = int(money.group(1)) if money else 0

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


def parse_trailer(content):
    """Extract current trailer type and active cargo from the save file."""
    trailer = {}

    # Trailer reference in the economy block
    my_trailer_m = re.search(r'\bmy_trailer\s*:\s*(\S+)', content)
    trailer['id'] = my_trailer_m.group(1) if my_trailer_m else None

    # Active job cargo / destination from job_info block
    job_info_m = re.search(r'job_info\s*:.*?\{([^}]*)\}', content, re.DOTALL)
    if job_info_m:
        block = job_info_m.group(1)
        cargo_m  = re.search(r'\bcargo\s*:\s*"?([^"\n]+)"?', block)
        target_m = re.search(r'target(?:_company)?\s*:\s*"?([^"\n]+)"?', block)
        trailer['active_cargo']       = cargo_m.group(1).strip() if cargo_m else None
        trailer['active_destination'] = target_m.group(1).strip() if target_m else None

    # Trailer definition type (e.g. "trailer.flatbed", "trailer.curtain")
    trailer_def_m = re.search(
        r'\btrailer\b\s*:\s*trailer\.\S+\s*\{([^}]*)\}', content, re.DOTALL
    )
    if trailer_def_m:
        td_m = re.search(r'\btrailer_def\s*:\s*"?([^"\n]+)"?', trailer_def_m.group(1))
        trailer['type'] = td_m.group(1).strip() if td_m else None

    return trailer


def parse_player(content):
    player = {}

    economy_block = re.search(r'economy\s*:.*?\{(.*?)\n\}', content, re.DOTALL)
    if economy_block:
        block = economy_block.group(1)
        xp = re.search(r'experience_points:\s*(\d+)', block)
        dist = re.search(r'total_distance:\s*(\d+)', block)
        game_time = re.search(r'game_time:\s*(\d+)', block)
        player['experience_points'] = int(xp.group(1)) if xp else 0
        player['total_distance_km'] = int(dist.group(1)) if dist else 0
        player['game_time_minutes'] = int(game_time.group(1)) if game_time else 0

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
