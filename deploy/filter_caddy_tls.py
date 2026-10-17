"""Filter Caddy JSON logs for one site's certificate diagnostics only."""
from collections import deque
import json
import re
import sys

DOMAIN = "s2s.kirinxu.cn"
TARGET = re.compile(r"(?<![A-Za-z0-9_.-])s2s\.kirinxu\.cn(?![A-Za-z0-9_.-])")
CA_URLS = {
    "https://acme-v02.api.letsencrypt.org/directory",
    "https://acme-staging-v02.api.letsencrypt.org/directory",
}


def redact(value, limit=1600):
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                  "[REDACTED KEY]", value, flags=re.S)
    text = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    text = re.sub(
        r'''(?ix)\b(authorization|cookie|api[_\ -]?key|dashscope_api_key|password|s2s_demo_password|secret|token)\b["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
        r"\1=[REDACTED]", text)
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(https?://[^\s?]+)\?\S+", r"\1?[REDACTED]", text)
    return re.sub(r"[\x00-\x1f\x7f]+", " ", text)[:limit]


def filter_event(event):
    if not isinstance(event, dict):
        return None
    logger = event.get("logger", "")
    if not isinstance(logger, str) or not (logger == "tls" or logger.startswith("tls.")):
        return None
    identifiers = []
    for key in ("identifier", "identifiers", "domains", "subjects"):
        value = event.get(key)
        if isinstance(value, str):
            identifiers.append(value)
        elif isinstance(value, list):
            identifiers.extend(value)
    if identifiers:
        if any(value != DOMAIN for value in identifiers):
            return None  # Also discard shared events naming any other site.
    elif not any(isinstance(event.get(key), str) and TARGET.search(event[key])
                 for key in ("msg", "error")):
        return None
    result = {"identifier": DOMAIN}
    for key in ("msg", "error"):
        if isinstance(event.get(key), str):
            result[key] = redact(event[key])
    if "ca" in event:
        ca = event["ca"]
        result["ca"] = ca if isinstance(ca, str) and ca in CA_URLS else "[unrecognized CA omitted]"
    return result


def collect(lines):
    events = deque(maxlen=20)
    for line in lines:
        if len(line) > 32768:
            continue
        try:
            event = filter_event(json.loads(line))
        except (ValueError, TypeError):
            continue
        if event is not None:
            events.append(event)
    return list(events)


if __name__ == "__main__":
    events = collect(sys.stdin)
    for event in events:
        print(json.dumps(event, ensure_ascii=False))
    print("matched_s2s_certificate_events=" + str(len(events)))
