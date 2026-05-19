from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from flask_cors import CORS
from models import db, Player, Snapshot, VTC, Job
from datetime import datetime
from functools import wraps
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__, template_folder='../templates', static_folder='../static')
CORS(app)

_db_url = os.getenv('DATABASE_URL', 'sqlite:///dispatch.db')
# Railway provides postgres:// but SQLAlchemy requires postgresql://
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.getenv('SECRET_KEY', 'dev_secret')

db.init_app(app)

from auth import auth_bp
app.register_blueprint(auth_bp)

with app.app_context():
    db.create_all()


# In-memory telemetry store — latest data per player
telemetry_store = {}


def require_discord(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'discord_id' not in session:
            return redirect(url_for('auth.login', next=request.path))
        return f(*args, **kwargs)
    return decorated


def _get_or_create_player(discord_id, discord_username):
    player = Player.query.filter_by(discord_id=discord_id).first()
    if not player:
        player = Player(discord_id=discord_id, discord_username=discord_username)
        db.session.add(player)
        db.session.flush()
    else:
        player.last_seen = datetime.utcnow()
        player.discord_username = discord_username
    return player


def _latest_snapshot(player_id):
    return Snapshot.query.filter_by(player_id=player_id)\
        .order_by(Snapshot.captured_at.desc()).first()


def _detect_and_log_jobs(player_id, snapshot_jobs):
    """
    Diff the snapshot's full job list against the Job table.
    Insert any jobs whose (game_day, revenue, source_city, destination_city)
    is not already recorded. Returns count of newly inserted jobs.
    """
    if not snapshot_jobs:
        return 0

    # Load all existing keys for this player in one query
    existing_rows = db.session.query(
        Job.game_day, Job.revenue, Job.source_city, Job.destination_city
    ).filter_by(player_id=player_id).all()
    existing_keys = {(r.game_day, r.revenue, r.source_city, r.destination_city)
                     for r in existing_rows}

    new_count = 0
    for j in snapshot_jobs:
        src = (j.get('source_city') or '').strip()
        dst = (j.get('destination_city') or '').strip()
        rev = j.get('revenue', 0)
        day = j.get('day', 0)

        if not src or not dst or not rev:
            continue

        key = (day, rev, src, dst)
        if key in existing_keys:
            continue

        db.session.add(Job(
            player_id=player_id,
            game_day=day,
            source_city=src,
            destination_city=dst,
            source_company=(j.get('source_company') or '').strip(),
            destination_company=(j.get('destination_company') or '').strip(),
            cargo=(j.get('cargo') or '').strip(),
            distance_km=j.get('distance_km', 0),
            revenue=rev,
            wage=j.get('wage', 0),
            fuel_cost=j.get('fuel', 0),
            maintenance_cost=j.get('maintenance', 0),
        ))
        existing_keys.add(key)  # guard against duplicate entries in same batch
        new_count += 1

    return new_count


def _job_to_dict(job):
    return {
        'id': job.id,
        'logged_at': job.logged_at.isoformat(),
        'game_day': job.game_day,
        'source_city': job.source_city,
        'destination_city': job.destination_city,
        'source_company': job.source_company,
        'destination_company': job.destination_company,
        'cargo': job.cargo,
        'distance_km': job.distance_km,
        'revenue': job.revenue,
        'wage': job.wage,
        'fuel_cost': job.fuel_cost,
        'maintenance_cost': job.maintenance_cost,
        'player_id': job.player_id,
    }


# ─── SIDEBAR CONTEXT PROCESSOR ─────────────────────────────────

@app.context_processor
def inject_sidebar():
    if 'discord_id' not in session:
        return {}
    discord_id = session['discord_id']
    player = Player.query.filter_by(discord_id=discord_id).first()
    vtc_obj = VTC.query.get(player.vtc_id) if player and player.vtc_id else None
    xp = 0
    if player:
        snap = _latest_snapshot(player.id)
        if snap:
            xp = snap.experience_points or 0
    return {'_sb_player': player, '_sb_vtc': vtc_obj, '_sb_xp': xp}


# ─── LEADERBOARD HELPER ─────────────────────────────────────────

def _build_leaderboard(player_ids=None, sort='distance', limit=100):
    """
    Return ranked leaderboard rows.
    player_ids=None means global; pass a list to scope to a VTC.
    sort: 'distance' | 'jobs' | 'earnings'
    """
    from sqlalchemy import func, desc

    # Subquery: latest snapshot captured_at per player
    latest_sub = db.session.query(
        Snapshot.player_id,
        func.max(Snapshot.captured_at).label('max_at'),
    ).group_by(Snapshot.player_id).subquery('latest_sub')

    # Subquery: job aggregates per player
    job_sub = db.session.query(
        Job.player_id,
        func.count(Job.id).label('job_count'),
        func.coalesce(func.sum(Job.revenue), 0).label('total_revenue'),
    ).group_by(Job.player_id).subquery('job_sub')

    q = db.session.query(
        Player.id,
        Player.discord_id,
        Player.discord_username,
        Player.vtc_id,
        Snapshot.total_distance_km,
        Snapshot.experience_points,
        func.coalesce(job_sub.c.job_count, 0).label('job_count'),
        func.coalesce(job_sub.c.total_revenue, 0).label('total_revenue'),
    ).join(latest_sub, Player.id == latest_sub.c.player_id)\
     .join(Snapshot, db.and_(
         Snapshot.player_id == Player.id,
         Snapshot.captured_at == latest_sub.c.max_at,
     ))\
     .outerjoin(job_sub, Player.id == job_sub.c.player_id)

    if player_ids is not None:
        q = q.filter(Player.id.in_(player_ids))

    if sort == 'jobs':
        q = q.order_by(desc('job_count'), desc(Snapshot.total_distance_km))
    elif sort == 'earnings':
        q = q.order_by(desc('total_revenue'), desc(Snapshot.total_distance_km))
    else:
        q = q.order_by(desc(Snapshot.total_distance_km))

    q = q.limit(limit)

    vtc_lookup = {v.id: v for v in VTC.query.all()}

    rows = []
    for i, r in enumerate(q.all()):
        vtc = vtc_lookup.get(r.vtc_id) if r.vtc_id else None
        rows.append({
            'rank': i + 1,
            'discord_id': r.discord_id,
            'discord_username': r.discord_username,
            'vtc_id': r.vtc_id,
            'vtc_name': vtc.name if vtc else None,
            'vtc_tag': vtc.tag if vtc else None,
            'total_distance_km': r.total_distance_km or 0,
            'experience_points': r.experience_points or 0,
            'job_count': int(r.job_count or 0),
            'total_revenue': int(r.total_revenue or 0),
        })

    return rows


# ─── WEB PAGES ──────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/dashboard')
@require_discord
def dashboard():
    discord_id = session['discord_id']
    discord_username = session['discord_username']

    player = Player.query.filter_by(discord_id=discord_id).first()
    latest = _latest_snapshot(player.id) if player else None
    vtc = VTC.query.get(player.vtc_id) if player and player.vtc_id else None

    jobs = []
    job_count = 0
    total_revenue = 0
    dist_rank = None
    if player:
        jobs = Job.query.filter_by(player_id=player.id)\
            .order_by(Job.logged_at.desc()).limit(50).all()
        job_count = Job.query.filter_by(player_id=player.id).count()
        total_revenue = db.session.query(
            db.func.coalesce(db.func.sum(Job.revenue), 0)
        ).filter_by(player_id=player.id).scalar() or 0

        for row in _build_leaderboard(sort='distance', limit=500):
            if row['discord_id'] == discord_id:
                dist_rank = row['rank']
                break

    return render_template('dashboard.html',
        discord_id=discord_id,
        discord_username=discord_username,
        player=player,
        latest=latest,
        vtc=vtc,
        jobs=jobs,
        job_count=job_count,
        total_revenue=total_revenue,
        dist_rank=dist_rank,
    )


# ─── SNAPSHOT API ───────────────────────────────────────────────

@app.route('/api/snapshot', methods=['POST'])
def receive_snapshot():
    auth = request.headers.get('Authorization')
    if not auth or not auth.startswith('Bearer '):
        return jsonify({'error': 'Unauthorized'}), 401

    discord_id = request.headers.get('X-Discord-ID')
    discord_username = request.headers.get('X-Discord-Username')

    if not discord_id or not discord_username:
        return jsonify({'error': 'Missing Discord identity'}), 400

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    player = _get_or_create_player(discord_id, discord_username)

    snapshot = Snapshot(
        player_id=player.id,
        money=data['finances']['money'],
        total_debt=data['finances']['total_debt'],
        experience_points=data['player']['experience_points'],
        total_distance_km=data['player']['total_distance_km'],
        driver_count=len(data['drivers']),
        loan_count=len(data['finances']['loans']),
        drivers=data['drivers'],
        jobs=data['jobs'],
        loans=data['finances']['loans'],
    )
    db.session.add(snapshot)

    new_jobs = _detect_and_log_jobs(player.id, data.get('jobs', []))

    db.session.commit()

    return jsonify({
        'status': 'ok',
        'snapshot_id': snapshot.id,
        'new_jobs': new_jobs,
    }), 200


# ─── TELEMETRY API ──────────────────────────────────────────────

@app.route('/api/telemetry', methods=['POST'])
def receive_telemetry():
    auth = request.headers.get('Authorization')
    if not auth or not auth.startswith('Bearer '):
        return jsonify({'error': 'Unauthorized'}), 401

    discord_id = request.headers.get('X-Discord-ID')
    if not discord_id:
        return jsonify({'error': 'Missing Discord ID'}), 400

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    telemetry_store[discord_id] = {
        **data,
        'updated_at': datetime.utcnow().isoformat()
    }

    return jsonify({'status': 'ok'}), 200


@app.route('/api/telemetry/<discord_id>', methods=['GET'])
def get_telemetry(discord_id):
    data = telemetry_store.get(discord_id)
    if not data:
        return jsonify({'error': 'No telemetry data'}), 404
    return jsonify(data), 200


# ─── PLAYER API ─────────────────────────────────────────────────

@app.route('/api/player/<discord_id>', methods=['GET'])
def get_player(discord_id):
    player = Player.query.filter_by(discord_id=discord_id).first()
    if not player:
        return jsonify({'error': 'Player not found'}), 404

    latest = _latest_snapshot(player.id)
    if not latest:
        return jsonify({'error': 'No data yet'}), 404

    return jsonify({
        'discord_id': player.discord_id,
        'discord_username': player.discord_username,
        'last_seen': player.last_seen.isoformat(),
        'vtc_id': player.vtc_id,
        'money': latest.money,
        'total_debt': latest.total_debt,
        'experience_points': latest.experience_points,
        'total_distance_km': latest.total_distance_km,
        'driver_count': latest.driver_count,
        'loan_count': latest.loan_count,
        'drivers': latest.drivers,
        'jobs': latest.jobs,
        'loans': latest.loans,
    }), 200


@app.route('/api/players', methods=['GET'])
def get_all_players():
    players = Player.query.all()
    result = []
    for p in players:
        latest = _latest_snapshot(p.id)
        if latest:
            result.append({
                'discord_id': p.discord_id,
                'discord_username': p.discord_username,
                'last_seen': p.last_seen.isoformat(),
                'vtc_id': p.vtc_id,
                'money': latest.money,
                'total_debt': latest.total_debt,
                'driver_count': latest.driver_count,
            })
    return jsonify(result), 200


# ─── JOB API ────────────────────────────────────────────────────

@app.route('/api/jobs/<discord_id>', methods=['GET'])
def get_player_jobs(discord_id):
    player = Player.query.filter_by(discord_id=discord_id).first()
    if not player:
        return jsonify({'error': 'Player not found'}), 404

    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 200)

    paginated = Job.query.filter_by(player_id=player.id)\
        .order_by(Job.game_day.desc(), Job.logged_at.desc())\
        .paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'discord_username': player.discord_username,
        'jobs': [_job_to_dict(j) for j in paginated.items],
        'total': paginated.total,
        'page': page,
        'pages': paginated.pages,
    }), 200


@app.route('/api/vtc/<int:vtc_id>/jobs', methods=['GET'])
def get_vtc_jobs(vtc_id):
    vtc = VTC.query.get_or_404(vtc_id)
    member_ids = [p.id for p in vtc.members]

    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 200)

    paginated = Job.query.filter(Job.player_id.in_(member_ids))\
        .order_by(Job.logged_at.desc())\
        .paginate(page=page, per_page=per_page, error_out=False)

    # Build a player-id → username lookup so we don't N+1 in the loop
    player_names = {p.id: p.discord_username for p in vtc.members}

    jobs_out = []
    for j in paginated.items:
        d = _job_to_dict(j)
        d['discord_username'] = player_names.get(j.player_id, 'Unknown')
        jobs_out.append(d)

    return jsonify({
        'vtc_id': vtc_id,
        'vtc_name': vtc.name,
        'jobs': jobs_out,
        'total': paginated.total,
        'page': page,
        'pages': paginated.pages,
    }), 200


# ─── VTC API ────────────────────────────────────────────────────

@app.route('/api/vtc/create', methods=['POST'])
def api_vtc_create():
    discord_id = request.headers.get('X-Discord-ID')
    discord_username = request.headers.get('X-Discord-Username')
    if not discord_id or not discord_username:
        return jsonify({'error': 'Missing Discord identity'}), 400

    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    tag = (data.get('tag') or '').strip().upper()

    if not name or not tag:
        return jsonify({'error': 'name and tag are required'}), 400
    if len(tag) < 2 or len(tag) > 6:
        return jsonify({'error': 'tag must be 2-6 characters'}), 400

    player = _get_or_create_player(discord_id, discord_username)
    if player.vtc_id:
        return jsonify({'error': 'Already in a VTC — leave it first'}), 409

    vtc = VTC(name=name, tag=tag, owner_discord_id=discord_id)
    db.session.add(vtc)
    db.session.flush()
    player.vtc_id = vtc.id
    db.session.commit()

    return jsonify({
        'status': 'created',
        'vtc_id': vtc.id,
        'name': vtc.name,
        'tag': vtc.tag,
        'access_code': vtc.access_code,
    }), 201


@app.route('/api/vtc/join', methods=['POST'])
def api_vtc_join():
    discord_id = request.headers.get('X-Discord-ID')
    discord_username = request.headers.get('X-Discord-Username')
    if not discord_id or not discord_username:
        return jsonify({'error': 'Missing Discord identity'}), 400

    data = request.get_json() or {}
    code = (data.get('access_code') or '').strip().upper()

    if not code:
        return jsonify({'error': 'access_code is required'}), 400

    vtc = VTC.query.filter_by(access_code=code).first()
    if not vtc:
        return jsonify({'error': 'Invalid access code'}), 404

    player = _get_or_create_player(discord_id, discord_username)
    if player.vtc_id == vtc.id:
        return jsonify({'error': 'Already a member'}), 409
    if player.vtc_id:
        return jsonify({'error': 'Already in a VTC — leave it first'}), 409

    player.vtc_id = vtc.id
    db.session.commit()

    return jsonify({'status': 'joined', 'vtc_id': vtc.id, 'name': vtc.name, 'tag': vtc.tag}), 200


@app.route('/api/vtc/leave', methods=['POST'])
def api_vtc_leave():
    discord_id = request.headers.get('X-Discord-ID')
    discord_username = request.headers.get('X-Discord-Username')
    if not discord_id or not discord_username:
        return jsonify({'error': 'Missing Discord identity'}), 400

    player = _get_or_create_player(discord_id, discord_username)
    if not player.vtc_id:
        return jsonify({'error': 'Not in a VTC'}), 409

    vtc = VTC.query.get(player.vtc_id)
    player.vtc_id = None

    if vtc and vtc.owner_discord_id == discord_id:
        remaining = Player.query.filter_by(vtc_id=vtc.id).count()
        if remaining == 0:
            db.session.delete(vtc)

    db.session.commit()
    return jsonify({'status': 'left'}), 200


@app.route('/api/vtc/<int:vtc_id>', methods=['GET'])
def api_get_vtc(vtc_id):
    vtc = VTC.query.get_or_404(vtc_id)
    members = []
    for p in vtc.members:
        latest = _latest_snapshot(p.id)
        job_count = Job.query.filter_by(player_id=p.id).count()
        members.append({
            'discord_id': p.discord_id,
            'discord_username': p.discord_username,
            'last_seen': p.last_seen.isoformat(),
            'is_owner': p.discord_id == vtc.owner_discord_id,
            'money': latest.money if latest else None,
            'total_distance_km': latest.total_distance_km if latest else None,
            'experience_points': latest.experience_points if latest else None,
            'driver_count': latest.driver_count if latest else None,
            'job_count': job_count,
        })
    return jsonify({
        'id': vtc.id,
        'name': vtc.name,
        'tag': vtc.tag,
        'access_code': vtc.access_code,
        'owner_discord_id': vtc.owner_discord_id,
        'created_at': vtc.created_at.isoformat(),
        'member_count': len(members),
        'members': members,
    }), 200


# ─── VTC WEB UI ─────────────────────────────────────────────────

@app.route('/vtc')
@require_discord
def vtc_home():
    discord_id = session['discord_id']
    discord_username = session['discord_username']

    player = Player.query.filter_by(discord_id=discord_id).first()
    vtc = None
    if player and player.vtc_id:
        vtc = VTC.query.get(player.vtc_id)

    return render_template('vtc.html',
        discord_id=discord_id,
        discord_username=discord_username,
        player=player,
        vtc=vtc,
    )


@app.route('/vtc/create', methods=['POST'])
@require_discord
def vtc_create():
    discord_id = session['discord_id']
    discord_username = session['discord_username']

    name = request.form.get('name', '').strip()
    tag = request.form.get('tag', '').strip().upper()

    error = None
    if not name or not tag:
        error = 'VTC name and tag are required.'
    elif len(tag) < 2 or len(tag) > 6:
        error = 'Tag must be 2–6 characters.'
    else:
        player = _get_or_create_player(discord_id, discord_username)
        if player.vtc_id:
            error = 'You are already in a VTC. Leave it first.'
        else:
            vtc = VTC(name=name, tag=tag, owner_discord_id=discord_id)
            db.session.add(vtc)
            db.session.flush()
            player.vtc_id = vtc.id
            db.session.commit()
            return redirect(url_for('vtc_dashboard', vtc_id=vtc.id))

    player = Player.query.filter_by(discord_id=discord_id).first()
    return render_template('vtc.html',
        discord_id=discord_id,
        discord_username=discord_username,
        player=player,
        vtc=None,
        error=error,
    )


@app.route('/vtc/join', methods=['POST'])
@require_discord
def vtc_join():
    discord_id = session['discord_id']
    discord_username = session['discord_username']

    code = request.form.get('access_code', '').strip().upper()

    error = None
    if not code:
        error = 'Access code is required.'
    else:
        vtc = VTC.query.filter_by(access_code=code).first()
        if not vtc:
            error = 'Invalid access code — no VTC found.'
        else:
            player = _get_or_create_player(discord_id, discord_username)
            if player.vtc_id:
                error = 'You are already in a VTC. Leave it first.'
            else:
                player.vtc_id = vtc.id
                db.session.commit()
                return redirect(url_for('vtc_dashboard', vtc_id=vtc.id))

    player = Player.query.filter_by(discord_id=discord_id).first()
    return render_template('vtc.html',
        discord_id=discord_id,
        discord_username=discord_username,
        player=player,
        vtc=None,
        error=error,
    )


@app.route('/vtc/leave', methods=['POST'])
@require_discord
def vtc_leave():
    discord_id = session['discord_id']
    player = Player.query.filter_by(discord_id=discord_id).first()

    if player and player.vtc_id:
        vtc = VTC.query.get(player.vtc_id)
        player.vtc_id = None
        if vtc and vtc.owner_discord_id == discord_id:
            remaining = Player.query.filter_by(vtc_id=vtc.id).count()
            if remaining == 0:
                db.session.delete(vtc)
        db.session.commit()

    return redirect(url_for('vtc_home'))


@app.route('/vtc/<int:vtc_id>')
@require_discord
def vtc_dashboard(vtc_id):
    discord_id = session['discord_id']
    vtc = VTC.query.get_or_404(vtc_id)

    aggregate = {
        'total_money': 0,
        'total_debt': 0,
        'total_xp': 0,
        'total_distance_km': 0,
        'total_drivers': 0,
        'on_job': 0,
        'total_jobs': 0,
    }

    members = []
    all_drivers = []
    member_ids = [p.id for p in vtc.members]
    player_names = {p.id: p.discord_username for p in vtc.members}

    for p in vtc.members:
        latest = _latest_snapshot(p.id)
        job_count = Job.query.filter_by(player_id=p.id).count()

        member = {
            'discord_id': p.discord_id,
            'discord_username': p.discord_username,
            'last_seen': p.last_seen,
            'is_owner': p.discord_id == vtc.owner_discord_id,
            'money': latest.money if latest else 0,
            'total_debt': latest.total_debt if latest else 0,
            'experience_points': latest.experience_points if latest else 0,
            'total_distance_km': latest.total_distance_km if latest else 0,
            'driver_count': latest.driver_count if latest else 0,
            'job_count': job_count,
            'has_data': latest is not None,
        }
        members.append(member)

        if latest:
            aggregate['total_money'] += latest.money or 0
            aggregate['total_debt'] += latest.total_debt or 0
            aggregate['total_xp'] += latest.experience_points or 0
            aggregate['total_distance_km'] += latest.total_distance_km or 0
            aggregate['total_drivers'] += latest.driver_count or 0
            aggregate['total_jobs'] += job_count

            if latest.drivers:
                for d in latest.drivers:
                    driver = dict(d)
                    driver['member'] = p.discord_username
                    driver['member_id'] = p.discord_id
                    all_drivers.append(driver)
                    if driver.get('state') == 2:
                        aggregate['on_job'] += 1

    # Recent jobs from the Job table — real timestamps, attributed to members
    recent_jobs = Job.query\
        .filter(Job.player_id.in_(member_ids))\
        .order_by(Job.logged_at.desc())\
        .limit(60).all()

    return render_template('vtc_dashboard.html',
        vtc=vtc,
        members=members,
        aggregate=aggregate,
        all_drivers=all_drivers,
        recent_jobs=recent_jobs,
        player_names=player_names,
        discord_id=discord_id,
        discord_username=session['discord_username'],
        is_owner=vtc.owner_discord_id == discord_id,
    )


# ─── LEADERBOARD API ────────────────────────────────────────────

@app.route('/api/leaderboard')
def api_leaderboard_global():
    sort = request.args.get('sort', 'distance')
    limit = min(request.args.get('limit', 100, type=int), 500)
    rows = _build_leaderboard(sort=sort, limit=limit)
    return jsonify({'scope': 'global', 'sort': sort, 'rows': rows}), 200


@app.route('/api/leaderboard/vtc/<int:vtc_id>')
def api_leaderboard_vtc(vtc_id):
    vtc = VTC.query.get_or_404(vtc_id)
    sort = request.args.get('sort', 'distance')
    limit = min(request.args.get('limit', 100, type=int), 500)
    member_ids = [p.id for p in vtc.members]
    rows = _build_leaderboard(player_ids=member_ids, sort=sort, limit=limit)
    return jsonify({
        'scope': 'vtc',
        'vtc_id': vtc_id,
        'vtc_name': vtc.name,
        'vtc_tag': vtc.tag,
        'sort': sort,
        'rows': rows,
    }), 200


# ─── LEADERBOARD WEB ────────────────────────────────────────────

@app.route('/leaderboard')
def leaderboard_global():
    sort = request.args.get('sort', 'distance')
    rows = _build_leaderboard(sort=sort, limit=100)
    discord_id = session.get('discord_id')
    discord_username = session.get('discord_username')
    return render_template('leaderboard.html',
        rows=rows,
        sort=sort,
        scope='global',
        vtc=None,
        discord_id=discord_id,
        discord_username=discord_username,
    )


@app.route('/leaderboard/vtc/<int:vtc_id>')
def leaderboard_vtc(vtc_id):
    vtc = VTC.query.get_or_404(vtc_id)
    sort = request.args.get('sort', 'distance')
    member_ids = [p.id for p in vtc.members]
    rows = _build_leaderboard(player_ids=member_ids, sort=sort, limit=100)
    discord_id = session.get('discord_id')
    discord_username = session.get('discord_username')
    return render_template('leaderboard.html',
        rows=rows,
        sort=sort,
        scope='vtc',
        vtc=vtc,
        discord_id=discord_id,
        discord_username=discord_username,
    )


if __name__ == '__main__':
    app.run(debug=True, use_reloader=False, port=5001)
