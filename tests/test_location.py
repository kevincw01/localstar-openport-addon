import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from location import LocationUnavailable, fetch_home_assistant_config, resolve_options
import run_addon

HA = {"latitude": 0.0, "longitude": 0.0, "timezone": "Etc/UTC"}


class TestLocation(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_default_inherits_all_fields_and_saves_only_location(self):
        options = {"timezone": "Home Assistant", "mqtt_password": "disposable"}
        result = resolve_options(options, self.directory, fetch=lambda: HA)
        self.assertEqual({key: result[key] for key in HA}, HA)
        self.assertEqual(options, {"timezone": "Home Assistant", "mqtt_password": "disposable"})
        cache = self.directory / 'location.json'
        self.assertEqual(json.loads(cache.read_text()), HA)
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)

    def test_explicit_values_including_zero_override_individually(self):
        inherited = HA | {"latitude": 10, "longitude": 20, "timezone": "Europe/London"}
        result = resolve_options({"latitude": 0, "timezone": "Etc/UTC"},
                                 self.directory, fetch=lambda: inherited)
        self.assertEqual(result, {"latitude": 0, "longitude": 20, "timezone": "Etc/UTC"})

    def test_explicit_configuration_does_not_need_core_and_is_saved(self):
        with patch('location.fetch_home_assistant_config') as fetch:
            self.assertEqual(resolve_options(HA, self.directory), HA)
            fetch.assert_not_called()
        self.assertEqual(json.loads((self.directory / 'location.json').read_text()), HA)

    def test_saved_location_is_static_across_core_outage_and_ha_setting_changes(self):
        resolve_options({}, self.directory, fetch=lambda: HA)
        with patch('location.fetch_home_assistant_config', side_effect=OSError('unavailable')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(resolve_options({}, self.directory), HA)
        changed = HA | {"latitude": 1, "timezone": "Europe/London"}
        with patch('location.fetch_home_assistant_config', return_value=changed) as fetch:
            self.assertEqual(resolve_options({}, self.directory), HA)
            fetch.assert_not_called()
        self.assertEqual(json.loads((self.directory/'location.json').read_text()), HA)

    def test_explicit_edits_update_only_selected_fields_and_remain_saved(self):
        resolve_options({}, self.directory, fetch=lambda: HA)
        changed = HA | {"latitude": 1, "timezone": "Europe/London"}
        with patch('location.fetch_home_assistant_config') as fetch:
            self.assertEqual(resolve_options({'latitude': 1, 'timezone': 'Europe/London'}, self.directory), changed)
            self.assertEqual(resolve_options({}, self.directory), changed)
            fetch.assert_not_called()
        self.assertEqual(json.loads((self.directory/'location.json').read_text()), changed)

    def test_corrupt_saved_location_requires_explicit_repair_and_never_imports_another_location(self):
        (self.directory / 'location.json').write_text('invalid')
        with patch('location.fetch_home_assistant_config') as fetch:
            with self.assertRaises(LocationUnavailable):
                resolve_options({}, self.directory)
            fetch.assert_not_called()
            self.assertEqual(resolve_options(HA, self.directory), HA)

    def test_missing_or_invalid_core_location_never_falls_back_to_zero(self):
        for fetch in (lambda: {}, lambda: HA | {"latitude": True},
                      lambda: HA | {"longitude": 181}, lambda: HA | {"timezone": 'invalid/zone'}):
            with self.subTest(fetch=fetch), self.assertRaises(LocationUnavailable):
                resolve_options({}, self.directory, fetch=fetch)
        self.assertFalse((self.directory / 'location.json').exists())

    def test_invalid_override_is_rejected_before_contacting_core(self):
        with patch('location.fetch_home_assistant_config') as fetch:
            for options in ({"latitude": 91}, {"latitude": ""}, {"timezone": "invalid/zone"}):
                with self.assertRaises(ValueError):
                    resolve_options(options, self.directory)
            fetch.assert_not_called()

    def test_unsaved_location_does_not_start_with_settings_that_could_change_next_restart(self):
        with patch('location.os.replace', side_effect=OSError('disk full')), patch('builtins.print'):
            with self.assertRaises(LocationUnavailable):
                resolve_options({}, self.directory, fetch=lambda: HA)
        self.assertFalse(list(self.directory.glob('*.tmp')))

    def test_core_proxy_uses_supervisor_token_without_storing_it(self):
        provider = {"latitude": 0, "longitude": 0, "time_zone": "Etc/UTC", "location_name": "Synthetic"}
        with patch.dict('os.environ', {"SUPERVISOR_TOKEN": 'disposable-token'}), \
                patch('location.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(provider).encode())) as fetch:
            self.assertEqual(fetch_home_assistant_config(), HA)
        request = fetch.call_args.args[0]
        self.assertEqual(request.full_url, 'http://supervisor/core/api/config')
        self.assertEqual(request.get_header('Authorization'), 'Bearer disposable-token')

    def test_startup_retries_core_and_then_starts_with_resolved_options(self):
        options = self.directory / 'options.json'
        options.write_text(json.dumps({"timezone": "Home Assistant"}))
        with patch.object(run_addon, 'resolve_options', side_effect=[LocationUnavailable('Core starting'), HA]), \
                patch.object(run_addon.time, 'sleep') as sleep, \
                patch.object(run_addon, 'create_application') as create, \
                patch.object(run_addon.web, 'run_app'), patch('builtins.print'):
            self.assertEqual(run_addon.main(options), 0)
        sleep.assert_called_once_with(30)
        create.assert_called_once_with(HA)


if __name__ == '__main__':
    unittest.main()
