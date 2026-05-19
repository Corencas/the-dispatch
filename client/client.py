import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import time
import os
import shutil
import requests
import threading
import subprocess
import tempfile
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from parser import parse_sii
from dotenv import load_dotenv
import pystray
from PIL import Image, ImageDraw
from dispatcher import generate_and_play, build_dispatch_messages
from telemetry import start_telemetry_loop

load_dotenv()

SERVER_URL       = os.getenv('SERVER_URL', 'http://127.0.0.1:5001')
DISCORD_TOKEN    = os.getenv('DISCORD_TOKEN', '')
DISCORD_ID       = os.getenv('DISCORD_ID', '')
DISCORD_USERNAME = os.getenv('DISCORD_USERNAME', '')
SAVE_PATH        = os.getenv('SAVE_PATH', '')

# ── Mutable state readable from tray menu ─────────────────────────
_status = {'text': 'Starting...', 'last_push': None, 'watching': ''}


def _set_status(text):
    _status['text'] = text


# ── Save file discovery ───────────────────────────────────────────

def find_save_file():
    candidates = []

    # ATS
    steam_userdata = os.path.expandvars(r'%PROGRAMFILES(X86)%\Steam\userdata')
    if os.path.exists(steam_userdata):
        for user_id in os.listdir(steam_userdata):
            ats_path = os.path.join(steam_userdata, user_id, '270880', 'remote', 'profiles')
            if os.path.exists(ats_path):
                for profile in sorted(os.listdir(ats_path),
                        key=lambda p: os.path.getmtime(os.path.join(ats_path, p)), reverse=True):
                    save_dir = os.path.join(ats_path, profile, 'save')
                    if os.path.exists(save_dir):
                        for save in sorted(os.listdir(save_dir),
                                key=lambda s: os.path.getmtime(os.path.join(save_dir, s)), reverse=True):
                            candidate = os.path.join(save_dir, save, 'game.sii')
                            if os.path.exists(candidate):
                                candidates.append(('ATS', candidate))

    # ETS2 (appid 227300)
    if os.path.exists(steam_userdata):
        for user_id in os.listdir(steam_userdata):
            ets_path = os.path.join(steam_userdata, user_id, '227300', 'remote', 'profiles')
            if os.path.exists(ets_path):
                for profile in sorted(os.listdir(ets_path),
                        key=lambda p: os.path.getmtime(os.path.join(ets_path, p)), reverse=True):
                    save_dir = os.path.join(ets_path, profile, 'save')
                    if os.path.exists(save_dir):
                        for save in sorted(os.listdir(save_dir),
                                key=lambda s: os.path.getmtime(os.path.join(save_dir, s)), reverse=True):
                            candidate = os.path.join(save_dir, save, 'game.sii')
                            if os.path.exists(candidate):
                                candidates.append(('ETS2', candidate))

    if candidates:
        # Prefer most recently modified
        candidates.sort(key=lambda x: os.path.getmtime(x[1]), reverse=True)
        game, path = candidates[0]
        print(f'[Dispatch] Found {game} save: {path}')
        return path

    return None


# ── SCS telemetry plugin auto-install ─────────────────────────────

def install_telemetry_plugin():
    """
    Copy the scs-telemetry DLL from the truck_telemetry package into the
    ATS and ETS2 plugins directories so the SDK can read live data.
    """
    try:
        import truck_telemetry
        pkg_dir = os.path.dirname(truck_telemetry.__file__)
        dlls = [f for f in os.listdir(pkg_dir) if f.lower().endswith('.dll')]
        if not dlls:
            return

        docs = os.path.expandvars('%USERPROFILE%\\Documents')
        game_dirs = {
            'ATS':  os.path.join(docs, 'American Truck Simulator', 'plugins'),
            'ETS2': os.path.join(docs, 'Euro Truck Simulator 2', 'plugins'),
        }

        for game, plugins_dir in game_dirs.items():
            parent = os.path.dirname(plugins_dir)
            if not os.path.exists(parent):
                continue
            os.makedirs(plugins_dir, exist_ok=True)
            for dll in dlls:
                dst = os.path.join(plugins_dir, dll)
                src = os.path.join(pkg_dir, dll)
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)
                    print(f'[Dispatch] Installed telemetry plugin for {game}')
    except Exception as e:
        print(f'[Dispatch] Telemetry plugin install skipped: {e}')


# ── Save decryption ───────────────────────────────────────────────

def decrypt_save(filepath):
    decrypt_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'SII_Decrypt.exe')
    if not os.path.exists(decrypt_exe):
        print('[Dispatch] SII_Decrypt.exe not found — place it in the client folder')
        return None

    tmp = tempfile.NamedTemporaryFile(suffix='.sii', delete=False)
    tmp.close()

    try:
        result = subprocess.run(
            [decrypt_exe, filepath, tmp.name],
            capture_output=True, timeout=60
        )
        return tmp.name if result.returncode == 0 else None
    except Exception as e:
        print(f'[Dispatch] Decryption error: {e}')
        return None


# ── Data push ─────────────────────────────────────────────────────

last_snapshot = {}


def push_data(filepath):
    global last_snapshot
    _set_status('Syncing...')

    decrypted = decrypt_save(filepath)
    if not decrypted:
        _set_status('Decrypt failed')
        return

    try:
        data = parse_sii(decrypted)

        messages = build_dispatch_messages(last_snapshot, data)
        for msg in messages:
            print(f'[Dispatch] 📻 {msg}')
            generate_and_play(msg)

        last_snapshot = data

        response = requests.post(
            f'{SERVER_URL}/api/snapshot',
            json=data,
            headers={
                'Authorization': f'Bearer {DISCORD_TOKEN}',
                'X-Discord-ID': DISCORD_ID,
                'X-Discord-Username': DISCORD_USERNAME,
            },
            timeout=10
        )
        if response.status_code == 200:
            new_jobs = response.json().get('new_jobs', 0)
            _status['last_push'] = time.strftime('%H:%M:%S')
            _set_status(f'Connected · {new_jobs} new job(s)' if new_jobs else 'Connected')
            print(f'[Dispatch] Pushed — {new_jobs} new job(s)')
        else:
            _set_status(f'Server error {response.status_code}')
    except requests.exceptions.ConnectionError:
        _set_status('Server unreachable')
        print('[Dispatch] Could not reach server')
    except Exception as e:
        _set_status('Push failed')
        print(f'[Dispatch] Push error: {e}')
    finally:
        if decrypted and os.path.exists(decrypted):
            os.unlink(decrypted)


# ── File watcher ──────────────────────────────────────────────────

class SaveWatcher(FileSystemEventHandler):
    def __init__(self, filepath):
        self.filepath = filepath
        self.last_push = 0

    def on_modified(self, event):
        if event.src_path.endswith('game.sii'):
            now = time.time()
            if now - self.last_push > 30:
                self.last_push = now
                print('[Dispatch] Save detected, syncing...')
                push_data(self.filepath)


def start_watcher(filepath):
    event_handler = SaveWatcher(filepath)
    observer = Observer()
    observer.schedule(event_handler, path=os.path.dirname(filepath), recursive=False)
    observer.start()
    _status['watching'] = os.path.dirname(filepath)
    print(f'[Dispatch] Watching {filepath}')
    return observer


# ── Tray icon ─────────────────────────────────────────────────────

def _create_tray_icon():
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # amber square with dark D
    draw.rectangle([4, 4, 60, 60], fill=(245, 166, 35))
    draw.rectangle([14, 12, 32, 52], fill=(15, 17, 23))
    draw.rectangle([14, 12, 44, 24], fill=(15, 17, 23))
    draw.rectangle([14, 40, 44, 52], fill=(15, 17, 23))
    draw.ellipse([30, 12, 54, 52], fill=(245, 166, 35))
    draw.ellipse([36, 18, 50, 46], fill=(15, 17, 23))
    return img


def _open_dashboard(icon, item):
    webbrowser.open(f'{SERVER_URL}/dashboard')


def _show_status(icon, item):
    last = _status.get('last_push') or 'Never'
    watching = _status.get('watching') or 'Not set'
    # Use a notification bubble; fall back to print if unavailable
    try:
        icon.notify(
            f'Status: {_status["text"]}\n'
            f'Last push: {last}\n'
            f'Watching: {watching}',
            title='The Dispatch'
        )
    except Exception:
        print(f'[Dispatch] {_status["text"]} | Last push: {last}')


def _make_menu(observer_ref):
    def on_quit(icon, item):
        if observer_ref.get('obs'):
            observer_ref['obs'].stop()
        icon.stop()

    return pystray.Menu(
        pystray.MenuItem('Open Dashboard', _open_dashboard),
        pystray.MenuItem(
            lambda _: f'Status: {_status["text"]}',
            _show_status,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Quit The Dispatch', on_quit),
    )


# ── Discord OAuth flow ────────────────────────────────────────────

_SUCCESS_HTML = b'''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>The Dispatch — Connected</title>
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Inter:wght@400;600&display=swap" rel="stylesheet">
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{background:#080a0c;color:#fff;font-family:'Inter',sans-serif;
         display:flex;align-items:center;justify-content:center;
         height:100vh;text-align:center}
    .card{padding:48px 40px;border:1px solid #1c2028;border-top:3px solid #f5a623;
          background:#0d0f12;max-width:400px;width:90%}
    h1{font-size:1.8rem;color:#f5a623;margin-bottom:8px;letter-spacing:0.04em}
    .user{font-family:'Share Tech Mono',monospace;font-size:0.75rem;
          color:#b0b8cc;letter-spacing:0.15em;margin-bottom:24px}
    p{font-size:0.9rem;color:#b0b8cc;line-height:1.6}
    .dot{width:8px;height:8px;border-radius:50%;background:#1fba5a;
         display:inline-block;margin-right:8px;animation:blink 1.8s infinite}
    @keyframes blink{0%,100%{opacity:1}50%{opacity:0.2}}
  </style>
</head>
<body>
  <div class="card">
    <h1>Connected</h1>
    <div class="user" id="uname"></div>
    <p><span class="dot"></span>The Dispatch client is running in your system tray.<br>
    You can close this window.</p>
  </div>
  <script>
    const p = new URLSearchParams(window.location.search);
    const u = p.get('u') || '';
    if (u) document.getElementById('uname').textContent = u.toUpperCase();
  </script>
</body>
</html>'''


def run_auth_flow():
    print('[Dispatch] No credentials found. Opening Discord login...')
    auth_result = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)

            if 'token' in params:
                auth_result['token']            = params['token'][0]
                auth_result['discord_id']       = params['discord_id'][0]
                auth_result['discord_username'] = params['discord_username'][0]

                # Redirect to success page with username in query string
                username = auth_result['discord_username']
                self.send_response(302)
                self.send_header('Location',
                    f'http://localhost:8080/ok?u={username}')
                self.end_headers()
            elif parsed.path == '/ok':
                self.send_response(200)
                self.send_header('Content-type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(_SUCCESS_HTML)
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    server = HTTPServer(('localhost', 8080), CallbackHandler)
    webbrowser.open(f'{SERVER_URL}/auth/login?client_callback=http://localhost:8080/callback')

    print('[Dispatch] Waiting for Discord login...')
    server.handle_request()  # /callback
    server.handle_request()  # /ok

    if not auth_result:
        print('[Dispatch] Auth cancelled or failed.')
        return None

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    with open(env_path, 'w') as f:
        f.write(f'SERVER_URL={SERVER_URL}\n')
        f.write(f'DISCORD_TOKEN={auth_result["token"]}\n')
        f.write(f'DISCORD_ID={auth_result["discord_id"]}\n')
        f.write(f'DISCORD_USERNAME={auth_result["discord_username"]}\n')
        f.write(f'SAVE_PATH=\n')

    print(f'[Dispatch] Authenticated as {auth_result["discord_username"]}')
    return auth_result


# ── Entry point ───────────────────────────────────────────────────

def main():
    global DISCORD_TOKEN, DISCORD_ID, DISCORD_USERNAME

    if not DISCORD_TOKEN or DISCORD_TOKEN in ('', 'your_discord_token_here'):
        result = run_auth_flow()
        if not result:
            return
        load_dotenv(override=True)
        DISCORD_TOKEN    = os.getenv('DISCORD_TOKEN', '')
        DISCORD_ID       = os.getenv('DISCORD_ID', '')
        DISCORD_USERNAME = os.getenv('DISCORD_USERNAME', '')

    # Auto-install SCS telemetry plugin
    install_telemetry_plugin()

    filepath = SAVE_PATH or find_save_file()
    if not filepath:
        _set_status('Save file not found')
        print('[Dispatch] Could not find ATS/ETS2 save file.')
        print('[Dispatch] Set SAVE_PATH= in client/.env to point at your game.sii')
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                'The Dispatch',
                'Could not find your ATS or ETS2 save file.\n\n'
                'Set SAVE_PATH in client/.env and restart.'
            )
            root.destroy()
        except Exception:
            pass
        return

    print(f'[Dispatch] Starting as {DISCORD_USERNAME}')
    _set_status(f'Watching — {DISCORD_USERNAME}')

    # Start telemetry and do an immediate push
    start_telemetry_loop()
    threading.Thread(target=push_data, args=(filepath,), daemon=True).start()

    observer = start_watcher(filepath)
    observer_ref = {'obs': observer}

    icon = pystray.Icon(
        'The Dispatch',
        _create_tray_icon(),
        f'The Dispatch — {DISCORD_USERNAME}',
        menu=_make_menu(observer_ref),
    )

    threading.Thread(target=observer.join, daemon=True).start()
    icon.run()


if __name__ == '__main__':
    main()
