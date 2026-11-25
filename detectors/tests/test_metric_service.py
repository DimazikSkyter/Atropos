"""Metadata-only catalog management and deferred lambda parameter binding."""
from dataclasses import FrozenInstanceError, replace
import json
import unittest

from apps.services import (
    AmbiguousMetricError, MetricCollection, MetricComposition, MetricMetadata,
    MetricPointer, MetricService,
)


def metadata(name='cpu', *, labels=None, additional_labels=None, start=10, end=610, step=15):
    return MetricMetadata(name=name, labels={} if labels is None else labels,
                          additional_labels={} if additional_labels is None else additional_labels,
                          start=start, end=end, step=step)


class MetadataTests(unittest.TestCase):
    def test_labels_are_separate_immutable_and_copied(self):
        labels, local = {'host': 'a'}, {'team': 'platform'}
        metric = metadata(labels=labels, additional_labels=local)
        labels['host'], local['team'] = 'b', 'changed'
        self.assertEqual(metric.labels, {'host': 'a'})
        self.assertEqual(metric.additional_labels, {'team': 'platform'})
        self.assertEqual(metric.effective_labels, {'__name__': 'cpu', 'host': 'a', 'team': 'platform'})
        with self.assertRaises(TypeError):
            metric.labels['host'] = 'changed'
        with self.assertRaises(TypeError):
            metric.additional_labels['team'] = 'changed'
        with self.assertRaises(FrozenInstanceError):
            metric.step = 1
        public = metric.effective_labels
        public['host'] = 'changed'
        self.assertEqual(metric.labels['host'], 'a')

    def test_pointer_identity_excludes_local_labels_and_window(self):
        a = metadata(labels={'host': 'a'}, additional_labels={'team': 'one'})
        b = metadata(labels={'host': 'a'}, additional_labels={'team': 'two'}, start=20, step=10)
        self.assertEqual(a.key, b.key)
        self.assertEqual(hash(a.key), hash(b.key))
        self.assertNotEqual(a.key, metadata(labels={'host': 'b'}).key)
        self.assertNotIn('team', a.key.labels)
        with self.assertRaises(TypeError):
            a.key.labels['host'] = 'changed'

    def test_sampling_config_and_json_export_have_no_sample_data(self):
        metric = metadata(start=10.25, end=30.75, step=0.5)
        self.assertEqual(metric.timestamp_range, (10.25, 30.75))
        exported = json.loads(json.dumps(metric.to_dict()))
        self.assertEqual(exported['step'], 0.5)
        self.assertEqual(set(exported), {'name', 'labels', 'additional_labels', 'start', 'end', 'step'})
        with self.assertRaises(TypeError):
            MetricMetadata(name='cpu', start=0, end=10, step=1, values=[1, 2])

    def test_invalid_metadata_is_rejected(self):
        cases = [dict(name=''), dict(labels={'host': 1}), dict(labels={'': 'a'}),
                 dict(labels={'__name__': 'wrong'}), dict(additional_labels={'__name__': 'wrong'}),
                 dict(labels={'host': 'a'}, additional_labels={'host': 'override'}),
                 dict(start=True), dict(end=float('inf')), dict(step=float('nan')),
                 dict(step=0), dict(step=-1), dict(start=1000), dict(step='15')]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                metadata(**kwargs)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.a = metadata(labels={'host': 'a', 'job': 'nodes'}, additional_labels={'team': 'payments'})
        self.b = metadata(labels={'host': 'b', 'job': 'nodes'}, additional_labels={'team': 'payments'})
        self.c = metadata('memory', labels={'host': 'c', 'job': 'nodes'}, additional_labels={'team': 'storage'})
        self.collection = MetricCollection([self.a, self.b, self.c])

    def test_filters_inventory_and_common_labels_include_local_labels(self):
        selected = self.collection.filter(names='cpu', labels={'team': 'payments'}, label_regex={'host': '[ab]'})
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected.common_labels, {'__name__': 'cpu', 'job': 'nodes', 'team': 'payments'})
        self.assertEqual(self.collection.label_values('team'), ['payments', 'storage'])
        self.assertIn('team', self.collection.label_names)
        self.assertEqual(len(self.collection.filter(labels={'missing': ''})), 0)
        self.assertEqual(len(self.collection.filter(label_regex={'missing': '.*'})), 0)
        self.assertEqual(len(self.collection.filter(names=[])), 0)
        self.assertEqual(self.collection.filter(prefix='mem', name_regex='memory').one(), self.c)
        self.assertEqual(self.collection.filter(exclude_names='cpu').one(), self.c)
        self.assertEqual(self.collection.filter(labels={'__name__': 'memory'}).one(), self.c)

    def test_grouping_by_mixed_labels_and_missing_values(self):
        groups = self.collection.group_by(['job', 'team'])
        self.assertEqual(len(groups[(('job', 'nodes'), ('team', 'payments'))]), 2)
        self.assertEqual(len(self.collection.group_by([])[()]), 3)
        self.assertEqual(self.collection.group_by('missing'), {})
        with self.assertRaises(ValueError):
            self.collection.group_by('missing', missing='raise')
        absent = MetricCollection([metadata('a'), metadata('b', additional_labels={'region': ''})])
        self.assertEqual(set(absent.group_by('region', missing='group')),
                         {(('region', None),), (('region', ''),)})
        with self.assertRaises(ValueError):
            self.collection.group_by(['team', 'team'])

    def test_exact_lookup_and_ambiguous_names(self):
        self.assertEqual(self.collection.get(self.a.key), self.a)
        self.assertEqual(self.collection.get('memory'), self.c)
        with self.assertRaises(AmbiguousMetricError):
            self.collection.get('cpu')
        with self.assertRaises(AmbiguousMetricError):
            self.collection.one()
        with self.assertRaises(AmbiguousMetricError):
            MetricCollection().one()
        with self.assertRaises(KeyError):
            self.collection.get('unknown')
        with self.assertRaises(TypeError):
            MetricCollection([{'name': 'cpu', 'values': [1]}])
        with self.assertRaises(ValueError):
            MetricCollection([self.a, self.a])

    def test_set_operations_compare_metadata_and_source_identity(self):
        left, right = MetricCollection([self.a, self.b]), MetricCollection([self.b, self.c])
        self.assertEqual(len(left.union(right)), 3)
        self.assertEqual(left.intersection(right).keys, (self.b.key,))
        self.assertEqual(left.difference(right).keys, (self.a.key,))
        changed = MetricCollection([replace(self.a, step=1, additional_labels={'team': 'other'})])
        with self.assertRaises(ValueError):
            left.union(changed)
        self.assertEqual(left.union(changed, conflict='left').get(self.a.key).step, 15)
        self.assertEqual(left.union(changed, conflict='right').get(self.a.key).step, 1)
        self.assertEqual(left.intersection(changed).get(self.a.key).additional_labels['team'], 'payments')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = metadata('used'), metadata('total')
        self.service = MetricService([self.a, self.b])

    def test_load_api_names_retains_labels_and_replaces_sampling_config(self):
        self.service.update('used', labels={'host': 'a'})
        self.service.add_additional_labels('used', {'team': 'payments'})
        snapshot = self.service.load({'status': 'success', 'data': ['used', 'free']}, start=20, end=40, step=0.5)
        self.assertEqual(snapshot.names, ['free', 'used'])
        self.assertEqual(snapshot.get('used').labels, {'host': 'a'})
        self.assertEqual(snapshot.get('used').additional_labels, {'team': 'payments'})
        self.assertEqual(snapshot.get('used').timestamp_range, (20, 40))
        self.assertEqual(snapshot.get('used').step, 0.5)
        self.assertEqual(snapshot.get('free').labels, {})
        self.assertEqual(self.service.names, ['free', 'used'])

    def test_api_names_restore_known_label_sets_after_temporary_absence(self):
        self.service.update('used', labels={'host': 'a'})
        self.service.add_additional_labels('used', {'team': 'payments'})
        saved = self.service.get('used').key
        self.service.load_names([], start=0, end=100, step=10)
        self.assertEqual(len(self.service), 0)
        self.service.update_additional_labels(saved, {'team': 'platform'})
        self.service.load_names(['used'], start=50, end=100, step=5)
        restored = self.service.get(saved)
        self.assertEqual(restored.additional_labels, {'team': 'platform'})
        self.assertEqual(restored.timestamp_range, (50, 100))

    def test_failed_api_load_or_replacement_preserves_state(self):
        self.service.add_additional_labels('used', {'team': 'payments'})
        invalid = [{'status': 'error'}, {'status': 'success', 'data': {'resultType': 'matrix', 'result': []}},
                   {'status': 'success', 'data': ['cpu', 123]}, ['cpu', 'cpu']]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.service.load(payload, start=0, end=10, step=1)
        with self.assertRaises(TypeError):
            self.service.load_names('cpu', start=0, end=10, step=1)
        with self.assertRaises(ValueError):
            self.service.replace([self.a, self.a])
        with self.assertRaises(ValueError):
            self.service.load_names([], start=0, end=10, step=0)
        self.assertEqual(self.service.names, ['total', 'used'])
        self.assertEqual(self.service.get('used').additional_labels, {'team': 'payments'})

    def test_crud_updates_metadata_and_preserves_old_snapshots(self):
        snapshot = self.service.snapshot()
        self.service.update('used', start=20, end=50, step=2)
        self.assertEqual(self.service.get('used').timestamp_range, (20, 50))
        self.assertEqual(snapshot.get('used').timestamp_range, (10, 610))
        with self.assertRaises(KeyError):
            self.service.add(self.a)
        self.service.add_additional_labels('used', {'team': 'payments'})
        self.service.remove('used')
        self.service.add(self.a)
        self.assertEqual(self.service.get('used').additional_labels, {})
        with self.assertRaises(ValueError):
            self.service.update('used', values=[1])
        self.service.clear()
        self.assertEqual(len(self.service), 0)

    def test_window_changes_apply_atomically_to_selections(self):
        self.service.set_window(self.service.snapshot(), start=100, end=200, step=5)
        self.assertTrue(all(metric.timestamp_range == (100, 200) and metric.step == 5
                            for metric in self.service.metrics))
        with self.assertRaises(ValueError):
            self.service.set_window(self.service.snapshot(), start=500, end=200, step=5)
        with self.assertRaises(KeyError):
            self.service.set_window(['used', 'unknown'], start=1, end=2, step=1)
        self.assertEqual(self.service.get('used').step, 5)

    def test_local_label_lifecycle_is_visible_in_filters_and_groups(self):
        self.service.add_additional_labels(self.service.snapshot(), {'team': 'payments', 'env': 'dev'})
        self.service.update_additional_labels('used', {'env': 'prod'})
        selected = self.service.filter(labels={'env': 'prod'}, label_regex={'team': 'pay.*'})
        self.assertEqual(selected.one().name, 'used')
        self.service.remove_additional_labels(selected, 'env')
        self.assertNotIn('env', self.service.get('used').effective_labels)
        self.service.set_additional_labels('used', {'owner': 'platform'})
        self.assertEqual(self.service.get('used').additional_labels, {'owner': 'platform'})
        self.assertEqual(len(self.service.group_by('team')), 1)
        self.service.set_additional_labels('used', {})
        self.assertEqual(self.service.get('used').additional_labels, {})

    def test_bulk_label_failures_do_not_partially_change_metadata(self):
        self.service.add_additional_labels('used', {'team': 'payments'})
        for labels in ({'__name__': 'wrong'}, {'bad': 123}, {'': 'bad'}):
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                self.service.set_additional_labels(self.service.snapshot(), labels)
        with self.assertRaises(KeyError):
            self.service.add_additional_labels(['total', 'used'], {'team': 'other'})
        self.assertEqual(self.service.get('total').additional_labels, {})
        with self.assertRaises(KeyError):
            self.service.update_additional_labels(['used', 'total'], {'team': 'other'})
        with self.assertRaises(KeyError):
            self.service.remove_additional_labels(['used', 'total'], 'team')
        self.assertEqual(self.service.get('used').additional_labels, {'team': 'payments'})
        self.service.update('used', labels={'host': 'a'})
        with self.assertRaises(ValueError):
            self.service.set_additional_labels(['total', 'used'], {'host': 'override'})
        self.assertEqual(self.service.get('total').additional_labels, {})


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.service = MetricService([metadata('used'), metadata('total')])

    def test_compose_and_resolve_never_invoke_the_lambda(self):
        calls = []
        function = lambda numerator, denominator: calls.append((numerator, denominator))
        plan = self.service.compose('ratio', function, bindings={'denominator': 'total', 'numerator': 'used'})
        self.assertIs(plan.function, function)
        self.assertEqual(plan.parameters, ('numerator', 'denominator'))
        self.assertEqual(plan.bindings['numerator'], self.service.get('used').key)
        resolved = self.service.resolve('ratio')
        self.assertEqual(resolved['numerator'].name, 'used')
        self.assertEqual(resolved['denominator'].name, 'total')
        self.service.update('used', step=1)
        self.service.add_additional_labels('used', {'team': 'platform'})
        self.service.update_additional_labels('used', {'team': 'payments'})
        self.assertEqual(self.service.resolve(plan)['numerator'].step, 1)
        self.service.update('used', labels={'host': 'a'})
        self.assertEqual(self.service.resolve('ratio')['numerator'].labels, {'host': 'a'})
        self.assertEqual(calls, [])

    def test_automatic_bindings_use_parameter_names_not_catalog_order(self):
        plan = self.service.compose('ratio', lambda total, used: used / total)
        self.assertEqual(plan.bindings, {'total': self.service.get('total').key,
                                         'used': self.service.get('used').key})
        self.assertEqual(tuple(self.service.resolve(plan)), ('total', 'used'))

    def test_local_label_selection_identifies_the_correct_metric(self):
        a = metadata('cpu', labels={'host': 'a'}, additional_labels={'role': 'primary'})
        b = metadata('cpu', labels={'host': 'b'}, additional_labels={'role': 'secondary'})
        service = MetricService([a, b])
        primary = service.filter(labels={'role': 'primary'}).one()
        secondary = service.filter(labels={'role': 'secondary'}).one()
        plan = service.compose('delta', lambda left, right: left - right,
                               bindings={'left': primary, 'right': secondary.key})
        self.assertEqual(service.resolve(plan), {'left': a, 'right': b})
        with self.assertRaises(AmbiguousMetricError):
            service.compose('wrong', lambda cpu: cpu)

    def test_missing_extra_or_unknown_bindings_rejected_without_registration(self):
        function = lambda left, right: left / right
        for bindings in ({'left': 'used'}, {'left': 'used', 'right': 'total', 'extra': 'used'},
                         {'left': 'unknown', 'right': 'total'}):
            with self.subTest(bindings=bindings), self.assertRaises((ValueError, KeyError)):
                self.service.compose('bad', function, bindings=bindings)
        self.assertEqual(self.service.compositions, ())

    def test_unsupported_signatures_and_noncallables_are_rejected(self):
        for function in (lambda: 1, lambda *args: 1, lambda **kwargs: 1, lambda used, /: used):
            with self.subTest(function=function), self.assertRaises(ValueError):
                self.service.compose('bad', function)
        with self.assertRaises(TypeError):
            self.service.compose('bad', 'sum')
        with self.assertRaises(ValueError):
            self.service.compose('bad', lambda used, total=1: used / total, bindings={'used': 'used'})

    def test_default_and_keyword_only_parameters_can_be_bound_explicitly(self):
        plan = self.service.compose('ratio', lambda used, *, total=1: used / total)
        self.assertEqual(plan.parameters, ('used', 'total'))
        self.assertEqual(self.service.resolve(plan)['total'].name, 'total')

    def test_references_resolve_current_labels_and_sampling_config(self):
        plan = self.service.compose('ratio', lambda used, total: used / total)
        self.service.add_additional_labels('used', {'team': 'platform'})
        self.service.set_window('used', start=100, end=200, step=3)
        resolved = self.service.resolve(plan)['used']
        self.assertEqual(resolved.additional_labels, {'team': 'platform'})
        self.assertEqual(resolved.timestamp_range, (100, 200))
        self.assertEqual(resolved.step, 3)
        self.assertEqual(plan.bindings['used'], resolved.key)

    def test_removed_binding_fails_and_reappearing_metric_resolves_again(self):
        self.service.compose('ratio', lambda used, total: used / total)
        self.service.load_names(['total'], start=0, end=100, step=1)
        with self.assertRaises(KeyError):
            self.service.resolve('ratio')
        self.service.load_names(['total', 'used'], start=0, end=100, step=1)
        self.assertEqual(self.service.resolve('ratio')['used'].name, 'used')

    def test_source_metadata_rekey_migrates_stored_composition_pointers_atomically(self):
        self.service.compose('ratio', lambda used, total: used / total)
        updated = self.service.update('used', name='usage', labels={'host': 'a'})
        self.assertEqual(self.service.get_composition('ratio').bindings['used'], updated.key)
        self.assertEqual(self.service.resolve('ratio')['used'].name, 'usage')
        with self.assertRaises(KeyError):
            self.service.update(updated.key, name='total', labels={})
        self.assertEqual(self.service.resolve('ratio')['used'].key, updated.key)
        self.service.add_additional_labels(updated.key, {'team': 'platform'})
        with self.assertRaises(ValueError):
            self.service.update(updated.key, labels={'team': 'collision'})
        self.assertEqual(self.service.resolve('ratio')['used'].labels, {'host': 'a'})

    def test_composition_crud_and_binding_immutability(self):
        plan = self.service.compose('ratio', lambda used, total: used / total)
        with self.assertRaises(TypeError):
            plan.bindings['used'] = MetricPointer('other')
        with self.assertRaises(KeyError):
            self.service.compose('ratio', lambda used: used)
        with self.assertRaises(KeyError):
            self.service.compose('ratio', lambda unknown: unknown, overwrite=True)
        self.assertIs(self.service.get_composition('ratio'), plan)
        changed = self.service.compose('ratio', lambda used: used, overwrite=True)
        self.assertEqual(changed.parameters, ('used',))
        self.assertIs(self.service.remove_composition('ratio'), changed)
        self.assertEqual(self.service.compositions, ())

    def test_collection_can_build_and_resolve_a_plan_without_registry(self):
        collection = self.service.snapshot()
        plan = collection.compose('ratio', lambda x, y: x / y,
                                  bindings={'x': 'used', 'y': 'total'})
        self.assertIsInstance(plan, MetricComposition)
        self.assertEqual(collection.resolve(plan)['x'].name, 'used')
        self.assertEqual(self.service.compositions, ())


if __name__ == '__main__':
    unittest.main()
