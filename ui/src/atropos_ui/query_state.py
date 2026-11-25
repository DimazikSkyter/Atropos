"""Move query drafts between pages; view time ranges live only in the session."""
from dataclasses import asdict

import streamlit as st


def open_query(source, query, definition=None):
    draft = {'datasource_id': source.id, 'query': query}
    if definition is not None:
        draft.update(asdict(definition), catalog_name=definition.name)
    st.session_state['pending_query'] = draft
    st.session_state['requested_page'] = 'queries'
    st.rerun()


def open_catalog(source, name=None):
    st.session_state['pending_catalog'] = {'datasource_id': source.id, 'name': name}
    st.session_state['requested_page'] = 'catalog'
    st.rerun()
