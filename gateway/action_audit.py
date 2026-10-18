"""Fixed, text-free actuator summaries. Never log screen text or voice content."""
import math

EXPRESSIONS = {'neutral', 'happy', 'laughing', 'scared', 'sad', 'surprised', 'thinking'}


def action_summary(arguments):
    frames = arguments.get('frames', []) if isinstance(arguments, dict) else []
    frames = frames if isinstance(frames, list) else []
    heads = [f['head'] for f in frames[:16] if isinstance(f, dict) and isinstance(f.get('head'), dict)]
    def finite(value):
        return type(value) in (int, float) and -1000000 <= value <= 1000000 and math.isfinite(value)
    axes = {}
    for axis in ('yaw_deg', 'pitch_deg'):
        values = [h[axis] for h in heads if finite(h.get(axis))]
        if values:
            axes[axis] = {'min': min(values), 'max': max(values)}
    ordered_heads, lights, screens = [], [], []
    for index, frame in enumerate(frames[:16]):
        head = frame.get('head') if isinstance(frame, dict) else None
        if (isinstance(head, dict) and finite(frame.get('at_ms'))
                and all(finite(head.get(key)) for key in ('yaw_deg', 'pitch_deg', 'move_ms'))):
            ordered_heads.append({'frame_index': index, 'at_ms': frame['at_ms'],
                                  **{key: head[key] for key in ('yaw_deg', 'pitch_deg', 'move_ms')}})
        if not isinstance(frame, dict) or not finite(frame.get('at_ms')):
            continue
        light, screen = frame.get('light'), frame.get('screen')
        if (isinstance(light, dict) and isinstance(light.get('rgb'), list) and len(light['rgb']) == 3
                and all(finite(x) for x in light['rgb'])
                and all(finite(light.get(key)) for key in ('brightness', 'fade_ms'))):
            lights.append({'frame_index': index, 'at_ms': frame['at_ms'],
                           **{key: light[key] for key in ('rgb', 'brightness', 'fade_ms')}})
        if isinstance(screen, dict) and screen.get('page') in ('face', 'text'):
            safe_screen = {'frame_index': index, 'at_ms': frame['at_ms'], 'page': screen['page']}
            if isinstance(screen.get('expression'), str) and screen['expression'] in EXPRESSIONS:
                safe_screen['expression'] = screen['expression']
            screens.append(safe_screen)
    return {'frame_count': len(frames), 'head_frame_count': len(heads), 'head_axes': axes,
            'head_frames': ordered_heads,
            'light_frames': lights, 'screen_frames': screens,
            'move_ms': [h['move_ms'] for h in heads if type(h.get('move_ms')) is int],
            'duration_ms': arguments.get('duration_ms') if isinstance(arguments, dict)
                and type(arguments.get('duration_ms')) is int else None,
            'expressions': sorted({f['screen']['expression'] for f in frames[:16]
                if isinstance(f, dict) and isinstance(f.get('screen'), dict)
                and isinstance(f['screen'].get('expression'), str)
                and f['screen']['expression'] in EXPRESSIONS})}
