import asyncio
import edge_tts
import sounddevice as sd
import soundfile as sf
import tempfile
import os
import threading

VOICE = "en-US-GuyNeural"  # professional male voice, sounds like a dispatcher

def generate_and_play(message: str):
    """Generate TTS audio and play it through speakers."""
    thread = threading.Thread(target=_run_dispatch, args=(message,), daemon=True)
    thread.start()

def _run_dispatch(message: str):
    asyncio.run(_speak(message))

async def _speak(message: str):
    tmp = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
    tmp.close()

    try:
        communicate = edge_tts.Communicate(message, VOICE)
        await communicate.save(tmp.name)

        data, samplerate = sf.read(tmp.name)
        sd.play(data, samplerate)
        sd.wait()
    except Exception as e:
        print(f'[Dispatch] Voice error: {e}')
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)

def build_dispatch_messages(old_snapshot: dict, new_snapshot: dict) -> list:
    """Compare two snapshots and generate dispatch messages for what changed."""
    messages = []

    if not old_snapshot:
        return messages

    old_drivers = {d['id']: d for d in old_snapshot.get('drivers', [])}
    new_drivers = {d['id']: d for d in new_snapshot.get('drivers', [])}

    for driver_id, new_driver in new_drivers.items():
        old_driver = old_drivers.get(driver_id)
        if not old_driver:
            continue

        old_state = old_driver.get('state', 0)
        new_state = new_driver.get('state', 0)
        current_city = new_driver.get('current_city', '').replace('_', ' ').title()
        hometown = new_driver.get('hometown', '').replace('_', ' ').title()

        # Driver just went on a job
        if old_state != 2 and new_state == 2:
            messages.append(
                f"Dispatch to {driver_id.replace('driver.', 'Driver ')}: "
                f"Departing {hometown}, en route. Have a safe run."
            )

        # Driver just completed a job / went idle
        elif old_state == 2 and new_state != 2:
            messages.append(
                f"{driver_id.replace('driver.', 'Driver ')} has arrived in {current_city}. "
                f"Job complete. Standing by for next assignment."
            )

    # Check for new jobs in job history
    old_job_count = len(old_snapshot.get('jobs', []))
    new_job_count = len(new_snapshot.get('jobs', []))

    if new_job_count > old_job_count:
        new_jobs = new_snapshot['jobs'][:new_job_count - old_job_count]
        for job in new_jobs:
            if job.get('revenue', 0) > 0 and job.get('source_city') and job.get('destination_city'):
                source = job['source_city'].replace('_', ' ').title()
                dest = job['destination_city'].replace('_', ' ').title()
                revenue = job['revenue']
                cargo = job.get('cargo', 'cargo').replace('_', ' ')
                messages.append(
                    f"Job logged: {source} to {dest}, hauling {cargo}. "
                    f"Revenue: {revenue:,} dollars."
                )

    return messages