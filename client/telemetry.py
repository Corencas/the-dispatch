import truck_telemetry
import time
import threading
import requests
import os
from dotenv import load_dotenv

load_dotenv()

SERVER_URL = os.getenv('SERVER_URL', 'http://127.0.0.1:5001')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
DISCORD_ID = os.getenv('DISCORD_ID', '')
DISCORD_USERNAME = os.getenv('DISCORD_USERNAME', '')

def get_telemetry():
    try:
        truck_telemetry.init()
        data = truck_telemetry.get_data()
        if not data:
            return None
        return {
            'sdk_active': True,
            'speed_kmh': round(abs(data.get('speed', 0)) * 3.6, 1),
            'rpm': round(data.get('engineRpm', 0)),
            'gear': data.get('gear', 0),
            'fuel': round(data.get('fuel', 0), 1),
            'fuel_capacity': round(data.get('fuelCapacity', 0), 1),
            'fuel_pct': round(data.get('fuel', 0) / data.get('fuelCapacity', 1) * 100, 1) if data.get('fuelCapacity') else 0,
            'paused': data.get('paused', False),
            'truck_make': data.get('truckMake', ''),
            'truck_model': data.get('truckModel', ''),
            'cargo': data.get('cargo', ''),
            'cargo_mass': round(data.get('cargoMass', 0), 1),
            'odometer': round(data.get('odometer', 0), 1),
            # World-space GPS coordinates (SCS SDK X/Z = horizontal plane)
            'pos_x': round(data.get('coordinateX', 0), 1),
            'pos_z': round(data.get('coordinateZ', 0), 1),
            # Live job city names from SDK (empty string when not on a job)
            'nav_dst_city': (data.get('cityDst') or '').strip(),
            'nav_src_city': (data.get('citySrc') or '').strip(),
            'on_job': bool(data.get('onJob', False)),
        }
    except Exception as e:
        return None

def push_telemetry(telemetry):
    try:
        requests.post(
            f'{SERVER_URL}/api/telemetry',
            json=telemetry,
            headers={
                'Authorization': f'Bearer {DISCORD_TOKEN}',
                'X-Discord-ID': DISCORD_ID,
                'X-Discord-Username': DISCORD_USERNAME,
            },
            timeout=5
        )
    except Exception:
        pass

def start_telemetry_loop():
    def loop():
        print('[Dispatch] Telemetry reader started.')
        while True:
            t = get_telemetry()
            if t:
                push_telemetry(t)
                # Feed live telemetry to the assistant for context-aware responses
                try:
                    import assistant
                    assistant.state.update_telemetry(t)
                except Exception:
                    pass
            time.sleep(2)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()

if __name__ == '__main__':
    print('[Telemetry] Testing — make sure ATS is running...')
    while True:
        t = get_telemetry()
        if t:
            print(f"Speed: {t['speed_kmh']} km/h | RPM: {t['rpm']} | Gear: {t['gear']} | Fuel: {t['fuel_pct']}%")
        else:
            print('[Telemetry] No data')
        time.sleep(1)