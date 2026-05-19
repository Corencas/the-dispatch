from flask import Blueprint, redirect, request, session, jsonify
import os
import secrets
import requests as http_requests
from urllib.parse import urlencode

auth_bp = Blueprint('auth', __name__)

DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID')
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET')
DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI')

@auth_bp.route('/auth/login')
def login():
    state = secrets.token_hex(16)
    session['oauth_state'] = state
    # Desktop client flow: pass client_callback param
    session['client_callback'] = request.args.get('client_callback', '')
    # Web flow: pass next param (e.g. /vtc)
    session['next_url'] = request.args.get('next', '/vtc')
    session.modified = True

    params = {
        'client_id': DISCORD_CLIENT_ID,
        'redirect_uri': DISCORD_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'identify',
        'state': state,
    }

    return redirect(f'https://discord.com/api/oauth2/authorize?{urlencode(params)}')

@auth_bp.route('/auth/callback')
def callback():
    code = request.args.get('code')

    if not code:
        return jsonify({'error': 'No code provided'}), 400

    token_response = http_requests.post(
        'https://discord.com/api/oauth2/token',
        data={
            'client_id': DISCORD_CLIENT_ID,
            'client_secret': DISCORD_CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': DISCORD_REDIRECT_URI,
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'}
    )

    token_data = token_response.json()
    access_token = token_data.get('access_token')

    if not access_token:
        return jsonify({'error': 'Failed to get access token', 'details': token_data}), 400

    user_response = http_requests.get(
        'https://discord.com/api/users/@me',
        headers={'Authorization': f'Bearer {access_token}'}
    )
    user = user_response.json()

    client_callback = session.get('client_callback', '')
    next_url = session.get('next_url', '/vtc')

    if client_callback:
        # Desktop client flow — redirect back to the local app
        session_token = secrets.token_hex(32)
        params = urlencode({
            'token': session_token,
            'discord_id': user['id'],
            'discord_username': user['username'],
        })
        return redirect(f'{client_callback}?{params}')

    # Web flow — store identity in session and redirect
    session['discord_id'] = user['id']
    session['discord_username'] = user['username']
    return redirect(next_url or '/vtc')

@auth_bp.route('/auth/logout')
def logout():
    session.pop('discord_id', None)
    session.pop('discord_username', None)
    return redirect('/')
