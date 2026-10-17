"""Synthetic logs only; no server, container, secrets or TLS operations."""
import json
import unittest

from deploy.filter_caddy_tls import CA_URLS, DOMAIN, collect, filter_event
from deploy.caddy_runtime_summary import summarize


class TLSLogFilterTests(unittest.TestCase):
    def test_only_target_tls_fields_are_exposed(self):
        event = {"logger": "tls.obtain", "identifier": DOMAIN, "msg": "could not obtain certificate",
                 "error": "HTTP 403 from validation", "ca": sorted(CA_URLS)[0],
                 "request": {"headers": {"Authorization": "fake-private"}}, "account": "fake-private"}
        result = filter_event(event)
        self.assertEqual(set(result), {"identifier", "msg", "error", "ca"})
        self.assertNotIn("fake-private", json.dumps(result))

    def test_other_sites_http_logs_and_shared_events_are_dropped(self):
        for event in [
            {"logger": "tls.obtain", "identifier": "ai.kirinxu.cn", "msg": "other-site-private"},
            {"logger": "http.log.error", "request": {"host": DOMAIN}, "msg": "private-cookie"},
            {"logger": "tls", "identifiers": [DOMAIN, "ai.kirinxu.cn"], "msg": "shared"},
        ]:
            self.assertIsNone(filter_event(event))

    def test_target_in_acme_error_without_identifier_is_kept(self):
        result = filter_event({"logger": "tls.issuance.acme", "msg": "challenge failed for " + DOMAIN})
        self.assertEqual(result["identifier"], DOMAIN)

    def test_common_secret_formats_are_redacted(self):
        secret = "fake-sensitive-value"
        message = ('password="' + secret + '"; API_KEY=' + secret + '; '
                   'Authorization: Bearer ' + secret + '; sk-fake0123456789 '
                   '-----BEGIN PRIVATE KEY-----' + secret + '-----END PRIVATE KEY----- '
                   'https://user:' + secret + '@example.com/?token=' + secret)
        result = filter_event({"logger": "tls", "identifier": DOMAIN, "error": message})
        output = json.dumps(result)
        self.assertNotIn(secret, output)
        self.assertNotIn('sk-fake0123456789', output)

    def test_malformed_lines_and_oversized_records_are_ignored(self):
        lines = ['not-json', '[]', 'x' * 32769,
                 json.dumps({"logger": "tls", "identifier": DOMAIN, "error": "bounded"})]
        self.assertEqual(len(collect(lines)), 1)

    def test_output_is_bounded_to_latest_twenty(self):
        lines = [json.dumps({"logger": "tls", "identifier": DOMAIN, "msg": str(n)}) for n in range(30)]
        events = collect(lines)
        self.assertEqual(len(events), 20)
        self.assertEqual(events[0]["msg"], "10")

    def test_counts_explain_empty_filtered_output_without_exposing_logs(self):
        stats = {}
        events = collect(['console private', '{}', json.dumps({'logger': 'tls.obtain',
                          'identifier': 'other.example', 'msg': 'private'})], stats)
        self.assertEqual(events, [])
        self.assertEqual(stats, dict(log_lines=3, json_lines=2, tls_lines=1, target_lines=0))

    def test_runtime_summary_exposes_only_target_booleans(self):
        config = {'private': 'fake-secret', 'apps': {'http': {'servers': {'srv0': {
            'routes': [{'match': [{'host': [DOMAIN]}]}]}}}, 'tls': {'automation': {'policies': [
                {'subjects': [DOMAIN], 'issuers': [{'module': 'acme',
                 'ca': 'https://acme-v02.api.letsencrypt.org/directory', 'api_key': 'fake-secret'}]}]}}}}
        result = summarize(config)
        self.assertTrue(result['active_s2s_route_present'])
        self.assertTrue(result['active_s2s_le_issuer_present'])
        self.assertFalse(result['active_s2s_auto_https_disabled'])
        self.assertTrue(all(isinstance(value, bool) for value in result.values()))
        self.assertNotIn('fake-secret', json.dumps(result))


if __name__ == "__main__":
    unittest.main()
