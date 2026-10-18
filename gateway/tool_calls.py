"""Per-voice-session function-call coordination; device tasks never block audio."""
import asyncio
from dataclasses import dataclass, field
import json

ACTION_TOOLS = {'stackchan_perform_targets', 'stackchan_perform'}

class ToolProtocolError(Exception):
    pass


def unique_json_object(pairs):
    """Reject ambiguous repeated keys before the parsed object loses them."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


@dataclass
class ActionBudget:
    failures: int = 0
    dispatched: bool = False
    blocked: bool = False
    block_reason: str = ''
    notice_sent: bool = False


@dataclass
class Response:
    id: str
    epoch: int
    ended: bool = False
    cancelled: bool = False
    items: dict = field(default_factory=dict)
    calls: dict = field(default_factory=dict)
    task: object = None
    budget: ActionBudget = field(default_factory=ActionBudget)


class ToolCalls:
    def __init__(self, control, send, notice, diagnostic=None):
        self.control, self.send, self.notice = control, send, notice
        self.diagnostic = diagnostic
        self.epoch = 0
        self.responses, self.calls = {}, {}
        self.active_response = None
        self.user_speaking = False
        self.closed = False
        self.fences = set()
        self.serial = asyncio.Lock()
        self.action_budget = ActionBudget()

    def valid(self, response):
        return not self.closed and not response.cancelled and response.epoch == self.epoch

    def invalidate(self):
        self.epoch += 1
        for response in self.responses.values():
            if response.epoch < self.epoch:
                response.cancelled = True
                if response.task and not response.task.done():
                    response.task.cancel()
                for call in response.calls.values():
                    if call['state'] != 'settled':
                        call['state'] = 'cancelled'
        # Own the coroutine as a Task immediately, even if shutdown follows
        # before the event loop next runs. No un-awaited nested coroutine.
        task = asyncio.create_task(self.control.fence())
        self.fences.add(task)
        task.add_done_callback(self.fence_finished)

    def fence_finished(self, task):
        self.fences.discard(task)
        if not task.cancelled():
            task.exception()  # Unknown stop outcome never causes action replay.

    def handle(self, event):
        kind = event['type']
        details = event.get('response', {})
        if kind in {'response.created', 'response.done'} and not isinstance(details, dict):
            raise ToolProtocolError('Invalid response')
        if kind == 'input_audio_buffer.speech_started':
            if not self.user_speaking:
                self.action_budget = ActionBudget()
            self.user_speaking = True
            return
        if kind == 'input_audio_buffer.speech_stopped':
            self.user_speaking = False
            return
        if kind == 'response.created':
            identifier = details.get('id')
            if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
                raise ToolProtocolError('Missing response identifier')
            if identifier not in self.responses:
                if len(self.responses) >= 128:
                    raise ToolProtocolError('Response limit')
                self.responses[identifier] = Response(identifier, self.epoch, budget=self.action_budget)
            self.active_response = identifier
            return
        identifier = event.get('response_id')
        if kind == 'response.done':
            identifier = details.get('id', identifier)
        response = self.responses.get(identifier) if isinstance(identifier, str) else None
        if response is None or not self.valid(response):
            return
        if kind in {'response.output_item.added', 'response.output_item.done'}:
            index, item = event.get('output_index'), event.get('item', {})
            if type(index) is not int or not 0 <= index < 64 or not isinstance(item, dict):
                raise ToolProtocolError('Invalid output index')
            response.items[index] = item
        elif kind == 'response.function_call_arguments.done':
            index, call_id = event.get('output_index'), event.get('call_id')
            name, arguments = event.get('name'), event.get('arguments')
            if (type(index) is not int or not 0 <= index < 64 or not isinstance(call_id, str)
                    or not 1 <= len(call_id) <= 128 or not isinstance(name, str)
                    or not isinstance(arguments, str) or len(arguments.encode()) > 32768):
                raise ToolProtocolError('Invalid function call')
            previous = self.calls.get(call_id)
            if previous:
                if (previous['name'], previous['arguments'], previous['response_id'], previous['index']) != (
                        name, arguments, identifier, index):
                    raise ToolProtocolError('Conflicting call identifier')
                return
            if response.ended:
                raise ToolProtocolError('Late function arguments')
            if len(self.calls) >= 64 or index in response.calls:
                raise ToolProtocolError('Tool call limit or duplicate index')
            call = {'call_id': call_id, 'name': name, 'arguments': arguments,
                    'response_id': identifier, 'index': index, 'state': 'pending'}
            self.calls[call_id], response.calls[index] = call, call
        elif kind == 'response.done':
            if response.ended:
                return
            response.ended = True
            if details.get('status') != 'completed':
                response.cancelled = True
                return
            outputs = details.get('output', [])
            if not isinstance(outputs, list) or len(outputs) > 64:
                raise ToolProtocolError('Invalid response output')
            for index, item in enumerate(outputs):
                if isinstance(item, dict):
                    response.items[index] = item
            for index, item in response.items.items():
                if item.get('type') == 'function_call' and index not in response.calls:
                    # Never execute arguments inferred from a partial/aggregate event.
                    raise ToolProtocolError('Function arguments completion missing')
                if index in response.calls:
                    call = response.calls[index]
                    if (item.get('type') != 'function_call' or item.get('call_id') != call['call_id']
                            or item.get('name') != call['name']):
                        raise ToolProtocolError('Conflicting function output')
            if response.calls:
                response.task = asyncio.create_task(self.execute(response))
                response.task.add_done_callback(self.finished)

    def finished(self, task):
        # Consume errors promptly; helper converts transport failures into a notice.
        if not task.cancelled():
            task.exception()

    def blocked_action(self, budget):
        return {'ok': False, 'error_code': 'INVALID_ARGUMENT' if budget.block_reason == 'validation' else 'NOT_READY',
                'details': {'reason': 'turn_action_retry_limit' if budget.block_reason == 'validation'
                            else 'turn_action_not_retryable'},
                'retryable': False, 'validation_failures': budget.failures, 'corrections_remaining': 0,
                'action_dispatched': False, 'simulated': self.control.simulated}

    def action_result(self, budget, result, name):
        """Bound model-authored corrections; never replay an action ourselves."""
        result = dict(result)
        result['retryable'] = False
        if result.get('command_id') or result.get('status') in {'accepted', 'running', 'completed'}:
            budget.dispatched = True
        preflight = result.get('error_code') in {'INVALID_ARGUMENT', 'OUT_OF_RANGE'} and not result.get('command_id')
        if preflight:
            budget.failures += 1
            result.update(retryable=budget.failures == 1 and not budget.dispatched,
                          validation_failures=budget.failures, action_dispatched=False,
                          corrections_remaining=int(budget.failures == 1 and not budget.dispatched))
            if result['retryable'] and name == 'stackchan_perform_targets':
                details = dict(result.get('details') or {})
                if details.get('field') == '$.steps':
                    details['repair_hint'] = 'steps必须是直接的对象数组，不是文本；将原步骤对象直接放进数组，保留原目标、值和顺序，不新增动作。'
                    result['details'] = details
            if not result['retryable']:
                budget.blocked, budget.block_reason = True, 'validation'
        elif result.get('ok') is False or result.get('error_code') or result.get('status') in {'unknown', 'failed', 'rejected'}:
            budget.blocked, budget.block_reason = True, 'execution'
            result['corrections_remaining'] = 0
        return result

    async def execute(self, response):
        try:
            async with self.serial:
                for index in sorted(response.calls):
                    call = response.calls[index]
                    if not self.valid(response):
                        return
                    call['state'] = 'executing'
                    action = call['name'] in ACTION_TOOLS
                    if action and response.budget.blocked:
                        result = self.blocked_action(response.budget)
                    else:
                        try:
                            arguments = json.loads(call['arguments'], object_pairs_hook=unique_json_object,
                                                   parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                            result = await self.control.execute(call['name'], arguments)
                        except (ValueError, TypeError, RecursionError):
                            result = {'ok': False, 'error_code': 'INVALID_ARGUMENT', 'simulated': self.control.simulated}
                        if action and self.valid(response):
                            result = self.action_result(response.budget, result, call['name'])
                    if not self.valid(response):
                        call['state'] = 'cancelled'
                        return
                    if self.diagnostic and (result.get('ok') is False or result.get('error_code')
                                            or result.get('timing_adjusted') is True):
                        await self.diagnostic(call['name'], result, call['call_id'])
                    await self.send({'type': 'conversation.item.create', 'item': {
                        'type': 'function_call_output', 'call_id': call['call_id'],
                        'output': json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False)}})
                    call['state'] = 'settled'
                    if response.budget.blocked and not response.budget.notice_sent:
                        response.budget.notice_sent = True
                        if response.budget.block_reason == 'validation':
                            message = ('这次动作参数未通过，未执行此动作；请继续说话。' if response.budget.dispatched
                                       else '本轮动作参数校验失败，未执行动作；请继续说话。')
                        else:
                            message = '动作执行未确认或不可用，不会自动重试；请继续说话或查询状态。'
                        await self.notice(message)
                if (self.valid(response) and not self.user_speaking and self.active_response == response.id
                        and not response.budget.blocked
                        and all(call['state'] == 'settled' for call in response.calls.values())):
                    await self.send({'type': 'response.create'})
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never relay exception text, device payloads or model arguments.
            response.cancelled = True
            await self.notice('工具结果传输未完成；不会重试设备动作，请查询设备状态。')

    async def close(self):
        self.closed = True
        tasks = [response.task for response in self.responses.values() if response.task]
        for task in tasks + list(self.fences):
            task.cancel()
        await asyncio.gather(*tasks, *list(self.fences), return_exceptions=True)
        await self.control.close()
