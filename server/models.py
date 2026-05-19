from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import secrets
import string

db = SQLAlchemy()

def _gen_access_code():
    chars = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(6))

class VTC(db.Model):
    __tablename__ = 'vtcs'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    tag = db.Column(db.String(8), nullable=False)
    owner_discord_id = db.Column(db.String(64), nullable=False)
    access_code = db.Column(db.String(8), unique=True, nullable=False, default=_gen_access_code)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    members = db.relationship('Player', backref='vtc', lazy=True)

class Player(db.Model):
    __tablename__ = 'players'
    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(64), unique=True, nullable=False)
    discord_username = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)
    vtc_id = db.Column(db.Integer, db.ForeignKey('vtcs.id'), nullable=True)
    snapshots = db.relationship('Snapshot', backref='player', lazy=True)
    jobs = db.relationship('Job', backref='player', lazy=True)

class Snapshot(db.Model):
    __tablename__ = 'snapshots'
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('players.id'), nullable=False)
    captured_at = db.Column(db.DateTime, default=datetime.utcnow)
    money = db.Column(db.BigInteger)
    total_debt = db.Column(db.BigInteger)
    experience_points = db.Column(db.BigInteger)
    total_distance_km = db.Column(db.BigInteger)
    driver_count = db.Column(db.Integer)
    loan_count = db.Column(db.Integer)
    drivers = db.Column(db.JSON)
    jobs = db.Column(db.JSON)
    loans = db.Column(db.JSON)

class Job(db.Model):
    __tablename__ = 'jobs'
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('players.id'), nullable=False)
    logged_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Fields extracted from profit_log_entry in the save file
    game_day = db.Column(db.Integer, nullable=False)   # in-game day counter
    source_city = db.Column(db.String(64), nullable=False)
    destination_city = db.Column(db.String(64), nullable=False)
    source_company = db.Column(db.String(64))
    destination_company = db.Column(db.String(64))
    cargo = db.Column(db.String(64))
    distance_km = db.Column(db.Integer)
    revenue = db.Column(db.BigInteger, nullable=False)
    wage = db.Column(db.BigInteger)
    fuel_cost = db.Column(db.BigInteger)
    maintenance_cost = db.Column(db.BigInteger)

    __table_args__ = (
        db.UniqueConstraint(
            'player_id', 'game_day', 'revenue', 'source_city', 'destination_city',
            name='uq_job_identity'
        ),
    )
