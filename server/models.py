from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Player(db.Model):
    __tablename__ = 'players'
    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(64), unique=True, nullable=False)
    discord_username = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)
    snapshots = db.relationship('Snapshot', backref='player', lazy=True)

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