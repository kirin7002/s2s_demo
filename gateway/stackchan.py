"""Single-demo-device routing. No model connection or physical retry is made here."""
import asyncio
from collections import OrderedDict
import hashlib
import hmac
import json
import logging
import math
from pathlib import Path
import re
import secrets
import time

from aiohttp import WSMsgType, web
from jsonschema import Draft7Validator
from .action_audit import action_summary

LOGGER = logging.getLogger(__name__)

CONTRACT = Path(__file__).with_name('stackchan_contract')
TOOLS = json.loads((CONTRACT / 'tools.json').read_text())['tools']
PARAMETERS = {tool['name']: Draft7Validator(tool['inputSchema']) for tool in TOOLS}
COMMAND = Draft7Validator(json.loads((CONTRACT / 'command.schema.json').read_text()))
QWEN_TOOLS = [{'type': 'function', 'function': {'name': tool['name'],
               'description': tool['description'], 'parameters': tool['inputSchema']}} for tool in TOOLS]
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,64}')
TERMINAL = {'completed', 'failed', 'cancelled', 'rejected'}
STATUSES = TERMINAL | {'accepted', 'running'}


class DeviceError(Exception):
    def __init__(self, code, command_id=None):
        super().__init__(code)
        self.code, self.command_id = code, command_id

    def result(self):
        return {'ok': False, 'error_code': self.code,
                **({'command_id': self.command_id, 'status': 'unknown'} if self.command_id else {})}


def encode(message):
    try:
        value = json.dumps(message, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        size = len(value.encode('utf-8'))
    except (ValueError, TypeError, RecursionError):
        raise DeviceError('INVALID_ARGUMENT') from None
    if size > 8192:
        raise DeviceError('MESSAGE_TOO_LARGE')
    return value


def decode(text):
    if not isinstance(text, str) or len(text.encode('utf-8')) > 8192:
        raise DeviceError('MESSAGE_TOO_LARGE')
    try:
        value = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        raise DeviceError('INVALID_ARGUMENT') from None
    if not isinstance(value, dict):
        raise DeviceError('INVALID_ARGUMENT')
    return value


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_frames(arguments, capabilities):
    duration, frames = arguments['duration_ms'], arguments['frames']
    for value, key, default in [(len(frames), 'max_frames', 16), (duration, 'max_duration_ms', 8000)]:
        limit = capabilities.get(key, default)
        if type(limit) is not int or limit < 1:
            raise DeviceError('NOT_READY')
        if value > limit:
            raise DeviceError('OUT_OF_RANGE')
    last_at, ends, previous_head = -1, {}, None
    for frame in frames:
        at = frame['at_ms']
        if (last_at < 0 and at != 0) or at <= last_at or at >= duration:
            raise DeviceError('INVALID_ARGUMENT')
        last_at = at
        for channel in ('head', 'light', 'screen'):
            if channel not in frame:
                continue
            setting, capability = frame[channel], capabilities.get(channel, {})
            if not isinstance(capability, dict) or capability.get('enabled') is not True:
                raise DeviceError('MOTION_DISABLED' if channel == 'head' else 'UNSUPPORTED_CAPABILITY')
            transition = setting.get('move_ms', setting.get('fade_ms', 0))
            if at < ends.get(channel, 0) or at + transition > duration:
                raise DeviceError('INVALID_ARGUMENT')
            ends[channel] = at + transition
            if channel == 'head':
                if capability.get('calibrated') is not True:
                    raise DeviceError('NOT_CALIBRATED')
                for axis in ('yaw', 'pitch'):
                    bounds = capability.get(axis + '_range_deg')
                    if not (isinstance(bounds, list) and len(bounds) == 2
                            and all(number(x) for x in bounds) and bounds[0] <= bounds[1]):
                        raise DeviceError('NOT_READY')
                    if not bounds[0] <= setting[axis + '_deg'] <= bounds[1]:
                        raise DeviceError('OUT_OF_RANGE')
                speed = capability.get('max_speed_deg_s')
                if not number(speed) or speed <= 0:
                    raise DeviceError('NOT_READY')
                if previous_head and any(abs(setting[key] - previous_head[key]) / (transition / 1000) > speed
                                         for key in ('yaw_deg', 'pitch_deg')):
                    raise DeviceError('OUT_OF_RANGE')
                previous_head = setting
                # First transition is validated against actual pose by firmware.
            elif channel == 'screen':
                pages, expressions = capability.get('pages'), capability.get('expressions')
                if not isinstance(pages, list) or setting['page'] not in pages:
                    raise DeviceError('UNSUPPORTED_CAPABILITY')
                if setting['page'] == 'face' and (not isinstance(expressions, list)
                                                or setting['expression'] not in expressions):
                    raise DeviceError('UNSUPPORTED_CAPABILITY')


class Connection:
    def __init__(self, hub, socket, hello):
        self.hub, self.socket = hub, socket
        self.id, self.boot_id = secrets.token_hex(16), hello['device_boot_id']
        self.capabilities = hello['capabilities']
        self.writer = asyncio.Lock()
        self.waiters = {}
        self.observed_at = time.monotonic()

    @property
    def online(self):
        return self.hub.connection is self and not self.socket.closed

    async def send(self, message, guard=None):
        text = encode(message)
        async with self.writer:
            if not self.online:
                raise DeviceError('DEVICE_OFFLINE')
            if guard and not guard():
                raise DeviceError('STALE_CONTEXT')
            try:
                await asyncio.wait_for(self.socket.send_str(text), 5)
            except (TimeoutError, ConnectionError, RuntimeError):
                raise DeviceError('DEVICE_OFFLINE') from None

    async def request(self, key, message):
        future = asyncio.get_running_loop().create_future()
        self.waiters[key] = future
        try:
            await self.send(message)
            return await asyncio.wait_for(future, self.hub.config.device_ack_seconds)
        except TimeoutError:
            raise DeviceError('ACK_TIMEOUT') from None
        finally:
            self.waiters.pop(key, None)

    def resolve(self, key, value):
        future = self.waiters.get(key)
        if future and not future.done():
            future.set_result(value)

    def lost(self):
        for future in self.waiters.values():
            if not future.done():
                future.set_exception(DeviceError('DEVICE_OFFLINE'))
        for record in self.hub.records.values():
            if record['connection_id'] == self.id and record['status'] not in TERMINAL:
                record['status'], record['fresh'] = 'unknown', False
                future = record.get('ack')
                if future and not future.done():
                    future.set_result({'ok': False, 'status': 'unknown',
                                       'error_code': 'DEVICE_OFFLINE', 'command_id': record['command_id']})

    def event(self, event):
        if event.get('v') != 1 or event.get('connection_id') != self.id:
            return
        kind = event.get('type')
        if kind == 'control.synced':
            if (type(event.get('generation')) is not int or type(event.get('active')) is not bool
                    or not isinstance(event.get('control_session_id'), str)):
                return
            key = ('sync', event.get('control_session_id'), event['generation'], event['active'])
            self.resolve(key, event)
        elif kind == 'device.state':
            request_id = event.get('request_id')
            if not isinstance(request_id, str) or ('query', request_id) not in self.waiters:
                return
            if isinstance(event.get('capabilities'), dict):
                self.capabilities = event['capabilities']
            self.observed_at = time.monotonic()
            self.resolve(('query', request_id), event)
        elif kind == 'command.status':
            command_id = event.get('command_id')
            if not isinstance(command_id, str):
                return
            record = self.hub.records.get(command_id)
            state = event.get('status')
            if (not record or record['connection_id'] != self.id
                    or record['control_session_id'] != event.get('control_session_id')
                    or type(event.get('generation')) is not int
                    or record['generation'] != event['generation']
                    or not isinstance(state, str) or state not in STATUSES
                    or type(event.get('partial_applied')) is not bool):
                return
            if record['status'] in TERMINAL:
                return  # Never regress terminal state because of duplicate/late events.
            if record['status'] == 'running' and state == 'accepted':
                return
            record.update(status=state, partial_applied=event['partial_applied'],
                          fresh=True, observed_at=time.monotonic())
            if state in ('failed', 'rejected', 'cancelled'):
                code = event.get('error_code')
                if isinstance(code, str) and code in self.hub.error_codes:
                    record['error_code'] = code
                elif state in ('failed', 'rejected'):
                    record['error_code'] = 'HARDWARE_ERROR'
            if self.hub.config.stackchan_audit_enabled:
                LOGGER.warning('s2s_action_audit %s', json.dumps({'stage': 'device_command_status',
                    'command_id': command_id, 'status': state, 'simulated': record.get('simulated'),
                    'error_code': record.get('error_code'), 'summary': record.get('action_summary')},
                    ensure_ascii=False, allow_nan=False))
            future = record.get('ack')
            if future and not future.done() and (state == 'accepted' or state in TERMINAL):
                future.set_result(self.hub.command_result(record))


class Control:
    def __init__(self, hub, owner):
        self.hub, self.owner = hub, owner
        self.id, self.generation = secrets.token_hex(16), 1
        self.connection_id, self.synced = None, False
        self.closed = False
        self.keepalive = None

    @property
    def authorized(self):
        return not self.closed and self.hub.bindings.get(self.owner) == self.hub.config.stackchan_device_id

    @property
    def simulated(self):
        connection = self.hub.connection
        value = connection.capabilities.get('simulated') if connection else None
        return value if type(value) is bool else None

    def envelope(self, kind, connection=None, generation=None, **fields):
        connection = connection or self.hub.connection
        return {'v': 1, 'type': kind, 'connection_id': connection.id,
                'control_session_id': self.id,
                'generation': self.generation if generation is None else generation, **fields}

    async def confirm(self, connection, generation, active):
        try:
            await connection.request(('sync', self.id, generation, active),
                                     self.envelope('control.sync', connection, generation, active=active))
            if self.generation != generation or self.hub.connection is not connection:
                raise DeviceError('STALE_CONTEXT')
            self.synced = active
            return {'ok': True, 'status': 'stopped' if not active else 'ready'}
        except DeviceError:
            if self.generation == generation and self.connection_id == connection.id:
                self.synced = False
            raise

    async def ensure(self):
        if not self.authorized:
            raise DeviceError('NOT_READY')
        connection = self.hub.connection
        if not connection or not connection.online:
            raise DeviceError('DEVICE_OFFLINE')
        if self.hub.control and self.hub.control is not self:
            raise DeviceError('DEVICE_BUSY')
        self.hub.control = self
        if self.connection_id != connection.id:
            self.id, self.generation = secrets.token_hex(16), 1
            self.connection_id, self.synced = connection.id, False
            await self.confirm(connection, self.generation, True)
            if not self.keepalive:
                self.keepalive = asyncio.create_task(self.renew())
        if not self.synced:
            raise DeviceError('NOT_READY')
        return connection

    def fence(self, active=True):
        # This synchronous part invalidates any in-flight command before yielding.
        self.generation += 1
        self.synced = False
        connection = self.hub.connection
        if (not connection or not connection.online or self.connection_id != connection.id
                or self.hub.control is not self):
            async def offline():
                raise DeviceError('DEVICE_OFFLINE')
            return offline()
        return self.confirm(connection, self.generation, active)

    async def renew(self):
        try:
            while self.authorized:
                await asyncio.sleep(self.hub.config.device_keepalive_seconds)
                connection = self.hub.connection
                if (connection and connection.online and self.synced
                        and connection.id == self.connection_id and self.hub.control is self):
                    await connection.send(self.envelope('control.keepalive', connection))
        except (DeviceError, asyncio.CancelledError):
            pass  # Firmware lease expires if transport/authorization is lost.

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.keepalive:
            self.keepalive.cancel()
            await asyncio.gather(self.keepalive, return_exceptions=True)
        if self.hub.control is self:
            try:
                await self.fence(active=False)
            except DeviceError:
                # Do not grant a competing controller while stop is unconfirmed.
                connection = self.hub.connection
                if connection:
                    await connection.socket.close(code=1001, message=b'Control released')
            self.hub.control = None
        self.hub.controllers.discard(self)

    async def query(self, command_id=None):
        connection = self.hub.connection
        if not connection or not connection.online:
            return None
        request_id = secrets.token_hex(12)
        message = {'v': 1, 'type': 'device.query', 'connection_id': connection.id, 'request_id': request_id}
        if command_id:
            message['command_id'] = command_id
        try:
            observed = await connection.request(('query', request_id), message)
            return observed if self.hub.connection is connection else None
        except DeviceError:
            return None

    async def execute(self, name, arguments):
        result = await self._execute(name, arguments)
        if 'simulated' not in result:
            record = self.hub.records.get(result.get('command_id'))
            result['simulated'] = record['simulated'] if record else self.simulated
        if name == 'stackchan_perform' and self.hub.config.stackchan_audit_enabled:
            LOGGER.warning('s2s_action_audit %s', json.dumps({'stage': 'perform_result',
                'summary': action_summary(arguments), **{key: result[key] for key in (
                    'command_id', 'status', 'error_code', 'fresh', 'simulated') if key in result}},
                ensure_ascii=False, allow_nan=False))
        return result

    async def _execute(self, name, arguments):
        try:
            if not self.authorized:
                raise DeviceError('NOT_READY')
            validator = PARAMETERS.get(name)
            if validator is None:
                raise DeviceError('UNKNOWN_TOOL')
            if not validator.is_valid(arguments):
                raise DeviceError('INVALID_ARGUMENT')
            encode(arguments)
            if name == 'stackchan_get_state':
                observed = await self.query()
                connection = self.hub.connection
                online = bool(connection and connection.online)
                return {'ok': True, 'online': online, 'fresh': observed is not None,
                        'age_ms': round((time.monotonic() - connection.observed_at) * 1000) if connection else None,
                        'device_boot_id': connection.boot_id if connection else None,
                        'capabilities': connection.capabilities if online else {},
                        'state': observed.get('state', {}) if observed else {},
                        'active_command_id': observed.get('active_command_id') if observed else None}
            if name == 'stackchan_get_command':
                record = self.hub.records.get(arguments['command_id'])
                if not record or record['owner'] != self.owner:
                    return {'ok': True, 'command_id': arguments['command_id'], 'status': 'unknown', 'fresh': False}
                observed = await self.query(record['command_id'])
                result = self.hub.command_result(record)
                result['fresh'] = False  # A query must explicitly confirm this command.
                if observed and observed.get('command_id') == record['command_id']:
                    state = observed.get('status')
                    if isinstance(state, str) and state in STATUSES | {'unknown'}:
                        same_connection = record['connection_id'] == observed.get('connection_id')
                        if same_connection and record['status'] in TERMINAL and state not in TERMINAL:
                            result['fresh'] = True
                            return result  # A delayed query cannot undo a final receipt.
                        result.update(status=state, fresh=True)
                        # Querying a rebooted device cannot confirm old physical state.
                        result.pop('error_code', None)
                        result['partial_applied'] = observed.get('partial_applied') is True
                        code = observed.get('error_code')
                        if isinstance(code, str) and code in self.hub.error_codes:
                            result['error_code'] = code
                return result
            if name == 'stackchan_stop':
                connection = self.hub.connection
                if not (connection and connection.online and self.hub.control is self
                        and self.connection_id == connection.id):
                    await self.ensure()
                result = await self.fence(active=True)
                result['status'] = 'stopped'
                return result
            if self.hub.config.stackchan_read_only:
                raise DeviceError('NOT_READY')
            target_limit = self.hub.config.stackchan_test_max_deg
            if target_limit is not None and any(
                    abs(item['head'][axis]) > target_limit
                    for item in arguments['frames'] if 'head' in item
                    for axis in ('yaw_deg', 'pitch_deg')):
                raise DeviceError('OUT_OF_RANGE')
            connection = await self.ensure()
            validate_frames(arguments, connection.capabilities)
            command_id, generation = secrets.token_hex(16), self.generation
            message = self.envelope('command.execute', connection, command_id=command_id, args=arguments)
            if not COMMAND.is_valid(message):
                raise DeviceError('INVALID_ARGUMENT')
            encode(message)  # Enforce the *complete* envelope byte budget before dispatch.
            self.hub.trim_records()
            future = asyncio.get_running_loop().create_future()
            record = {**{key: message[key] for key in ('command_id', 'connection_id', 'control_session_id', 'generation')},
                      'owner': self.owner, 'status': 'unknown', 'fresh': False, 'partial_applied': False,
                      'observed_at': time.monotonic(), 'ack': future, 'simulated': self.simulated}
            if self.hub.config.stackchan_audit_enabled:
                record['action_summary'] = action_summary(arguments)
            self.hub.records[command_id] = record
            try:
                await connection.send(message, guard=lambda: self.authorized and self.synced and self.generation == generation)
                return await asyncio.wait_for(future, self.hub.config.device_ack_seconds)
            except TimeoutError:
                raise DeviceError('ACK_TIMEOUT', command_id) from None
            finally:
                record.pop('ack', None)
        except DeviceError as error:
            return error.result()


class DeviceHub:
    error_codes = {'DEVICE_OFFLINE', 'DEVICE_BUSY', 'NOT_READY', 'INVALID_ARGUMENT', 'MESSAGE_TOO_LARGE',
                   'UNSUPPORTED_CAPABILITY', 'MOTION_DISABLED', 'NOT_CALIBRATED', 'OUT_OF_RANGE', 'BUSY',
                   'STALE_CONTEXT', 'IDEMPOTENCY_CONFLICT', 'ACK_TIMEOUT', 'MOTION_TIMEOUT', 'HARDWARE_ERROR'}
    error_codes |= {'SCHEDULER_OVERRUN', 'LEASE_EXPIRED', 'LOCAL_STOP'}

    def __init__(self, config):
        self.config = config
        self.connection, self.control = None, None
        self.bindings, self.records = {}, OrderedDict()
        self.controllers = set()

    def for_owner(self, owner):
        if not self.config.stackchan_configured or self.bindings.get(owner) != self.config.stackchan_device_id:
            return None
        control = Control(self, owner)
        self.controllers.add(control)
        return control

    async def revoke(self, owner):
        self.bindings.pop(owner, None)
        await asyncio.gather(*(c.close() for c in list(self.controllers) if c.owner == owner))

    def trim_records(self):
        while len(self.records) >= 64:
            key = next((key for key, record in self.records.items()
                        if record['status'] in TERMINAL or record['status'] == 'unknown'), None)
            if key is None:
                raise DeviceError('BUSY')
            del self.records[key]

    @staticmethod
    def command_result(record):
        return {'ok': record['status'] not in {'rejected', 'failed'},
                **{key: record[key] for key in ('command_id', 'status', 'partial_applied', 'fresh')},
                'age_ms': round((time.monotonic() - record['observed_at']) * 1000),
                'simulated': record.get('simulated'),
                **({'error_code': record['error_code']} if 'error_code' in record else {})}

    async def device_socket(self, request):
        if not self.config.stackchan_configured:
            raise web.HTTPServiceUnavailable()
        supplied = request.headers.get('Authorization', '')
        expected = 'Bearer ' + self.config.stackchan_device_token
        if not hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(expected.encode()).digest()):
            raise web.HTTPUnauthorized()
        if request.headers.get('Origin'):
            raise web.HTTPForbidden()  # Device endpoint is not a cookie/browser endpoint.
        socket = web.WebSocketResponse(heartbeat=10, max_msg_size=8192)
        await socket.prepare(request)
        connection = None
        try:
            msg = await socket.receive(timeout=5)
            if msg.type != WSMsgType.TEXT:
                raise DeviceError('INVALID_ARGUMENT')
            hello = decode(msg.data)
            if (hello.get('type') != 'device.hello' or hello.get('v') != 1
                    or not isinstance(hello.get('device_boot_id'), str)
                    or not IDENTIFIER.fullmatch(hello['device_boot_id'])
                    or not isinstance(hello.get('firmware_version'), str)
                    or len(hello['firmware_version']) > 64 or not isinstance(hello.get('capabilities'), dict)):
                raise DeviceError('INVALID_ARGUMENT')
            previous = self.connection
            if previous:
                previous.lost()
                await previous.socket.close(code=1001, message=b'New connection')
            connection = Connection(self, socket, hello)
            self.connection = connection
            await connection.send({'v': 1, 'type': 'connection.ready', 'connection_id': connection.id})
            while True:
                msg = await socket.receive(timeout=30)
                if msg.type == WSMsgType.TEXT:
                    connection.event(decode(msg.data))
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
                elif msg.type == WSMsgType.BINARY:
                    raise DeviceError('INVALID_ARGUMENT')
        except (DeviceError, TimeoutError, ConnectionError):
            await socket.close(code=1008, message=b'Device protocol rejected')
        finally:
            if connection:
                connection.lost()
                if self.connection is connection:
                    self.connection = None
                    if self.control:
                        self.control.synced = False
        return socket

    async def close(self):
        await asyncio.gather(*(c.close() for c in list(self.controllers)), return_exceptions=True)
        if self.connection:
            await self.connection.socket.close(code=1001, message=b'Shutdown')
