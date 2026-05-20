import re

def parse_sii(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    result = {
        'finances': parse_finances(content),
        'player': parse_player(content),
        'drivers': parse_drivers(content),
        'jobs': parse_jobs(content),
        'freight_market': parse_freight_market(content),
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

        jobs.append({
            'revenue': revenue_val,
            'wage': int(wage.group(1)) if wage else 0,
            'fuel': int(fuel.group(1)) if fuel else 0,
            'maintenance': int(maintenance.group(1)) if maintenance else 0,
            'distance_km': int(distance.group(1)) if distance else 0,
            'cargo': cargo.group(1).strip() if cargo else '',
            'source_city': source_city.group(1).strip() if source_city else '',
            'destination_city': dest_city.group(1).strip() if dest_city else '',
            'source_company': source_company.group(1).strip() if source_company else '',
            'destination_company': dest_company.group(1).strip() if dest_company else '',
            'day': int(timestamp.group(1)) if timestamp else 0,
        })

    jobs.sort(key=lambda j: j['day'], reverse=True)
    return jobs


def parse_freight_market(content):
    """
    Parse available job offers from the ATS/ETS2 save file.
    Returns a list of dicts sorted by revenue descending.

    ATS/ETS2 save format (post-SII_Decrypt):
      economy.job_offer.N : .job_offer {
          source_city: los_angeles
          target_city: phoenix
          cargo: cargo.flat_boards
          revenue: 14500
          distance: 372
          ...
      }

    The TYPE is `.job_offer` (after the colon), not the block name.
    The legacy inline form `job_offer : token { ... }` is also attempted as a fallback.
    """
    import logging
    _log = logging.getLogger('parser')

    # Primary: match blocks whose TYPE is .job_offer / job_offer
    # Format: "some.name : .job_offer {\n  fields\n}"
    blocks = re.findall(
        r'[^\n]+:\s*\.?job_offer\s*\{(.*?)(?:\r?\n)\}',
        content, re.DOTALL
    )

    # Fallback: some game versions or mods use inline syntax
    if not blocks:
        blocks = re.findall(
            r'job_offer\s*:\s*\S+\s*\{([^}]+)\}',
            content, re.DOTALL
        )

    _log.info(f"freight market: {len(blocks)} raw job_offer blocks found in save")
    print(f'[Parser] Freight market: {len(blocks)} job_offer blocks found in save')

    def _clean(s, is_city=False):
        """Strip internal namespace prefixes and convert to Title Case."""
        s = s.strip().strip('"')
        # "company.weigh_station_ats.los_angeles" → "Los Angeles"
        # "cargo.coil_steel" → "Coil Steel"
        # "city.los_angeles" → "Los Angeles"
        parts = s.split('.')
        name = parts[-1]
        return name.replace('_', ' ').title()

    for block in blocks:
        # City fields: ATS uses source_city / target_city;
        # some versions use destination_city instead of target_city.
        src_city_m = re.search(r'source_city\s*:\s*(\S+)', block)
        tgt_city_m = re.search(r'(?:target_city|destination_city)\s*:\s*(\S+)', block)
        src_co_m   = re.search(r'source_company\s*:\s*(\S+)', block)
        tgt_co_m   = re.search(r'(?:target_company|destination_company)\s*:\s*(\S+)', block)
        cargo_m    = re.search(r'\bcargo\s*:\s*(\S+)', block)
        # ATS uses "distance" (km); some versions use "shortest_path_distance"
        dist_m     = re.search(r'(?:\bdistance\b|shortest_path_distance)\s*:\s*(\d+)', block)
        rev_m      = re.search(r'\brevenue\s*:\s*(\d+)', block)
        mass_m     = re.search(r'cargo_mass\s*:\s*([\d.]+)', block)
        urgency_m  = re.search(r'\burgency\s*:\s*(\d+)', block)
        expiry_m   = re.search(r'expiration_time\s*:\s*(\d+)', block)

        if not rev_m:
            continue
        rev = int(rev_m.group(1))
        if rev <= 0:
            continue

        # City: prefer explicit city field, fall back to last dotted segment of company token
        def _city_from_token(city_m, company_m):
            if city_m:
                return _clean(city_m.group(1))
            if company_m:
                # e.g. "company.carrier_station.los_angeles" → "Los Angeles"
                return _clean(company_m.group(1))
            return ''

        src_city = _city_from_token(src_city_m, src_co_m)
        tgt_city = _city_from_token(tgt_city_m, tgt_co_m)

        if not src_city or not tgt_city:
            continue

        jobs.append({
            'source_city':         src_city,
            'destination_city':    tgt_city,
            'source_company':      _clean(src_co_m.group(1)) if src_co_m else '',
            'destination_company': _clean(tgt_co_m.group(1)) if tgt_co_m else '',
            'cargo':               _clean(cargo_m.group(1)) if cargo_m else '',
            'distance_km':         int(dist_m.group(1)) if dist_m else 0,
            'revenue':             rev,
            'cargo_mass_t':        float(mass_m.group(1)) if mass_m else 0.0,
            'urgency':             int(urgency_m.group(1)) if urgency_m else 0,
            'expiration_time':     int(expiry_m.group(1)) if expiry_m else 0,
        })

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
