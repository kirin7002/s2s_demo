"""One bounded real-provider audio test; never synthesizes function calls."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import ssl
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp import ClientSession, CookieJar, WSMsgType
from gateway.config import Config
from scripts.stackchan_hardware import private_json, request_local


def battery_sample_valid(result):
    power = result.get('state', {}).get('power', {})
    age = power.get('sample_age_ms')
    return (result.get('online') is True and result.get('fresh') is True and result.get('simulated') is False
            and power.get('available') is True and power.get('vbus_voltage_mv') == 0
            and type(age) in (int, float) and 0 <= age <= 5000)


async def verify(directory, hardware_file, audio_file, report_name, expect_action=False, require_battery=False):
    output = directory / report_name
    if output.exists() or Path(report_name).name != report_name:
        raise ValueError('Existing report must be preserved')
    hardware = json.loads(hardware_file.read_text())
    config = Config.from_env()
    status_config = {'control_socket': str(directory / 'control.sock')}
    with wave.open(str(audio_file), 'rb') as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
            raise ValueError('Expected mono PCM16 16k audio')
        pcm = audio.readframes(audio.getnframes())
    if not 0 < len(pcm) <= 15 * 32000:
        raise ValueError('Input must be at most 15 seconds')
    report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'real_provider': True,
        'input_source': 'local_os_synthesized_test_speech_not_phone_microphone',
        'input_audio_seconds': len(pcm) / 32000, 'provider_model': config.model,
        'browser_audio_bytes': 0, 'transcripts': [], 'events': {}, 'passed': False,
        'function_call_injection': False, 'maximum_wall_seconds': 45}
    report['expect_action'] = expect_action
    report['require_battery'] = require_battery
    tls = ssl.create_default_context(cafile=hardware['ca_certificate_file'])
    origin = hardware['origin'].lower()
    base = f"https://127.0.0.1:{hardware['port']}"
    async def status():
        return (await request_local(status_config, {'op': 'status'}))['result']
    socket, pump = None, None
    try:
        async with asyncio.timeout(45):
            initial = await status()
            assert initial['mode'] == 'real_provider' and initial['device_connected']
            assert initial['simulated'] is False and not initial['active_sessions'] and not initial['control_session_active']
            report['initial_status'] = initial
            if require_battery:
                before = await request_local(status_config, {'op': 'get_state'})
                report['before_power_state'] = before
                if not before.get('ok') or not battery_sample_valid(before['result']):
                    raise ValueError('Fresh battery-only state required before a paid call')
            async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
                async with client.post(base + '/api/login', ssl=tls, headers={'Origin': origin},
                    json={'password': config.demo_password}) as response:
                    assert response.status == 200
                async with client.post(base + '/api/stackchan/bind', ssl=tls, headers={'Origin': origin},
                    json={'device_id': hardware['device_id']}) as response:
                    assert response.status == 200
                    bound = await response.json()
                    assert bound['online'] and bound['simulated'] is False
                socket = await client.ws_connect(base + '/ws', ssl=tls, headers={'Origin': origin})
                async def audio_pump():
                    # Same 100ms PCM packets as the browser; add a short quiet tail.
                    data = pcm + bytes(32000 * 3)
                    for start in range(0, len(data), 3200):
                        await socket.send_bytes(data[start:start + 3200])
                        await asyncio.sleep(.1)
                async for packet in socket:
                    if packet.type == WSMsgType.BINARY:
                        report['browser_audio_bytes'] += len(packet.data)
                        continue
                    if packet.type != WSMsgType.TEXT:
                        raise RuntimeError('Gateway closed before test completed')
                    event = json.loads(packet.data)
                    kind = event['type']
                    report['events'][kind] = report['events'].get(kind, 0) + 1
                    if kind == 'ready':
                        pump = asyncio.create_task(audio_pump())
                    elif kind == 'error':
                        report['gateway_error'] = {k: event.get(k) for k in ('code', 'stage', 'ws_close_code')}
                        raise RuntimeError('Gateway reported an error')
                    elif kind == 'transcript':
                        report['transcripts'].append({'role': event['role'], 'text': event['text']})
                    elif kind == 'turn_done':
                        current = await status()
                        action_done = not expect_action or (current['latest_completed_tool_outputs'] > 0
                            and current['nonterminal_commands'] == 0 and current['command_count'] > 0)
                        if action_done and current['latest_tool_output_count'] > 0 and report['browser_audio_bytes'] > 0 and report['events']['turn_done'] >= 2:
                            report['tool_status'] = current
                            report['passed'] = True
                            break
                    private_json(output, report)
                if pump:
                    pump.cancel()
                    await asyncio.gather(pump, return_exceptions=True)
                await socket.close()
                async with client.post(base + '/api/logout', ssl=tls, headers={'Origin': origin}) as response:
                    report['logout_status'] = response.status
    except Exception as error:
        report['passed'] = False
        report['failure'] = type(error).__name__
    finally:
        if pump:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        if socket and not socket.closed:
            await socket.close()
        deadline = time.monotonic() + 5
        while True:
            final = await status()
            if (not final['active_sessions'] and not final['control_session_active']) or time.monotonic() >= deadline:
                break
            await asyncio.sleep(.05)
        report['final_status'] = final
        report['control_released'] = not final['active_sessions'] and not final['control_session_active']
        if require_battery:
            report['after_power_state'] = await request_local(status_config, {'op': 'get_state'})
            report['battery_verified_before_and_after'] = bool(report.get('before_power_state', {}).get('ok')
                and battery_sample_valid(report['before_power_state']['result'])
                and report['after_power_state'].get('ok') and battery_sample_valid(report['after_power_state']['result']))
            report['passed'] = report['passed'] and report['battery_verified_before_and_after']
        report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        private_json(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True)
    parser.add_argument('--directory', type=Path, default=ROOT / '.run/s2s-live')
    parser.add_argument('--hardware-config', type=Path, default=ROOT / '.run/stackchan-hardware/server-config.json')
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--report-name', required=True)
    parser.add_argument('--expect-action', action='store_true')
    parser.add_argument('--require-battery', action='store_true')
    args = parser.parse_args()
    report = asyncio.run(verify(args.directory, args.hardware_config, args.audio, args.report_name, args.expect_action, args.require_battery))
    print(json.dumps({k: report.get(k) for k in ('passed', 'failure', 'gateway_error', 'browser_audio_bytes', 'control_released')}, ensure_ascii=False))
    return int(not report['passed'] or not report['control_released'])


if __name__ == '__main__':
    raise SystemExit(main())
