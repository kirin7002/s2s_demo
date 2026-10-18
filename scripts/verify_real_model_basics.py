"""Opt-in synthetic speech -> real model -> real robot, one bounded suite.

Only natural-language PCM is sent to the normal voice endpoint. No injected
functions, preset action frames, physical retries or firmware changes.
"""
import argparse
import asyncio
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import shutil
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp import ClientSession, CookieJar, WSMsgType
from gateway.config import Config
from scripts.stackchan_hardware import private_json, request_local


class ToolFailure(Exception):
    pass


def events_since(path, offset):
    with path.open('rb') as stream:
        stream.seek(offset)
        lines = stream.read().splitlines()
    result = []
    for line in lines:
        try:
            result.append(json.loads(line))
        except ValueError:
            pass  # An append may still be in flight; the next read starts at the same offset.
    return result


def color(frame):
    rgb = frame['rgb']
    if frame['brightness'] <= 0 or max(rgb) == 0:
        return 'off'
    axis = rgb.index(max(rgb))
    return ('red', 'green', 'blue')[axis] if all(rgb[i] == 0 for i in range(3) if i != axis) else 'mixed'


def prior_diagnostic(event, prior_commands):
    return bool(event.get('command_id') in prior_commands)


def one_parameter_correction(errors, *, execution_started=False):
    """One explicit preflight correction; never retry an accepted physical fault."""
    if execution_started or len(errors) != 1 or errors[0].get('command_id'):
        return False
    error = errors[0]
    details = error.get('details', {})
    if error.get('error_code') == 'INVALID_ARGUMENT':
        return bool(details.get('field')) and details.get('reason') in {
            'type', 'required', 'additionalProperties', 'anyOf', 'oneOf', 'enum', 'const',
            'minimum', 'maximum', 'minItems', 'maxItems', 'minProperties',
            'minLength', 'maxLength', 'steps_json_encoding'}
    return error.get('error_code') == 'OUT_OF_RANGE' and details.get('reason') in {
        'speed', 'target', 'duration', 'frame_count', 'move_duration', 'step_count',
        'generated_duration', 'generated_frame_count', 'generated_move_duration'}


def correction_evidence(audits, diagnostics):
    """Budget one model-authored correction, keeping the original rejection.

    Audit and browser diagnostics describe the same call; do not count both.
    Diagnostics can arrive first, but cannot conceal a physical failure.
    """
    errors = [e for e in audits if e.get('error_code') or
              (e.get('stage') == 'device_command_status' and e.get('status') in ('failed', 'rejected'))]
    evidence = errors or diagnostics
    if not evidence:
        return {'errors': [], 'allowed': False, 'attempts': 0, 'rejection': None}
    first = next((i for i, e in enumerate(audits) if e is evidence[0]), len(audits))
    execution_started = any(e.get('command_id') for e in audits[:first])
    attempts = sum(e.get('stage') == 'perform_result' for e in audits[first + 1:]) if errors else 0
    allowed = (one_parameter_correction(evidence, execution_started=execution_started)
               and (not diagnostics or one_parameter_correction(diagnostics)) and attempts <= 1)
    rejection = evidence[0] if one_parameter_correction([evidence[0]], execution_started=execution_started) else None
    return {'errors': evidence, 'allowed': allowed, 'attempts': attempts, 'rejection': rejection}


async def run_isolated(args, cases):
    """One authenticated voice/provider connection per intent, fully closed first."""
    output = args.directory / args.report_name
    report = {'started_utc': datetime.now(timezone.utc).isoformat(),
              'input_source': 'macOS_synthetic_Chinese_not_user_microphone',
              'function_call_injection': False, 'isolated_provider_session_per_case': True,
              'real_provider_connections_planned': len(cases), 'cases': [], 'provider_audit': [], 'passed': False}
    private_json(output, report)
    head_failed = False
    for case in cases:
        if head_failed and 'head_axis' in case['expected']:
            report['cases'].append({'id': case['id'], 'passed': False,
                                   'not_executed': 'Earlier physical head failure; no retry'})
            continue
        child = copy.copy(args)
        child.case_ids = [case['id']]
        child.report_name = Path(args.report_name).stem + '-' + case['id'] + '.json'
        result = await run(child)
        report['cases'].extend([{**item, 'isolated_report': child.report_name} for item in result['cases']])
        report['provider_audit'].extend(result.get('provider_audit', []))
        report['final_status'] = result.get('final_status')
        report['final_device_state'] = result.get('final_device_state')
        report['control_released'] = result.get('control_released')
        outputs = [o for p in result.get('provider_audit', []) for o in p.get('outputs', [])]
        evidence = [e for item in result['cases'] for e in item.get('failure_evidence', [])]
        head_failed = any(e.get('error_code') in {'MOTION_TIMEOUT', 'SCHEDULER_OVERRUN',
                          'HARDWARE_ERROR', 'ACK_TIMEOUT', 'DEVICE_OFFLINE'} for e in outputs + evidence)
        report['real_provider_connections_actual'] = len(report['provider_audit'])
        private_json(output, report)
        if not result.get('control_released') or result.get('failure'):
            report['failure'] = result.get('failure', 'Control release unconfirmed')
            break
    report['passed'] = len(report['cases']) == len(cases) and all(c.get('passed') for c in report['cases'])
    report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    private_json(output, report)
    return report


def assess(expected, summaries, pose):
    heads = [f for s in summaries for f in s.get('head_frames', [])]
    screens = [f for s in summaries for f in s.get('screen_frames', [])]
    lights = [f for s in summaries for f in s.get('light_frames', [])]
    checks = {'model_chose_perform': bool(summaries)}
    if 'head_axis' in expected:
        axis = expected['head_axis'] + '_deg'
        values = [f[axis] for f in heads]
        checks['requested_axis_moves'] = bool(values and max(values) - min(values) >= .5)
        if expected['head_axis'] == 'yaw':
            checks['both_yaw_sides'] = bool(values and min(values) < -.25 and max(values) > .25)
        if expected.get('cycles'):
            signs = []
            for value in values:
                sign = 1 if value > 1.5 else -1 if value < -1.5 else 0
                if not signs or signs[-1] != sign:
                    signs.append(sign)
            returns = sum(signs[i] == 0 and signs[i - 1] != 0 for i in range(1, len(signs)))
            cycles = max(returns, min(signs.count(1), signs.count(-1)))
            checks['requested_cycles'] = cycles >= expected['cycles']
    if expected.get('returns_to_center'):
        # Natural-language "face forward" allows the advertised 1.5 degree
        # tolerance; a software-zero request would require exact zero targets.
        checks['planned_center'] = bool(heads and all(abs(heads[-1][k]) <= 1.5 for k in ('yaw_deg', 'pitch_deg')))
        checks['measured_center'] = all(type(pose.get(k)) in (int, float) and abs(pose[k]) <= 1.5
                                        for k in ('yaw_deg', 'pitch_deg'))
    if 'expression' in expected:
        checks['expression'] = any(f.get('expression') == expected['expression'] for f in screens)
    if 'ends_expression' in expected:
        checks['ends_expression'] = bool(screens and screens[-1].get('expression') == expected['ends_expression'])
    if expected.get('ends_light_off'):
        checks['ends_light_off'] = bool(lights and color(lights[-1]) == 'off')
    if 'light_colors' in expected:
        checks['rgb_order'] = [color(f) for f in lights if color(f) != 'off'] == expected['light_colors']
        checks['half_second_holds'] = len(summaries) == 1 and len(lights) >= 4 and all(
            lights[i + 1]['at_ms'] - lights[i]['at_ms'] - lights[i]['fade_ms'] >= 500 for i in range(3))
    if 'light_color' in expected:
        checks['light_color'] = any(color(f) == expected['light_color'] for f in lights)
        checks['combined_same_frame_time'] = any(
            {f['at_ms'] for f in summary.get('head_frames', [])}
            & {f['at_ms'] for f in summary.get('screen_frames', []) if f.get('expression') == expected['expression']}
            & {f['at_ms'] for f in summary.get('light_frames', []) if color(f) == expected['light_color']}
            for summary in summaries)
    return checks


async def run(args):
    directory = args.directory
    output = directory / args.report_name
    if output.exists() or Path(args.report_name).name != args.report_name:
        raise ValueError('Preserve existing reports')
    cases = json.loads(args.cases.read_text())['cases']
    if args.case_ids:
        known = {case['id'] for case in cases}
        if len(set(args.case_ids)) != len(args.case_ids) or not set(args.case_ids) <= known:
            raise ValueError('Unknown or duplicate case id')
        cases = [case for case in cases if case['id'] in args.case_ids]
    if not 1 <= len(cases) <= 7:
        raise ValueError('Suite must have 1..7 cases')
    if len(cases) > 1:
        return await run_isolated(args, cases)
    audio_dir = directory / 'basic-synthetic-audio'
    audio_dir.mkdir(mode=0o700, exist_ok=True)
    pcm_cases = []
    for index, case in enumerate(cases):
        aiff, wav = audio_dir / f'{index}.aiff', audio_dir / f'{index}.wav'
        source = args.audio_directory / (case['id'] + '.wav') if args.audio_directory else None
        if source:
            shutil.copyfile(source, wav)
        else:
            subprocess.run(['say', '-v', 'Tingting', '-o', str(aiff), case['utterance']], check=True)
            subprocess.run(['afconvert', '-f', 'WAVE', '-d', 'LEI16@16000', '-c', '1', str(aiff), str(wav)], check=True)
            aiff.chmod(0o600)
        wav.chmod(0o600)
        with wave.open(str(wav)) as stream:
            assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (16000, 1, 2)
            pcm = stream.readframes(stream.getnframes())
        if not 0 < len(pcm) <= 20 * 32000 or not any(pcm):
            raise ValueError('Synthetic case is silent or exceeds input budget')
        pcm_cases.append(pcm)
    rpc_config = {'control_socket': str(directory / 'control.sock')}
    async def rpc(op):
        value = await request_local(rpc_config, {'op': op})
        if not value.get('ok'):
            raise RuntimeError('Private diagnostic unavailable')
        return value['result']
    status = await rpc('status')
    if status['active_sessions'] or status['control_session_active']:
        raise RuntimeError('User session active; leave it untouched')
    if not status['device_connected'] or status['simulated'] is not False:
        raise RuntimeError('Real device must be online')
    cfg = Config.from_env()
    device = json.loads((directory / 'device-config.json').read_text())
    audit_path = directory / 'events.jsonl'
    suite_offset = audit_path.stat().st_size
    report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'provider_model': status['model'],
              'input_source': 'macOS_synthetic_Chinese_not_user_microphone', 'function_call_injection': False,
              'real_provider_connections_planned': 1, 'initial_status': status, 'cases': [], 'passed': False}
    socket, pump = None, None
    try:
        async with asyncio.timeout(420), ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            base = 'http://localhost:8080'
            headers = {'Origin': base}
            async with client.post(base + '/api/login', headers=headers, json={'password': cfg.demo_password}) as response:
                assert response.status == 200
            async with client.post(base + '/api/stackchan/bind', headers=headers,
                                   json={'device_id': device['device_id']}) as response:
                assert response.status == 200
            # Recheck immediately before the single paid provider connection.
            current = await rpc('status')
            if current['active_sessions'] or current['control_session_active']:
                raise RuntimeError('User session began; do not connect')
            socket = await client.ws_connect(base + '/ws', headers=headers)
            async with asyncio.timeout(20):
                while (await socket.receive_json())['type'] != 'ready':
                    pass
            head_failed = False
            for case, pcm in zip(cases, pcm_cases):
                if head_failed and 'head_axis' in case['expected']:
                    report['cases'].append({'id': case['id'], 'passed': False,
                        'not_executed': 'Earlier head failure; no further motion'})
                    continue
                offset = audit_path.stat().st_size
                prior_commands = {e['command_id'] for e in events_since(audit_path, suite_offset)
                                  if e.get('command_id')}
                before = await rpc('status')
                initial_pose = (await rpc('get_state')).get('state', {})
                result = {'id': case['id'], 'expected': case['expected'], 'audio_input_seconds': len(pcm) / 32000,
                          'audio_output_bytes': 0, 'responses': [], 'input_transcripts': [],
                          'diagnostics': [], 'late_prior_diagnostics': [],
                          'parameter_correction_limit': 1, 'parameter_correction_attempts': 0,
                          'tool_call_start': len(before['latest_tool_call_names']), 'passed': False}
                result['initial_pose'] = {key: initial_pose.get(key) for key in ('yaw_deg', 'pitch_deg')}
                report['cases'].append(result)
                private_json(output, report)
                print(json.dumps({'case': case['id'], 'stage': 'real_model_input'}, ensure_ascii=False), flush=True)
                async def send_audio(data):
                    data += bytes(32000 * 2)
                    for start in range(0, len(data), 3200):
                        await socket.send_bytes(data[start:start + 3200])
                        await asyncio.sleep(.1)
                pump = asyncio.create_task(send_audio(pcm))
                last_message, last_done, text = time.monotonic(), False, ''
                try:
                    async with asyncio.timeout(55):
                        while True:
                            try:
                                packet = await socket.receive(timeout=.25)
                            except TimeoutError:
                                packet = None
                            if packet is not None:
                                last_message = time.monotonic()
                                if packet.type == WSMsgType.BINARY:
                                    result['audio_output_bytes'] += len(packet.data)
                                elif packet.type == WSMsgType.TEXT:
                                    event = json.loads(packet.data)
                                    if event['type'] == 'error':
                                        result['gateway_error'] = event.get('code')
                                        raise RuntimeError('Gateway error; no retry')
                                    if event['type'] == 'responding':
                                        last_done = False
                                        text = ''
                                    if event['type'] == 'transcript' and event['role'] == 'assistant':
                                        text += event['text']
                                    if event['type'] == 'transcript' and event['role'] == 'user':
                                        # This opt-in runner sends only its known synthetic fixtures.
                                        # The everyday runtime never logs user transcripts.
                                        result['input_transcripts'].append(event['text'])
                                    if event['type'] == 'tool_diagnostic':
                                        key = 'late_prior_diagnostics' if prior_diagnostic(event, prior_commands) else 'diagnostics'
                                        result[key].append(event)
                                    if event['type'] == 'turn_done':
                                        last_done = True
                                        if text:
                                            result['responses'].append({'text': text, 'characters': len(text)})
                                        text = ''
                                else:
                                    raise RuntimeError('Voice connection ended')
                            audits = events_since(audit_path, offset)
                            correction = correction_evidence(audits, result['diagnostics'])
                            if correction['allowed']:
                                result['parameter_rejections'] = [correction['rejection']]
                                result['parameter_correction_attempts'] = correction['attempts']
                            elif correction['errors']:
                                result['failure_evidence'] = correction['errors']
                                result['parameter_correction_attempts'] = correction['attempts']
                                raise ToolFailure('Tool failure; no automatic retry')
                            if pump.done() and last_done and time.monotonic() - last_message >= 1:
                                if correction['allowed'] and not correction['attempts'] and not result['responses']:
                                    continue  # Observe the bounded model continuation, not just the rejected tool turn.
                                now = await rpc('status')
                                if now['nonterminal_commands'] == 0 and now['latest_tool_output_count'] == len(now['latest_tool_call_names']):
                                    break
                    await pump
                    pump = None
                except ToolFailure:
                    if pump:
                        pump.cancel()
                        await asyncio.gather(pump, return_exceptions=True)
                        pump = None
                    # Each case owns its own provider socket. Close it on failure;
                    # no late continuation can be mistaken for the next intent.
                    await socket.close()
                    async with asyncio.timeout(8):
                        while True:
                            stopped = await rpc('get_state')
                            now = await rpc('status')
                            if (stopped.get('fresh') and not stopped.get('active_command_id')
                                    and not now['active_sessions'] and not now['control_session_active']):
                                break
                            await asyncio.sleep(.1)
                    result['stopped_after_failure'] = stopped
                    head_failed = any(e.get('error_code') in
                        {'MOTION_TIMEOUT', 'HARDWARE_ERROR', 'SCHEDULER_OVERRUN'}
                        for e in result.get('failure_evidence', []))
                audits = events_since(audit_path, offset)
                performed = [e for e in audits if e.get('stage') == 'perform_result' and e.get('command_id')]
                terminals = {e['command_id']: e for e in audits if e.get('stage') == 'device_command_status'
                             and e.get('status') in ('completed', 'failed', 'cancelled', 'rejected')}
                result['commands'] = [{'command_id': e['command_id'], 'summary': e['summary'],
                                       'requested_summary': e.get('requested_summary'),
                                       **{key: e[key] for key in ('timing_mode', 'timing_adjusted',
                                           'requested_duration_ms', 'effective_duration_ms', 'frame_timing',
                                           'source_tool', 'planning_mode', 'pace', 'planned_speed_deg_s', 'input_steps',
                                           'generated_frames', 'return_to_neutral', 'neutral_frame_added', 'step_frames',
                                           'inherited_axes', 'input_normalized', 'normalizations', 'received_steps_type',
                                           'normalized_steps_type') if key in e},
                                       'terminal': terminals.get(e['command_id'])} for e in performed]
                correction = correction_evidence(audits, result['diagnostics'])
                if correction['rejection']:
                    result['parameter_rejections'] = [correction['rejection']]
                    result['parameter_correction_attempts'] = correction['attempts']
                    result['parameter_correction_outcome'] = (
                        'completed' if correction['allowed'] and performed and all(
                            terminals.get(e['command_id'], {}).get('status') == 'completed' for e in performed)
                        else 'not_attempted' if not correction['attempts'] else 'failed_or_unconfirmed')
                heads = [f for e in performed for f in e['summary'].get('head_frames', [])]
                if heads:
                    result['return_target'] = {key: heads[-1][key] for key in ('yaw_deg', 'pitch_deg')}
                    result['return_target_is_exact_software_zero'] = all(heads[-1][k] == 0 for k in ('yaw_deg', 'pitch_deg'))
                    result['return_target_matches_initial_pose'] = all(
                        type(initial_pose.get(k)) in (int, float) and abs(heads[-1][k] - initial_pose[k]) <= .01
                        for k in ('yaw_deg', 'pitch_deg'))
                fresh = await rpc('get_state')
                result['final_device_state'] = fresh
                now = await rpc('status')
                result['tool_call_end'] = len(now['latest_tool_call_names'])
                result['model_tools'] = now['latest_tool_call_names'][result['tool_call_start']:result['tool_call_end']]
                result['checks'] = assess(case['expected'], [e['summary'] for e in performed], fresh.get('state', {}))
                result['checks']['all_commands_completed_real'] = bool(performed) and all(
                    terminals.get(e['command_id'], {}).get('status') == 'completed'
                    and terminals[e['command_id']].get('simulated') is False for e in performed)
                result['checks']['fresh_real_state'] = fresh.get('fresh') is True and fresh.get('simulated') is False
                if args.require_targets:
                    result['checks']['model_selected_free_targets'] = bool(performed) and all(
                        e.get('source_tool') == 'stackchan_perform_targets' for e in performed)
                    result['checks']['explicit_software_zero_target'] = not case['expected'].get('returns_to_center') or bool(
                        heads and all(heads[-1][key] == 0 for key in ('yaw_deg', 'pitch_deg')))
                result['checks']['short_spoken_reply'] = bool(result['responses']) and all(
                    r['characters'] <= 20 for r in result['responses'])
                result['audio_output_seconds'] = result['audio_output_bytes'] / 48000
                result['passed'] = not result.get('failure_evidence') and all(result['checks'].values())
                private_json(output, report)
                print(json.dumps({'case': case['id'], 'passed': result['passed'], 'checks': result['checks'],
                                  'audio_seconds': result['audio_output_seconds']}, ensure_ascii=False), flush=True)
            report['passed'] = all(case['passed'] for case in report['cases'])
    except Exception as error:
        report['failure'] = type(error).__name__
        if report['cases']:
            report['cases'][-1]['partial_audit'] = events_since(audit_path, offset)
    finally:
        if pump:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        if socket and not socket.closed:
            # Existing hangup releases control and stops remaining frames,
            # restoring idle display/lights; it does not issue a home movement.
            await socket.close()
        deadline = time.monotonic() + 8
        while True:
            final = await rpc('status')
            if not final['active_sessions'] and not final['control_session_active'] or time.monotonic() >= deadline:
                break
            await asyncio.sleep(.1)
        report['final_status'] = final
        report['control_released'] = not final['active_sessions'] and not final['control_session_active']
        report['final_device_state'] = await rpc('get_state')
        report['provider_audit'] = [e for e in events_since(audit_path, suite_offset)
                                  if e.get('stage') == 'provider_session_closed']
        report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        private_json(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Explicitly run paid model and physical robot')
    parser.add_argument('--directory', type=Path, default=ROOT / '.run/s2s-live')
    parser.add_argument('--cases', type=Path, default=ROOT / 'tests/fixtures/real-model-basic-cases.json')
    parser.add_argument('--case-ids', nargs='+', help='Explicitly authorized subset; preserves canonical order')
    parser.add_argument('--require-targets', action='store_true', help='Verify real model selected free-target tool')
    parser.add_argument('--audio-directory', type=Path, help='Reuse verified PCM16 mono 16k synthetic WAVs')
    parser.add_argument('--report-name', default='real-model-basic-results.json')
    args = parser.parse_args()
    if not args.run:
        print('Plan only: seven natural-language cases; --run connects the real model and moves the robot.')
        return 0
    report = asyncio.run(run(args))
    print(json.dumps({key: report.get(key) for key in ('passed', 'failure', 'control_released')}, ensure_ascii=False))
    return int(not report['passed'] or not report['control_released'])


if __name__ == '__main__':
    raise SystemExit(main())
