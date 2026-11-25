"""Use the repository's shared services without importing scientific dependencies.

The UI is run from an Atropos checkout containing the sibling detectors module.
Load the package under its own namespace so unrelated ``apps`` packages do not
interfere, and no global sys.path modification is required.
"""
import importlib.util
import sys
from pathlib import Path

PACKAGE_NAME = 'atropos_detection'
if PACKAGE_NAME not in sys.modules:
    package = Path(__file__).resolve().parents[3] / 'detectors' / 'apps'
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME, package / '__init__.py', submodule_search_locations=[str(package)],
    )
    if spec is None or spec.loader is None or not package.is_dir():
        raise ImportError('Run the UI from an Atropos checkout with detectors/apps/services present')
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(PACKAGE_NAME, None)
        raise

from atropos_detection.services.accounts import (  # noqa: E402
    AccountStore,
    account_hash,
    create_account,
    startup_accounts,
)
from atropos_detection.services.configuration import (  # noqa: E402
    MemoryConfigRepository,
    MetricDefinition,
    ServiceSettings,
)
from atropos_detection.services.datasources import (  # noqa: E402
    DATASOURCE_KINDS,
    DEFAULT_URLS,
    Datasource,
    DatasourceHealth,
    DatasourceService,
    validate_catalog,
    validate_url,
)
from atropos_detection.services.legacy_config import (  # noqa: E402
    read_legacy_configuration,
)
from atropos_detection.services.metric_sets import (  # noqa: E402
    catalog_metrics,
    definition_id,
    detector_instances,
    normalize_time,
    validate_metric_set,
)
from atropos_detection.services.postgres import (  # noqa: E402
    ConfigurationError,
    PostgresConfigRepository,
)
from atropos_detection.services.reactions import (  # noqa: E402
    aggregate_components,
    collect_components,
    group_labels,
    select_components,
    source_reference,
)

__all__ = [
    'DATASOURCE_KINDS', 'DEFAULT_URLS', 'Datasource', 'DatasourceHealth',
    'DatasourceService', 'validate_catalog', 'validate_url',
    'MemoryConfigRepository', 'MetricDefinition', 'ServiceSettings', 'read_legacy_configuration',
    'ConfigurationError', 'PostgresConfigRepository',
    'AccountStore', 'account_hash', 'create_account', 'startup_accounts',
    'aggregate_components', 'collect_components', 'group_labels', 'select_components', 'source_reference',
    'normalize_time', 'validate_metric_set', 'catalog_metrics', 'definition_id', 'detector_instances',
]
