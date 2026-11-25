import unittest
from dataclasses import asdict

from apps.services import Datasource, DatasourceHealth, DatasourceService


class DatasourceTests(unittest.TestCase):
    def test_four_types_and_influx_versions(self):
        for kind, settings, language in [
            ('VictoriaMetrics', {}, 'MetricsQL'), ('Prometheus', {}, 'PromQL'),
            ('ClickHouse', {}, 'SQL'),
            ('InfluxDB', {'influx_version': '1', 'database': 'metrics'}, 'InfluxQL'),
            ('InfluxDB', {'organization': 'org', 'bucket': 'metrics'}, 'Flux'),
            ('InfluxDB', {'influx_version': '3', 'database': 'metrics'}, 'SQL'),
        ]:
            with self.subTest(kind=kind, settings=settings):
                source = Datasource('source', ' Source ', 'http://localhost:1234/', kind, **settings)
                self.assertEqual(source.name, 'Source')
                self.assertEqual(source.url, 'http://localhost:1234')
                self.assertEqual(source.query_language, language)
                self.assertEqual(source.uid, 'source')
                if kind == 'ClickHouse':
                    self.assertEqual(source.database, 'default')

    def test_legacy_token_inference_and_secret_references(self):
        legacy = Datasource('vm', 'VM', 'http://vm', token_env='VM_TOKEN')
        self.assertEqual(legacy.auth_type, 'bearer')
        basic = Datasource('ch', 'CH', 'http://ch', 'ClickHouse', auth_type='basic',
                           username='reader', password_env='CH_PASSWORD')
        self.assertEqual(asdict(basic)['password_env'], 'CH_PASSWORD')
        self.assertNotIn('password', asdict(basic))

    def test_invalid_settings(self):
        cases = [dict(id=''), dict(name=' '), dict(kind='unknown'), dict(url='http://u:p@host'),
                 dict(url='http://host?token=x'), dict(url='file:///tmp/a'), dict(url='http://host:bad'),
                 dict(url='http://host/\n'), dict(auth_type='basic'), dict(auth_type='bearer'),
                 dict(token_env='actual secret'), dict(auth_type='none', token_env='TOKEN'),
                 dict(timeout_seconds=0), dict(timeout_seconds=float('nan')), dict(timeout_seconds=True),
                 dict(min_interval_seconds=0), dict(min_interval_seconds=True), dict(tls_verify='false'),
                 dict(kind='InfluxDB'), dict(kind='InfluxDB', influx_version='1'),
                 dict(kind='InfluxDB', influx_version='4'), dict(kind='ClickHouse', token_env='TOKEN')]
        for changes in cases:
            fields = {'id': 'x', 'name': 'X', 'url': 'http://host', **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Datasource(**fields)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = DatasourceService()

    def test_crud_stable_uid_and_default_switching(self):
        first = self.service.create('VM', 'http://vm')
        second = self.service.create('Prom', 'http://prom', 'Prometheus')
        self.assertTrue(first.is_default)
        self.assertFalse(second.is_default)
        changed = self.service.update(first.id, name='Renamed', url='https://vm')
        self.assertEqual(changed.id, first.id)
        self.service.set_default(second.id)
        self.assertEqual(self.service.default().id, second.id)
        self.assertFalse(self.service.get(first.id).is_default)
        self.service.delete(second.id)
        self.assertEqual(self.service.default().id, first.id)
        self.service.delete(first.id)
        self.assertIsNone(self.service.default())

    def test_duplicate_names_and_invalid_update_leave_catalog_intact(self):
        vm = self.service.create('VM', 'http://vm')
        prom = self.service.create('Prom', 'http://prom', 'Prometheus')
        before = self.service.list()
        with self.assertRaises(ValueError):
            self.service.update(prom.id, name=' vm ', is_default=True)
        with self.assertRaises(ValueError):
            self.service.update(vm.id, id='changed')
        with self.assertRaises(KeyError):
            self.service.update('missing', name='New')
        with self.assertRaises(KeyError):
            self.service.delete('missing')
        self.assertEqual(self.service.list(), before)

    def test_explicit_save_default_and_fallback_are_unique(self):
        self.service.save(Datasource('b', 'B', 'http://b'))
        self.service.save(Datasource('a', 'A', 'http://a', is_default=True))
        self.assertEqual(self.service.default().id, 'a')
        self.service.update('a', is_default=False)
        self.assertEqual(self.service.default().id, 'b')
        self.assertEqual(sum(source.is_default for source in self.service.list()), 1)

    def test_list_filter_and_checker_injection(self):
        calls = []
        def checker(source):
            calls.append(source)
            return DatasourceHealth(source.id, 'ok', 'Checked', 123)
        self.service.checker = checker
        vm = self.service.create('VM prod', 'http://vm')
        self.service.create('Prom dev', 'http://prom', 'Prometheus')
        self.assertEqual(self.service.list(search='PROD'), (vm,))
        self.assertEqual(len(self.service.list(kind='Prometheus')), 1)
        self.assertEqual(self.service.test_connection(vm.id).status, 'ok')
        self.assertEqual(calls, [vm])
        self.service.checker = None
        with self.assertRaises(RuntimeError):
            self.service.test_connection(vm.id)

    def test_demo_health_does_not_claim_a_real_connection(self):
        source = self.service.create('VM', 'http://vm')
        result = self.service.demo_check(source)
        self.assertEqual(result.status, 'demo')
        self.assertIn('не проверялось', result.message)


if __name__ == '__main__':
    unittest.main()
