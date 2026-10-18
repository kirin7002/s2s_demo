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
    return {'frame_count': len(frames), 'head_frame_count': len(heads), 'head_axes': axes,
            'move_ms': [h['move_ms'] for h in heads if type(h.get('move_ms')) is int],
            'duration_ms': arguments.get('duration_ms') if isinstance(arguments, dict)
                and type(arguments.get('duration_ms')) is int else None,
            'expressions': sorted({f['screen']['expression'] for f in frames[:16]
                if isinstance(f, dict) and isinstance(f.get('screen'), dict)
                and isinstance(f['screen'].get('expression'), str)
                and f['screen']['expression'] in EXPRESSIONS})}
