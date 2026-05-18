from flask import Flask, request, jsonify
from flask_cors import CORS
from models import db, Player, Snapshot
from datetime import datetime
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__)
CORS(app)

app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///dispatch.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

with app.app_context():
    db.create_all()

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

    player = Player.query.filter_by(discord_id=discord_id).first()
    if not player:
        player = Player(discord_id=discord_id, discord_username=discord_username)
        db.session.add(player)
        db.session.flush()
    else:
        player.last_seen = datetime.utcnow()
        player.discord_username = discord_username

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
    db.session.commit()

    return jsonify({'status': 'ok', 'snapshot_id': snapshot.id}), 200

@app.route('/api/player/<discord_id>', methods=['GET'])
def get_player(discord_id):
    player = Player.query.filter_by(discord_id=discord_id).first()
    if not player:
        return jsonify({'error': 'Player not found'}), 404

    latest = Snapshot.query.filter_by(player_id=player.id)\
        .order_by(Snapshot.captured_at.desc()).first()

    if not latest:
        return jsonify({'error': 'No data yet'}), 404

    return jsonify({
        'discord_id': player.discord_id,
        'discord_username': player.discord_username,
        'last_seen': player.last_seen.isoformat(),
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
        latest = Snapshot.query.filter_by(player_id=p.id)\
            .order_by(Snapshot.captured_at.desc()).first()
        if latest:
            result.append({
                'discord_id': p.discord_id,
                'discord_username': p.discord_username,
                'last_seen': p.last_seen.isoformat(),
                'money': latest.money,
                'total_debt': latest.total_debt,
                'driver_count': latest.driver_count,
            })
    return jsonify(result), 200

if __name__ == '__main__':
    app.run(debug=True, port=5001)