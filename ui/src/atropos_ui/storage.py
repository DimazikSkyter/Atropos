"""Production configuration belongs to the shared PostgreSQL service repository."""
from atropos_ui.service_api import ConfigurationError as ConfigurationError
from atropos_ui.service_api import PostgresConfigRepository as Store

__all__ = ['ConfigurationError', 'Store']
