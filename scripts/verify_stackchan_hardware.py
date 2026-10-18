"""One-shot no-head hardware checks via the private TLS harness RPC.

Default prints the plan only. Use --run only after the device coordinator says
USB probing/reflashing is finished. No serial access or real model is used.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.stackchan_hardware import DEFAULT_DIRECTORY, private_json, request_local

EXPRESSIONS = ['neutral', 'happy', 'laughing', 'scared', 'sad', 'surprised', 'thinking']
NEUTRAL_OFF = {'duration_ms': 300, 'frames': [{'at_ms': 0,
    'screen': {'page': 'face', 'expression': 'neutral'},
    'light': {'rgb': [0, 0, 0], 'brightness': 0, 'fade_ms': 0}}]}
PLAN = ['real_device_capabilities'] + ['expression_' + x for x in EXPRESSIONS] + [
    'chinese_text', 'rgb_red', 'rgb_green', 'rgb_blue', 'rgb_white', 'sequence_with_pauses',
    'provider_call_id_deduplication', 'busy_rejection_and_stop', 'manual_interrupt',
    'speech_interrupt', 'disconnect_offline_rejection_and_reconnect', 'post_reconnect_execution']


class CheckFailed(Exception):
    pass


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


def no_head(arguments):
    return isinstance(arguments, dict) and all('head' not in frame for frame in arguments.get('frames', []))


class Suite:
    def __init__(self, config, report_file, expected_simulated=False):
        self.config, self.report_file = config, Path(report_file)
        self.expected_simulated = expected_simulated
        self.report = {'mode': 'real_wss_device_with_mock_qwen' if not expected_simulated else 'isolated_simulator_selftest',
                       'started_utc': datetime.now(timezone.utc).isoformat(), 'host': config['host'], 'port': config['port'],
                       'head_frames_sent': 0, 'servo_ready_enabled': False, 'real_model_calls': 0, 'cases': [],
                       'command_ids': [], 'cleanup': {}}

    def save(self):
        private_json(self.report_file, self.report)

    async def rpc(self, operation, **fields):
        response = await request_local(self.config, {'op': operation, **fields})
        require(response.get('ok') is True, 'LOCAL_RPC_REJECTED')
        return response['result']

    async def tool(self, name, arguments, duplicate=False):
        require(no_head(arguments), 'HEAD_FRAME_FORBIDDEN')
        values = await self.rpc('tool', calls=[{'name': name, 'arguments': arguments}], duplicate=duplicate)
        return values[0]

    def physical(self, result):
        require(result.get('simulated') is self.expected_simulated, 'UNEXPECTED_DEVICE_SIMULATION_FLAG')

    async def scene(self, arguments, duplicate=False):
        require(no_head(arguments), 'HEAD_FRAME_FORBIDDEN')
        result = await self.tool('stackchan_perform', arguments, duplicate)
        self.physical(result)
        require(result.get('ok') is True and result.get('status') in {'accepted', 'running', 'completed'},
                result.get('error_code', 'SCENE_NOT_ACCEPTED'))
        self.report['command_ids'].append(result['command_id'])
        return result

    async def command(self, command_id):
        result = await self.tool('stackchan_get_command', {'command_id': command_id})
        self.physical(result)
        return result

    async def completed(self, arguments, duplicate=False):
        accepted = await self.scene(arguments, duplicate)
        await asyncio.sleep(arguments['duration_ms'] / 1000 + .12)
        result = await self.command(accepted['command_id'])
        require(result.get('status') == 'completed' and result.get('fresh') is True,
                result.get('error_code', 'COMPLETION_NOT_CONFIRMED'))
        return {'command_id': accepted['command_id'], 'status': 'completed',
                'fresh': True, 'simulated': result['simulated']}

    async def case(self, name, action):
        try:
            evidence = await action()
            self.report['cases'].append({'name': name, 'passed': True, 'evidence': evidence})
        except Exception as error:
            code = str(error) if isinstance(error, CheckFailed) else 'LOCAL_CHECK_ERROR'
            self.report['cases'].append({'name': name, 'passed': False, 'error_code': code})
            # Do not let a failed non-moving scene block subsequent cases.
            try:
                await self.tool('stackchan_stop', {})
            except Exception:
                pass
        self.save()

    async def capabilities(self):
        state = await self.rpc('status')
        require(state['device_connected'], 'DEVICE_OFFLINE')
        require(state['motion_enabled'] is False, 'SERVO_GATE_MUST_REMAIN_OFF')
        require(state['voice_session_active'] is False and state['control_session_active'] is False,
                'EXISTING_CONTROL_SESSION')
        result = await self.tool('stackchan_get_state', {})
        self.physical(result)
        require(result.get('online') and result.get('fresh'), 'STATE_NOT_FRESH')
        caps = result['capabilities']
        require(set(EXPRESSIONS) <= set(caps.get('screen', {}).get('expressions', [])), 'MISSING_EXPRESSIONS')
        require(caps.get('light', {}).get('enabled') is True, 'LIGHT_DISABLED')
        return {'online': True, 'fresh': True, 'simulated': result['simulated'],
                'head_enabled': caps.get('head', {}).get('enabled'),
                'head_calibrated': caps.get('head', {}).get('calibrated'), 'expressions': EXPRESSIONS}

    async def deduplication(self):
        before = await self.rpc('status')
        evidence = await self.completed({'duration_ms': 250, 'frames': [{'at_ms': 0,
            'screen': {'page': 'face', 'expression': 'happy'}}]}, duplicate=True)
        after = await self.rpc('status')
        require(after['command_count'] == before['command_count'] + 1, 'DUPLICATE_COMMAND_DISPATCH')
        return {**evidence, 'scope': 'provider_call_id', 'device_commands_added': 1}

    async def busy_and_stop(self):
        active = await self.scene({'duration_ms': 3000, 'frames': [{'at_ms': 0,
            'screen': {'page': 'face', 'expression': 'thinking'}}]})
        rejected = await self.tool('stackchan_perform', NEUTRAL_OFF)
        self.physical(rejected)
        require(rejected.get('status') == 'rejected' and rejected.get('error_code') == 'BUSY', 'BUSY_NOT_REJECTED')
        stopped = await self.tool('stackchan_stop', {})
        self.physical(stopped)
        require(stopped.get('status') == 'stopped' and stopped.get('ok'), 'STOP_NOT_CONFIRMED')
        previous = await self.command(active['command_id'])
        require(previous.get('status') == 'cancelled' and previous.get('fresh'), 'ACTIVE_COMMAND_NOT_CANCELLED')
        return {'busy_error': 'BUSY', 'stop': 'stopped', 'prior_command': 'cancelled',
                'simulated': previous['simulated']}

    async def interrupted(self, operation):
        active = await self.scene({'duration_ms': 3000, 'frames': [{'at_ms': 0,
            'screen': {'page': 'face', 'expression': 'surprised'}}]})
        before = (await self.rpc('status'))['voice_events'].get('interrupted', 0)
        await self.rpc(operation)
        deadline = time.monotonic() + 4
        while (await self.rpc('status'))['voice_events'].get('interrupted', 0) <= before:
            require(time.monotonic() < deadline, 'INTERRUPT_EVENT_NOT_OBSERVED')
            await asyncio.sleep(.02)
        previous = await self.command(active['command_id'])
        require(previous.get('status') == 'cancelled' and previous.get('fresh'), 'INTERRUPT_NOT_CANCELLED')
        return {'trigger': operation, 'prior_command': 'cancelled', 'fresh': True,
                'simulated': previous['simulated'], 'microphone_used': False}

    async def reconnect(self):
        active = await self.scene({'duration_ms': 3000, 'frames': [{'at_ms': 0,
            'screen': {'page': 'face', 'expression': 'thinking'}}]})
        before = await self.rpc('status')
        await self.rpc('disconnect')
        offline = await self.tool('stackchan_perform', NEUTRAL_OFF)
        require(offline.get('ok') is False and offline.get('error_code') == 'DEVICE_OFFLINE', 'OFFLINE_NOT_REJECTED')
        deadline = time.monotonic() + 30
        while True:
            after = await self.rpc('status')
            if after['device_connected'] and after['connection_id'] != before['connection_id']:
                break
            require(time.monotonic() < deadline, 'DEVICE_DID_NOT_RECONNECT')
            await asyncio.sleep(.15)
        require(after['simulated'] is self.expected_simulated, 'UNEXPECTED_RECONNECTED_DEVICE')
        require(after['command_count'] == before['command_count'], 'ACTION_REPLAYED_ON_RECONNECT')
        previous = await self.command(active['command_id'])
        require(previous.get('status') == 'unknown', 'CLEARED_HISTORY_NOT_UNKNOWN')
        return {'offline_error': 'DEVICE_OFFLINE', 'new_connection_id': True,
                'command_replay': False, 'prior_command': 'unknown', 'simulated': after['simulated']}

    async def run(self):
        self.save()
        try:
            # Refuse to act if no real device or another operator is controlling it.
            evidence = await self.capabilities()
            self.report['cases'].append({'name': 'real_device_capabilities', 'passed': True, 'evidence': evidence})
            self.save()
            for expression in EXPRESSIONS:
                arguments = {'duration_ms': 450, 'frames': [{'at_ms': 0,
                    'screen': {'page': 'face', 'expression': expression}}]}
                await self.case('expression_' + expression, lambda args=arguments: self.completed(args))
            await self.case('chinese_text', lambda: self.completed({'duration_ms': 650, 'frames': [{'at_ms': 0,
                'screen': {'page': 'text', 'text': '你好，StackChan！中文显示联合测试。'}}]}))
            for name, rgb in [('red', [64, 0, 0]), ('green', [0, 64, 0]), ('blue', [0, 0, 64]), ('white', [32, 32, 32])]:
                arguments = {'duration_ms': 450, 'frames': [{'at_ms': 0,
                    'light': {'rgb': rgb, 'brightness': .12, 'fade_ms': 100}}]}
                await self.case('rgb_' + name, lambda args=arguments: self.completed(args))
            await self.case('sequence_with_pauses', lambda: self.completed({'duration_ms': 2400, 'frames': [
                {'at_ms': 0, 'screen': {'page': 'face', 'expression': 'happy'},
                 'light': {'rgb': [24, 0, 0], 'brightness': .1, 'fade_ms': 100}},
                {'at_ms': 800, 'screen': {'page': 'face', 'expression': 'neutral'},
                 'light': {'rgb': [0, 0, 0], 'brightness': 0, 'fade_ms': 0}},
                {'at_ms': 1600, 'screen': {'page': 'face', 'expression': 'thinking'},
                 'light': {'rgb': [0, 0, 24], 'brightness': .1, 'fade_ms': 100}}]}))
            await self.case('provider_call_id_deduplication', self.deduplication)
            await self.case('busy_rejection_and_stop', self.busy_and_stop)
            await self.case('manual_interrupt', lambda: self.interrupted('interrupt'))
            await self.case('speech_interrupt', lambda: self.interrupted('speech_interrupt'))
            await self.case('disconnect_offline_rejection_and_reconnect', self.reconnect)
            await self.case('post_reconnect_execution', lambda: self.completed(NEUTRAL_OFF))
        except Exception as error:
            code = str(error) if isinstance(error, CheckFailed) else 'LOCAL_SUITE_ERROR'
            self.report['fatal_error'] = code
        finally:
            try:
                state = await self.rpc('status')
                if state['device_connected']:
                    await self.tool('stackchan_stop', {})
                    self.report['cleanup']['neutral_and_lights_off'] = await self.completed(NEUTRAL_OFF)
                else:
                    self.report['cleanup']['neutral_and_lights_off'] = 'unconfirmed_device_offline'
            except Exception:
                self.report['cleanup']['neutral_and_lights_off'] = 'unconfirmed'
            try:
                await self.rpc('close_voice')
                deadline = time.monotonic() + 5
                while True:
                    state = await self.rpc('status')
                    if not state['voice_session_active'] and not state['control_session_active']:
                        break
                    require(time.monotonic() < deadline, 'SESSION_NOT_RELEASED')
                    await asyncio.sleep(.03)
                require(state['motion_enabled'] is False, 'SERVO_GATE_CHANGED')
                self.report['cleanup'].update(voice_closed=True, control_released=True,
                                              motion_enabled=False, gateway_running=True)
            except Exception:
                self.report['cleanup']['control_released'] = False
            self.report['passed'] = sum(case['passed'] for case in self.report['cases'])
            self.report['failed'] = sum(not case['passed'] for case in self.report['cases'])
            self.report['finished_utc'] = datetime.now(timezone.utc).isoformat()
            self.save()
        return self.report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--directory', type=Path, default=DEFAULT_DIRECTORY)
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({'mode': 'plan_only', 'head_frames': 0, 'cases': PLAN}, ensure_ascii=False))
        return 0
    config = json.loads((args.directory / 'server-config.json').read_text())
    report_file = args.directory / 'hardware-no-head-results.json'
    report = asyncio.run(Suite(config, report_file).run())
    print(json.dumps({'passed': report['passed'], 'failed': report['failed'],
                      'fatal_error': report.get('fatal_error'), 'report_file': str(report_file)}, ensure_ascii=False))
    return int(bool(report.get('fatal_error') or report['failed'] or not report['cleanup'].get('control_released')))


if __name__ == '__main__':
    raise SystemExit(main())
