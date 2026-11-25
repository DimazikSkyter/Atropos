import os
from dataclasses import dataclass

import streamlit as st

from atropos_ui.clients import (
    ApiError,
    PrometheusClient,
    ServerClient,
    check_datasource,
)
from atropos_ui.demo import DemoClient
from atropos_ui.models import Datasource
from atropos_ui.service_api import (
    AccountStore,
    DatasourceService,
    MemoryConfigRepository,
)
from atropos_ui.storage import Store


@dataclass
class Context:
    store: Store | MemoryConfigRepository
    demo: bool

    @property
    def datasources(self) -> DatasourceService:
        return DatasourceService(self.store, checker=DatasourceService.demo_check if self.demo else check_datasource)

    def source_client(self, source: Datasource):
        if not source.prometheus_compatible:
            raise ApiError("Редактор PromQL поддерживает только VictoriaMetrics и Prometheus.")
        return self.demo_client() if self.demo else PrometheusClient(source.url, datasource=source)

    def server_client(self):
        if self.demo:
            return self.demo_client()
        settings = self.store.settings()
        return ServerClient(settings.server_url, settings.server_token_env)

    @staticmethod
    def demo_client():
        if "demo_client" not in st.session_state:
            st.session_state["demo_client"] = DemoClient()
        return st.session_state["demo_client"]


def configuration_store(demo: bool):
    if not demo:
        identity = st.session_state.get('identity')
        if not identity:
            raise ValueError('Требуется авторизация.')
        return AccountStore(identity)
    if "demo_store" not in st.session_state:
        store = MemoryConfigRepository()
        store.save(Datasource("local-vm", "Local VictoriaMetrics", "http://localhost:8428"))
        st.session_state["demo_store"] = store
    return st.session_state["demo_store"]


def context() -> Context:
    demo = os.getenv("ATROPOS_UI_DEMO") == "1"
    return Context(configuration_store(demo), demo)
