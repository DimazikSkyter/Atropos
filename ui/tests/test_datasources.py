import base64

import httpx
import pytest

from atropos_ui.clients import ApiError, PrometheusClient, check_datasource
from atropos_ui.models import Datasource, MetricDefinition
from atropos_ui.service_api import DatasourceService, MemoryConfigRepository


def test_shared_service_default_unique_names_and_revisions():
    service = DatasourceService(MemoryConfigRepository())
    vm = service.create('VM', 'http://vm')
    ch = service.create('Warehouse', 'http://ch', 'ClickHouse', is_default=True,
                        database='metrics', auth_type='basic', username='reader', password_env='CH_PASSWORD')
    assert service.default().id == ch.id
    assert not service.get(vm.id).is_default
    before = service.list()
    with pytest.raises(ValueError, match='уникальн'):
        service.update(vm.id, name='warehouse', is_default=True)
    assert service.list() == before
    with pytest.raises(ValueError, match='ревизии'):
        service.save(vm)
    service.delete(ch.id)
    assert service.default().id == vm.id


def test_used_sources_cannot_be_deleted_or_retyped():
    store = MemoryConfigRepository()
    service = DatasourceService(store)
    vm = service.create('VM', 'http://vm')
    service.create('Prom', 'http://prom', 'Prometheus')
    store.save(MetricDefinition('derived', vm.id, 'up'))
    before = service.list()
    with pytest.raises(ValueError, match='используется'):
        service.delete(vm.id)
    with pytest.raises(ValueError, match='PromQL'):
        service.update(vm.id, kind='ClickHouse')
    assert service.list() == before
    ch = service.create('Warehouse', 'http://ch', 'ClickHouse')
    with pytest.raises(ValueError, match='PromQL'):
        store.save(MetricDefinition('wrong', ch.id, 'up'))


@pytest.mark.parametrize('kind', ['VictoriaMetrics', 'Prometheus'])
def test_prometheus_health_keeps_cluster_prefix_and_uses_bearer(monkeypatch, kind):
    monkeypatch.setenv('VM_TOKEN', 'private-token')
    source = Datasource('vm', 'VM', 'https://vm/select/42/prometheus', kind, token_env='VM_TOKEN')
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.path == '/select/42/prometheus/api/v1/query'
        assert request.url.params['query'] == 'vector(1)'
        assert request.headers['Authorization'] == 'Bearer private-token'
        return httpx.Response(200, json={'status': 'success', 'data': {
            'resultType': 'vector', 'result': [{'metric': {}, 'value': [10, '1']}]}})
    result = check_datasource(source, transport=httpx.MockTransport(handler))
    assert result.status == 'ok'
    assert len(requests) == 1


def test_clickhouse_health_checks_database_and_basic_auth(monkeypatch):
    monkeypatch.setenv('CH_PASSWORD', 'private-password')
    source = Datasource('ch', 'CH', 'https://proxy/clickhouse', 'ClickHouse', database='observability',
                        auth_type='basic', username='reader', password_env='CH_PASSWORD')
    def handler(request):
        assert request.url.path == '/clickhouse/'
        assert request.url.params['database'] == 'observability'
        assert request.url.params['query'] == 'SELECT 1 AS value FORMAT JSON'
        assert request.url.params['readonly'] == '2'
        encoded = base64.b64encode(b'reader:private-password').decode()
        assert request.headers['Authorization'] == 'Basic ' + encoded
        assert 'private-password' not in str(request.url)
        return httpx.Response(200, json={'data': [{'value': 1}]})
    assert check_datasource(source, transport=httpx.MockTransport(handler)).status == 'ok'


@pytest.mark.parametrize('version', ['1', '2', '3'])
def test_influx_health_uses_the_selected_protocol(monkeypatch, version):
    monkeypatch.setenv('INFLUX_TOKEN', 'private-token')
    settings = {'influx_version': version, 'database': 'metrics'}
    if version == '2':
        settings.update(organization='org', bucket='metrics "quoted"', token_env='INFLUX_TOKEN')
    elif version == '3':
        settings.update(token_env='INFLUX_TOKEN')
    source = Datasource('influx', 'Influx', 'http://influx/prefix', 'InfluxDB', **settings)
    def handler(request):
        if version == '1':
            assert request.url.path == '/prefix/query'
            assert request.url.params['db'] == 'metrics'
            assert request.url.params['q'] == 'SHOW MEASUREMENTS LIMIT 1'
            return httpx.Response(200, json={'results': [{'statement_id': 0}]})
        if version == '2':
            assert request.method == 'POST'
            assert request.url.path == '/prefix/api/v2/query'
            assert request.url.params['org'] == 'org'
            assert request.headers['Authorization'] == 'Token private-token'
            assert request.headers['Content-Type'] == 'application/vnd.flux'
            assert request.content.decode().startswith('from(bucket: "metrics \\"quoted\\"")')
            return httpx.Response(200, text='#datatype,string,long\n,result,table\n')
        assert request.url.path == '/prefix/api/v3/query_sql'
        assert request.url.params['db'] == 'metrics'
        assert request.url.params['q'] == 'SHOW TABLES'
        assert request.headers['Authorization'] == 'Bearer private-token'
        return httpx.Response(200, json=[])
    assert check_datasource(source, transport=httpx.MockTransport(handler)).status == 'ok'


@pytest.mark.parametrize('response', [
    httpx.Response(401, text='private-token'), httpx.Response(302, headers={'Location': 'https://other/'}),
    httpx.Response(200, text='<html>private-token</html>'), httpx.Response(200, json={'status': 'error'}),
])
def test_connection_errors_are_reported_without_response_secrets(response):
    source = Datasource('vm', 'VM', 'http://vm')
    result = check_datasource(source, transport=httpx.MockTransport(lambda request: response))
    assert result.status == 'error'
    assert 'private-token' not in result.message


def test_missing_secret_and_network_timeout_are_health_errors(monkeypatch):
    monkeypatch.delenv('ABSENT_PASSWORD', raising=False)
    source = Datasource('ch', 'CH', 'http://ch', 'ClickHouse', auth_type='basic',
                        username='reader', password_env='ABSENT_PASSWORD')
    result = check_datasource(source, transport=httpx.MockTransport(lambda request: pytest.fail('No request expected')))
    assert result.status == 'error' and 'ABSENT_PASSWORD' in result.message
    def fail(request):
        raise httpx.ReadTimeout('private details')
    result = check_datasource(Datasource('vm', 'VM', 'http://vm'), transport=httpx.MockTransport(fail))
    assert result.status == 'error' and 'private details' not in result.message


@pytest.mark.parametrize('data', [
    {'resultType': 'vector', 'result': []},
    {'resultType': 'vector', 'result': [{'metric': {}, 'value': [10, 'NaN']}]},
    {'resultType': 'vector', 'result': [{'metric': {}}]},
])
def test_prometheus_health_rejects_malformed_or_missing_probe_values(data):
    result = check_datasource(Datasource('vm', 'VM', 'http://vm'), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'status': 'success', 'data': data})))
    assert result.status == 'error'


def test_flux_stream_error_and_influxql_error_are_not_success(monkeypatch):
    monkeypatch.setenv('TOKEN', 'private-token')
    flux = Datasource('flux', 'Flux', 'http://influx', 'InfluxDB', token_env='TOKEN', organization='org', bucket='metrics')
    response = '#datatype,string,string\n,error,reference\n,private-token,123\n'
    result = check_datasource(flux, transport=httpx.MockTransport(lambda request: httpx.Response(200, text=response)))
    assert result.status == 'error' and 'private-token' not in result.message
    influxql = Datasource('ql', 'InfluxQL', 'http://influx', 'InfluxDB', influx_version='1', database='metrics')
    result = check_datasource(influxql, transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={'results': [{'error': 'private-token'}]})))
    assert result.status == 'error' and 'private-token' not in result.message


def test_saved_query_client_obeys_auth_timeout_and_minimum_step(monkeypatch):
    monkeypatch.setenv('PASSWORD', 'private-password')
    source = Datasource('vm', 'VM', 'http://vm', auth_type='basic', username='reader', password_env='PASSWORD',
                        timeout_seconds=7, min_interval_seconds=60)
    def handler(request):
        assert request.headers['Authorization'].startswith('Basic ')
        assert request.extensions['timeout']['read'] == 7
        return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'matrix', 'result': []}})
    client = PrometheusClient(source.url, datasource=source, transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match='не меньше'):
        client.range('up', 0, 600, 15)
    assert client.range('up', 0, 600, 60)['result'] == []
    with pytest.raises(ApiError):
        PrometheusClient('http://vm', 'ABSENT_TOKEN', transport=httpx.MockTransport(handler)).instant('up')
