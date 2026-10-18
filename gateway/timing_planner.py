"""Compile model-authored frame timing into the unchanged device wire format.

Callers must validate the complete tool schema before this function and validate
the resulting wire command before dispatch. This module never sends commands.
"""
import copy
import math


class TimingPlanError(ValueError):
    def __init__(self, code, **details):
        super().__init__(code)
        self.code, self.details = code, details


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def plan_timing(arguments, capabilities, initial_pose=None):
    """Return (wire_args, timing_metadata), without mutating inputs.

    Auto timing only extends movement budgets and moves later frame starts
    forward. Same-frame channels stay simultaneous. Explicit nonnegative gaps
    on each channel are never shortened. Targets and output settings never
    change. Limits are checked atomically; no clipping or command splitting.
    """
    mode = arguments.get('timing_mode', 'auto')
    if mode not in ('auto', 'strict'):
        raise TimingPlanError('INVALID_ARGUMENT', reason='timing_mode')
    requested = {k: copy.deepcopy(v) for k, v in arguments.items() if k != 'timing_mode'}
    frames, duration = requested.get('frames'), requested.get('duration_ms')
    if not isinstance(frames, list) or not frames or not _integer(duration, 100, 8000):
        raise TimingPlanError('INVALID_ARGUMENT', reason='sequence_shape')
    max_frames = capabilities.get('max_frames', 16)
    max_duration = capabilities.get('max_duration_ms', 8000)
    if not _integer(max_frames, 1, 2147483647) or not _integer(max_duration, 100, 2147483647):
        raise TimingPlanError('NOT_READY', reason='invalid_timing_capabilities')
    max_frames, max_duration = min(max_frames, 16), min(max_duration, 8000)
    if len(frames) > max_frames:
        raise TimingPlanError('OUT_OF_RANGE', reason='frame_count', maximum=max_frames)
    previous_at = -1
    for index, frame in enumerate(frames):
        at = frame.get('at_ms') if isinstance(frame, dict) else None
        if not _integer(at, 0, 7900) or at <= previous_at or (index == 0 and at != 0):
            raise TimingPlanError('INVALID_ARGUMENT', reason='frame_order', frame_index=index)
        previous_at = at
        for channel, key, minimum in (('head', 'move_ms', 100), ('light', 'fade_ms', 0)):
            if channel in frame and (not isinstance(frame[channel], dict)
                                    or not _integer(frame[channel].get(key), minimum, 4000)):
                raise TimingPlanError('INVALID_ARGUMENT', reason='transition_shape', frame_index=index,
                                      field=key)

    result = copy.deepcopy(requested)
    if mode == 'auto':
        has_head = any('head' in f for f in frames)
        head_caps = capabilities.get('head', {})
        speed = head_caps.get('max_speed_deg_s')
        previous_pose = initial_pose
        if has_head:
            if (not _number(speed) or speed <= 0 or not isinstance(initial_pose, dict)
                    or not all(_number(initial_pose.get(k)) for k in ('yaw_deg', 'pitch_deg'))):
                raise TimingPlanError('NOT_READY', reason='fresh_pose_or_speed_unavailable')
            for axis, outer in (('yaw', (-60, 60)), ('pitch', (-25, 25))):
                bounds = head_caps.get(axis + '_range_deg')
                if (not isinstance(bounds, list) or len(bounds) != 2
                        or not all(_number(x) for x in bounds) or bounds[0] > bounds[1]):
                    raise TimingPlanError('NOT_READY', reason='invalid_pose_capabilities')
                lo, hi = max(bounds[0], outer[0]), min(bounds[1], outer[1])
                if not lo <= initial_pose[axis + '_deg'] <= hi:
                    raise TimingPlanError('NOT_READY', reason='initial_pose_outside_range', axis=axis)
                for index, frame in enumerate(frames):
                    if 'head' not in frame:
                        continue
                    target = frame['head'].get(axis + '_deg')
                    if not _number(target):
                        raise TimingPlanError('INVALID_ARGUMENT', reason='target_type', frame_index=index)
                    if not lo <= target <= hi:
                        raise TimingPlanError('OUT_OF_RANGE', reason='target', frame_index=index,
                                              axis=axis, limits=[lo, hi])

        old_ends, new_ends = {}, {}
        shift, original_end, planned_end = 0, 0, 0
        for index, (original, frame) in enumerate(zip(frames, result['frames'])):
            old_at = original['at_ms']
            at = old_at + shift
            for channel in ('head', 'light', 'screen'):
                if channel in frame and channel in old_ends:
                    gap = max(0, old_at - old_ends[channel])
                    at = max(at, new_ends[channel] + gap)
            frame['at_ms'] = at
            shift = at - old_at
            if 'head' in frame:
                head = frame['head']
                distance = max(abs(head[k] - previous_pose[k]) for k in ('yaw_deg', 'pitch_deg'))
                required = math.ceil(distance * 1000 / speed) + (250 if distance > 0 else 0)
                move_ms = max(head['move_ms'], required)
                if move_ms > 4000:
                    raise TimingPlanError('OUT_OF_RANGE', reason='planned_move_duration', frame_index=index,
                                          required_move_ms=move_ms, max_move_ms=4000)
                head['move_ms'] = move_ms
                previous_pose = head
            for channel in ('head', 'light', 'screen'):
                if channel not in frame:
                    continue
                old_transition = original[channel].get('move_ms', original[channel].get('fade_ms', 0))
                transition = frame[channel].get('move_ms', frame[channel].get('fade_ms', 0))
                old_ends[channel] = old_at + old_transition
                new_ends[channel] = at + transition
                original_end = max(original_end, old_at + max(1, old_transition))
                planned_end = max(planned_end, at + max(1, transition))
            if at > 7900:
                raise TimingPlanError('OUT_OF_RANGE', reason='planned_frame_start', frame_index=index,
                                      required_at_ms=at, maximum_at_ms=7900)
        result['duration_ms'] = max(100, planned_end + max(0, duration - original_end))
        if result['duration_ms'] > max_duration:
            raise TimingPlanError('OUT_OF_RANGE', reason='planned_duration',
                                  required_duration_ms=result['duration_ms'], max_duration_ms=max_duration)

    metadata = {'timing_mode': mode, 'timing_adjusted': result != requested,
                'requested_duration_ms': duration, 'effective_duration_ms': result['duration_ms'],
                'frame_timing': [{'frame_index': i, 'at_ms': frame['at_ms'],
                                 **({'move_ms': frame['head']['move_ms']} if 'head' in frame else {})}
                                for i, frame in enumerate(result['frames'])]}
    return result, metadata
