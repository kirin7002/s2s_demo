"""Free target keyframes -> timed device sequence; no gesture-name presets."""
import copy
import math

from .timing_planner import TimingPlanError

PACE = {'gentle': .5, 'normal': .75, 'brisk': 1.0}


def compile_targets(arguments, capabilities, initial_pose=None):
    """Caller validates target tool schema; output still needs wire validation.

    Pose interpolation happens in the device at its existing 20ms cadence.
    Long legs are additionally divided into collinear wire frames within the
    same atomic command. No target, expression, or light setting is clipped.
    """
    steps = arguments['steps']
    pace = arguments.get('pace', 'normal')
    neutral = arguments.get('return_to_neutral', False)
    if pace not in PACE or type(neutral) is not bool or not isinstance(steps, list) or not 1 <= len(steps) <= 16:
        raise TimingPlanError('INVALID_ARGUMENT', reason='target_plan_shape')
    max_frames = capabilities.get('max_frames', 16)
    max_duration = capabilities.get('max_duration_ms', 8000)
    if (type(max_frames) is not int or max_frames < 1
            or type(max_duration) is not int or max_duration < 100):
        raise TimingPlanError('NOT_READY', reason='timing_capabilities')
    max_frames, max_duration = min(16, max_frames), min(8000, max_duration)
    has_head = neutral or any('head' in s for s in steps)
    speed, bounds, previous = None, {}, copy.deepcopy(initial_pose)
    if has_head:
        head = capabilities.get('head', {})
        if head.get('enabled') is not True:
            raise TimingPlanError('MOTION_DISABLED', reason='head_disabled')
        if head.get('calibrated') is not True:
            raise TimingPlanError('NOT_CALIBRATED', reason='head_not_calibrated')
        maximum = head.get('max_speed_deg_s')
        if type(maximum) not in (int, float) or not math.isfinite(maximum) or maximum <= 0:
            raise TimingPlanError('NOT_READY', reason='speed_capability')
        speed = maximum * PACE[pace]
        if not isinstance(previous, dict):
            raise TimingPlanError('NOT_READY', reason='fresh_pose_unavailable')
        for axis, outer in (('yaw_deg', (-60, 60)), ('pitch_deg', (-25, 25))):
            advertised = head.get(axis.replace('_deg', '_range_deg'))
            if (not isinstance(advertised, list) or len(advertised) != 2
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in advertised)
                    or advertised[0] > advertised[1]):
                raise TimingPlanError('NOT_READY', reason='pose_capability')
            bounds[axis] = max(outer[0], advertised[0]), min(outer[1], advertised[1])
            value = previous.get(axis)
            if type(value) not in (int, float) or not math.isfinite(value) or not bounds[axis][0] <= value <= bounds[axis][1]:
                raise TimingPlanError('NOT_READY', reason='fresh_pose_outside_range', axis=axis)
            if neutral and not bounds[axis][0] <= 0 <= bounds[axis][1]:
                raise TimingPlanError('OUT_OF_RANGE', reason='neutral_outside_range', axis=axis)

    frames, mapping, cursor = [], [], 0

    def append_step(step, index):
        nonlocal previous, cursor
        hold = step.get('hold_ms', 0)
        if type(hold) is not int or not 0 <= hold <= 8000 or not any(c in step for c in ('head', 'screen', 'light')):
            raise TimingPlanError('INVALID_ARGUMENT', reason='target_step', step_index=index)
        target = step.get('head')
        start = copy.deepcopy(previous)
        segments = 1
        if target is not None:
            for axis in ('yaw_deg', 'pitch_deg'):
                value = target.get(axis)
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise TimingPlanError('INVALID_ARGUMENT', reason='target_type', step_index=index, axis=axis)
                if not bounds[axis][0] <= value <= bounds[axis][1]:
                    raise TimingPlanError('OUT_OF_RANGE', reason='target', step_index=index,
                                          axis=axis, limits=list(bounds[axis]))
            distance = max(abs(target[k] - start[k]) for k in ('yaw_deg', 'pitch_deg'))
            segments = max(1, math.ceil(distance * 1000 / speed / 3750))
        first = len(frames)
        if first + segments > max_frames:
            raise TimingPlanError('OUT_OF_RANGE', reason='generated_frame_count',
                                  required_frames=first + segments, max_frames=max_frames)
        for part in range(1, segments + 1):
            frame = {'at_ms': cursor}
            budget = 250  # Budget for a visible output update before a dwell.
            if target is not None:
                # Use the exact requested endpoint for the last segment.
                point = copy.deepcopy(target) if part == segments else {
                    k: start[k] + (target[k] - start[k]) * part / segments
                    for k in ('yaw_deg', 'pitch_deg')}
                distance = max(abs(point[k] - previous[k]) for k in ('yaw_deg', 'pitch_deg'))
                budget = max(750, math.ceil(distance * 1000 / speed) + 250)
                if budget > 4000:
                    raise TimingPlanError('OUT_OF_RANGE', reason='generated_move_duration', step_index=index)
                frame['head'] = {**point, 'move_ms': budget}
                previous = point
            if part == 1:
                if 'screen' in step:
                    frame['screen'] = copy.deepcopy(step['screen'])
                if 'light' in step:
                    frame['light'] = {**copy.deepcopy(step['light']), 'fade_ms': 150}
            frames.append(frame)
            cursor += budget + (hold if part == segments else 0)
            if cursor > max_duration or frame['at_ms'] > 7900:
                raise TimingPlanError('OUT_OF_RANGE', reason='generated_duration',
                                      required_duration_ms=cursor, max_duration_ms=max_duration)
        mapping.append({'step_index': index, 'first_frame': first, 'last_frame': len(frames) - 1})

    for index, step in enumerate(steps):
        append_step(step, index)
    neutral_added = False
    if neutral and (not any('head' in f for f in frames) or any(previous[k] != 0 for k in ('yaw_deg', 'pitch_deg'))):
        append_step({'head': {'yaw_deg': 0, 'pitch_deg': 0}}, len(steps))
        neutral_added = True
    result = {'duration_ms': cursor, 'frames': frames}
    metadata = {'planning_mode': 'targets', 'pace': pace, 'planned_speed_deg_s': speed,
                'input_steps': len(steps), 'generated_frames': len(frames),
                'effective_duration_ms': cursor, 'return_to_neutral': neutral,
                'neutral_frame_added': neutral_added, 'step_frames': mapping,
                'frame_timing': [{'frame_index': i, 'at_ms': f['at_ms'],
                                  **({'move_ms': f['head']['move_ms']} if 'head' in f else {})}
                                 for i, f in enumerate(frames)]}
    return result, metadata
