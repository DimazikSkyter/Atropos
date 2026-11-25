"""Display immutable metadata stored with a calculation, including legacy runs."""
import streamlit as st


def metadata_panel(result, metric_id=None):
    st.caption('Параметры сохранены при расчёте. Текущие настройки метрики могут отличаться.')
    st.json({k: result[k] for k in ('run_id', 'revision', 'started_at', 'finished_at', 'execution_mode',
                                   'bounds_ns', 'session_id', 'configuration_fingerprint') if k in result})
    for uid, metric in result.get('metrics', {}).items():
        if metric_id is not None and uid != metric_id:
            continue
        for name, output in metric.get('detectors', {}).items():
            with st.expander(metric['name'] + ' · ' + name):
                st.json({'metric_id': uid, **{k: metric[k] for k in
                    ('datasource_id', 'measurement', 'field') if k in metric}})
                metadata = output.get('metadata')
                if metadata:
                    st.json(metadata)
                else:
                    st.info('Старый прогон: полные параметры запуска не сохранены. Пересчитайте метрику.')
                    st.json({'saved_parameters': output.get('parameters', {}), 'complete': False})
                st.caption('Статус: ' + output.get('status', 'unknown') + '. Score не является вероятностью.')
                st.dataframe([{'Ряд / пара': s.get('label', ''), 'Готовых точек': s.get('ready_points', 0),
                               'Статус': s.get('status', output.get('status', ''))}
                              for s in output.get('series', output.get('series_summary', []))],
                             hide_index=True, use_container_width=True)
