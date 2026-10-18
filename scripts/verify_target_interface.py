"""Explicit local mock-model -> real gateway -> compiled C++ simulator checks.

No provider key, real model request, physical robot or persistent service is used.
"""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway.server import DEVICES
from scripts.stackchan_hardware import private_json
from tests.test_gateway import Harness


async def verify(device_project):
    source = device_project / 'simulator/device.py'
    binary = device_project / 'firmware/.pio/build/simulator/program'
    if not source.is_file() or not binary.is_file():
        raise ValueError('Build the device repository simulator environment first')
    spec = importlib.util.spec_from_file_location('target_native_transport', source)
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    token = secrets.token_hex(24)
    harness = await Harness(stackchan_enabled=True, stackchan_device_id='target_simulator',
                            stackchan_device_token=token, device_ack_seconds=1).open()
    device = browser = None
    checks, results, counter = [], [], 0
    try:
        origin = {'Origin': harness.cfg.origin}
        response = await harness.client.post('/api/stackchan/bind', headers=origin,
                                             json={'device_id': 'target_simulator'})
        assert response.status == 200
        url = str(harness.client.make_url('/device/v1/ws')).replace('http://', 'ws://', 1)
        device = asyncio.create_task(transport.run_connection(url, token, binary=binary))
        hub = harness.app[DEVICES]
        async with asyncio.timeout(5):
            while hub.connection is None:
                await asyncio.sleep(.01)
        browser = await harness.socket()
        assert (await browser.receive_json(timeout=5))['type'] == 'ready'
        registration = await harness.events.get()
        target = next(t['function'] for t in registration['session']['tools']
                      if t['function']['name'] == 'stackchan_perform_targets')
        fields = target['parameters']['properties']['steps']['items']['properties']
        assert 'yaw_deg' in fields and not any(k in fields for k in ('head', 'screen', 'light'))
        checks.append('normal voice registers flat target fields')
        provider = harness.upstreams[-1]

        async def new_turn():
            await provider.send_json({'type': 'input_audio_buffer.speech_started'})
            await provider.send_json({'type': 'input_audio_buffer.speech_stopped'})

        async def call(name, arguments):
            nonlocal counter
            counter += 1
            identifier = f'local_{counter}'
            item = {'type': 'function_call', 'call_id': identifier, 'name': name,
                    'arguments': json.dumps(arguments, ensure_ascii=False)}
            await provider.send_json({'type': 'response.created', 'response': {'id': identifier}})
            await provider.send_json({'type': 'response.output_item.added', 'response_id': identifier,
                                      'output_index': 0, 'item': item})
            await provider.send_json({'type': 'response.function_call_arguments.done', 'response_id': identifier,
                                      'output_index': 0, **{k: item[k] for k in ('call_id', 'name', 'arguments')}})
            await provider.send_json({'type': 'response.done', 'response': {
                'id': identifier, 'status': 'completed', 'output': [item]}})
            async with asyncio.timeout(5):
                while True:
                    event = await harness.events.get()
                    if event['type'] == 'conversation.item.create' and event['item']['call_id'] == identifier:
                        return json.loads(event['item']['output'])

        cases = [
            ('flat_combination_and_pause', {'steps': [
                {'yaw_deg': 6, 'pitch_deg': -3, 'expression': 'laughing',
                 'rgb': [0, 255, 0], 'brightness': .08, 'hold_ms': 500}, {'hold_ms': 500}]}),
            ('observed_nested_goal_plus_pause', {'steps': [
                {'head': {'yaw_deg': 0, 'pitch_deg': 0}, 'screen': {'page': 'face', 'expression': 'happy'}},
                {'hold_ms': 1000}]}),
            ('free_flat_round_trip', {'steps': [{'yaw_deg': -8}, {'yaw_deg': 8, 'hold_ms': 200}],
                                      'return_to_neutral': True}),
        ]
        for name, request in cases:
            await new_turn()
            before = len(hub.records)
            accepted = await call('stackchan_perform_targets', request)
            assert accepted['ok'] and accepted['status'] in ('accepted', 'running') and accepted['simulated'] is True, {
                'case': name, 'result': accepted}
            assert len(hub.records) == before + 1
            record = hub.records[accepted['command_id']]
            await asyncio.sleep(accepted['effective_duration_ms'] / 1000 + .15)
            completed = await call('stackchan_get_command', {'command_id': accepted['command_id']})
            assert completed['status'] == 'completed' and completed['fresh'] and completed['simulated'] is True
            assert completed['merged_holds'] == accepted['merged_holds']
            state = await call('stackchan_get_state', {})
            assert state['fresh'] and state['simulated'] is True
            if name == 'free_flat_round_trip':
                assert abs(state['state']['yaw_deg']) < .01 and abs(state['state']['pitch_deg']) < .01
            results.append({'case': name, 'status': completed['status'],
                            'effective_args': record['effective_args'], 'input_step_mapping': accepted['input_step_mapping'],
                            'merged_holds': accepted['merged_holds'], 'pose': state['state']})
            checks.extend([name + ': one atomic dispatch', name + ': real-clock C++ completed'])
        for request in ({'steps': [{'hold_ms': 1000}]}, {'steps': [{'expression': 'laughing'}, {'hold_ms': 8000}]}):
            await new_turn()
            before = len(hub.records)
            rejected = await call('stackchan_perform_targets', request)
            assert rejected['ok'] is False and not rejected.get('command_id') and len(hub.records) == before
        checks.append('first pause and excessive total duration reject before dispatch')
        return {'mode': 'scripted_mock_model_and_compiled_cpp_simulator', 'real_model_calls': 0,
                'physical_device_commands': 0, 'passed': len(checks), 'checks': checks, 'cases': results}
    finally:
        if browser:
            await browser.close()
        if device:
            device.cancel()
            await asyncio.gather(device, return_exceptions=True)
        await harness.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device-project', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.report and args.report.exists():
        raise ValueError('Preserve existing reports')
    result = asyncio.run(verify(args.device_project.resolve()))
    if args.report:
        private_json(args.report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
