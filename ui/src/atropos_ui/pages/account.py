import streamlit as st

from atropos_ui.service_api import AccountStore


def render(ctx):
    st.title('Настройки учётной записи')
    if not isinstance(ctx.store, AccountStore):
        st.info('Настройки доступны после входа в рабочую учётку.')
        return
    store = ctx.store
    _, revision = store.snapshot()
    preferences = store.preferences()
    st.caption('Эти настройки, каталог, источники, привязки и результаты относятся только к вашей учётке.')
    st.info('Сеты, сохранённые метрики и последнее рассчитанное окно каждого сета сохраняются в PostgreSQL всегда. '
            'Настройки ниже относятся к прежнему импорту и пересчёту InfluxDB. '
            'При наличии сетов он запускается вручную; автообновление сетов задаётся в мониторинге.')
    with st.form('account_preferences'):
        persist = st.checkbox('Сохранять результаты детекторов', value=preferences['persist_results'])
        st.caption('По умолчанию результаты живут в памяти сервиса. При включении сохраняется последний прогон; '
                   'выключение удаляет сохранённую копию. Исходная история остаётся в InfluxDB.')
        login = st.checkbox('Пересчитывать при новой авторизации', value=preferences['rebuild_on_login'])
        startup = st.checkbox('Пересчитывать при запуске сервиса', value=preferences['rebuild_on_startup'])
        if st.form_submit_button('Сохранить настройки учётки', type='primary'):
            store.save_preferences({'persist_results': persist, 'rebuild_on_login': login,
                                    'rebuild_on_startup': startup}, expected_revision=revision)
            st.success('Настройки сохранены. Для применения новых параметров выполните пересчёт.')
