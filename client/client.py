import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import time
import os
import sys
import shutil
import requests
import threading
import subprocess
import tempfile
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from parser import parse_sii, parse_freight_market
from dotenv import load_dotenv
import pystray
from PIL import Image, ImageDraw
from dispatcher import build_dispatch_messages
from telemetry import start_telemetry_loop
from city_db import build_city_db
import assistant

load_dotenv()

# ── Single-instance guard ─────────────────────────────────────────────────────

def _acquire_single_instance_lock():
    """
    Create a Windows named mutex. If another process already holds it,
    show a warning and exit — only one Dispatch client may run at a time.
    Returns the mutex handle (must stay alive for the process lifetime).
    """
    import ctypes
    mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "TheDispatch_SingleInstance_v1")
    ERROR_ALREADY_EXISTS = 183
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning(
                'The Dispatch',
                'The Dispatch is already running.\nCheck the system tray.'
            )
            root.destroy()
        except Exception:
            print('[Dispatch] Already running — only one instance allowed.')
        sys.exit(0)
    return mutex   # keep reference so GC doesn't release the handle

_instance_mutex = _acquire_single_instance_lock()

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
    """
    Return the path to the most recently written game.sii across all save
    slots for ATS and ETS2.  Autosave slots (name starts with 'autosave')
    are updated every few minutes during active play; manual saves are only
    written when the player uses the in-game save menu.  Sorting purely by
    mtime means the freshest file always wins regardless of slot type.
    """
    candidates = []

    steam_userdata = os.path.expandvars(r'%PROGRAMFILES(X86)%\Steam\userdata')
    for appid, label in [('270880', 'ATS'), ('227300', 'ETS2')]:
        if not os.path.exists(steam_userdata):
            continue
        for user_id in os.listdir(steam_userdata):
            prof_root = os.path.join(steam_userdata, user_id, appid, 'remote', 'profiles')
            if not os.path.exists(prof_root):
                continue
            for profile in os.listdir(prof_root):
                save_dir = os.path.join(prof_root, profile, 'save')
                if not os.path.exists(save_dir):
                    continue
                for slot in os.listdir(save_dir):
                    candidate = os.path.join(save_dir, slot, 'game.sii')
                    if os.path.exists(candidate):
                        candidates.append((label, candidate))

    if not candidates:
        return None

    # Always pick the most recently written file — autosaves beat manual saves
    # automatically because they are written more frequently.
    candidates.sort(key=lambda x: os.path.getmtime(x[1]), reverse=True)
    label, path = candidates[0]
    slot_name = os.path.basename(os.path.dirname(path))
    print(f'[Dispatch] Freshest {label} save: slot={slot_name!r}  path={path}')
    return path


def _save_root_dir(save_path: str) -> str:
    """Given .../save/SLOT/game.sii return .../save/ (the directory to watch)."""
    return os.path.dirname(os.path.dirname(save_path))


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

    # Copy first — ATS may hold an exclusive lock on the original save file.
    # Decrypting a copy avoids blocking on that lock.
    copy_tmp = tempfile.NamedTemporaryFile(suffix='_copy.sii', delete=False)
    copy_tmp.close()
    try:
        shutil.copy2(filepath, copy_tmp.name)
        print(f'[Dispatch] Copied save to temp: {copy_tmp.name}')
    except Exception as e:
        print(f'[Dispatch] Could not copy save file: {e}')
        try:
            os.unlink(copy_tmp.name)
        except OSError:
            pass
        return None

    tmp = tempfile.NamedTemporaryFile(suffix='.sii', delete=False)
    tmp.close()
    try:
        result = subprocess.run(
            [decrypt_exe, copy_tmp.name, tmp.name],
            capture_output=True, timeout=120
        )
        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='replace').strip()
            print(f'[Dispatch] Decryption failed (rc={result.returncode}): {stderr}')
            return None
        return tmp.name
    except Exception as e:
        print(f'[Dispatch] Decryption error: {e}')
        return None
    finally:
        try:
            os.unlink(copy_tmp.name)
        except OSError:
            pass


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

        with open(decrypted, 'r', encoding='utf-8') as _f:
            data['freight_market'] = parse_freight_market(_f.read())

        messages = build_dispatch_messages(last_snapshot, data)
        for msg in messages:
            print(f'[Dispatch] 📻 {msg}')
            assistant.speak(msg)   # routes through tts_queue — never overlaps PTT

        last_snapshot = data

        # Feed parsed data to the assistant (includes freight_market from parse_sii)
        assistant.state.update_snapshot(data, data.get('freight_market', []))
        # Check if any proactive briefing triggers fired
        assistant.check_proactive_triggers()

        # Update overlay with current job info from the parsed snapshot
        try:
            from overlay import overlay_state
            player = data.get('player', {}) or {}
            job_cargo = (player.get('job_info_cargo') or '').strip()
            try:
                job_dist_km = int(player.get('job_info_planned_distance_km') or 0)
            except (TypeError, ValueError):
                job_dist_km = 0
            if job_cargo and job_cargo not in ('null', 'nil') and job_dist_km > 0:
                job_target = player.get('job_info_target') or ''
                dest_parts = job_target.split('.') if job_target else []
                dest_city  = dest_parts[-1].replace('_', ' ').title() if dest_parts else 'unknown'
                overlay_state['current_job'] = {
                    'cargo':          job_cargo,
                    'destination':    dest_city,
                    'distance_miles': round(job_dist_km * 0.621371),
                }
            else:
                overlay_state['current_job'] = None
        except Exception:
            pass

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
    """
    Watch the entire save/ directory tree recursively.  When any game.sii
    changes (autosave, autosave_drive_N, autosave_job_N, manual slots…),
    re-discover the freshest file via find_save_file() and push that.
    This ensures the assistant always sees the most current game state
    regardless of which slot ATS chose to write.
    """
    def __init__(self):
        self.last_push = 0

    def on_modified(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith('game.sii'):
            now = time.time()
            print(f'[Dispatch] FS event: game.sii modified at {event.src_path}')
            if now - self.last_push > 30:
                self.last_push = now
                freshest = find_save_file()
                if freshest:
                    slot = os.path.basename(os.path.dirname(freshest))
                    print(f'[Dispatch] Save change detected — using freshest slot={slot!r}')
                    push_data(freshest)
                else:
                    print('[Dispatch] FS event fired but find_save_file() returned None')
            else:
                print(f'[Dispatch] FS event throttled (last push {now - self.last_push:.0f}s ago)')
        else:
            # Log non-game.sii events at low verbosity so we can confirm watchdog is firing
            fname = os.path.basename(event.src_path)
            if fname not in ('info.sii',):  # skip noisy metadata files
                print(f'[Dispatch] FS event (ignored): {fname}')


def start_watcher(save_root: str):
    """Watch save_root (the .../save/ directory) recursively for any game.sii writes."""
    event_handler = SaveWatcher()
    observer = Observer()
    observer.schedule(event_handler, path=save_root, recursive=True)
    observer.start()
    _status['watching'] = save_root
    print(f'[Dispatch] Watching save directory: {save_root}')
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
  <title>The Dispatch &mdash; Connected</title>
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


# ── Server preferences sync ───────────────────────────────────────

def _sync_server_prefs(discord_id: str, discord_token: str):
    """
    Pull preferences from the server and write to local preferences.json.
    The server is canonical for settings changed via the web dashboard form.
    PTT_KEY from .env always wins (applied separately in load_prefs).
    """
    if not discord_id or not discord_token:
        return
    try:
        resp = requests.get(
            f'{SERVER_URL}/api/preferences/{discord_id}',
            headers={'Authorization': f'Bearer {discord_token}'},
            timeout=5,
        )
        if resp.status_code != 200:
            return
        server_prefs = resp.json()
        if not isinstance(server_prefs, dict):
            return

        prefs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'preferences.json')
        import json
        local_prefs = {}
        if os.path.exists(prefs_path):
            try:
                with open(prefs_path, 'r', encoding='utf-8') as f:
                    local_prefs = json.load(f)
            except Exception:
                pass

        # Merge: server values override local, but preserve any keys the server
        # doesn't know about (e.g. future local-only settings).
        merged = {**local_prefs, **server_prefs}
        with open(prefs_path, 'w', encoding='utf-8') as f:
            json.dump(merged, f, indent=2)
        print(f'[Dispatch] Preferences synced from server ({len(server_prefs)} keys)')
    except Exception as e:
        print(f'[Dispatch] Could not sync preferences from server: {e}')


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

    # Sync preferences from server → local preferences.json so web-form changes
    # take effect on next client restart without manual file editing.
    _sync_server_prefs(DISCORD_ID, DISCORD_TOKEN)

    # Auto-install SCS telemetry plugin
    install_telemetry_plugin()

    # Build city coordinate database (base-game hardcoded + unencrypted ZIP mods)
    ats_path = os.getenv('ATS_INSTALL_PATH', '') or None
    assistant.city_db = build_city_db(ats_path)

    # Always use find_save_file() so autosaves are preferred over stale manual slots.
    # SAVE_PATH in .env is ignored — it was previously pointing to a manual save (slot 2)
    # which hadn't been written since the last time the player used the in-game save menu.
    filepath = find_save_file()
    if not filepath:
        _set_status('Save file not found')
        print('[Dispatch] Could not find ATS/ETS2 save file.')
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                'The Dispatch',
                'Could not find your ATS or ETS2 save file.\n\n'
                'Make sure ATS or ETS2 has been run at least once.'
            )
            root.destroy()
        except Exception:
            pass
        return

    save_root = _save_root_dir(filepath)

    print(f'[Dispatch] Starting as {DISCORD_USERNAME}')
    _set_status(f'Watching — {DISCORD_USERNAME}')

    # Background services — each gets its own thread
    start_telemetry_loop()
    assistant.start()
    threading.Thread(target=push_data, args=(filepath,), daemon=True).start()

    observer = start_watcher(save_root)
    observer_ref = {'obs': observer}
    threading.Thread(target=observer.join, daemon=True).start()

    # Launch the tkinter HUD (always-on-top window, borderless windowed mode)
    try:
        from overlay import start_overlay, overlay_state as _ov_state
        start_overlay(_ov_state)
    except Exception as _ov_err:
        print(f'[Dispatch] Overlay failed to start: {_ov_err}')

    # Tray icon — blocks the main thread for the process lifetime
    icon = pystray.Icon(
        'The Dispatch',
        _create_tray_icon(),
        f'The Dispatch — {DISCORD_USERNAME}',
        menu=_make_menu(observer_ref),
    )
    icon.run()


if __name__ == '__main__':
    main()
