"""Comparable metric/reaction panels with shared time bounds and compact legends."""
import pandas as pd
import plotly.graph_objects as go

SOURCE_COLORS = ('#2563eb', '#0284c7', '#4f46e5', '#0e7490')
REACTION_COLORS = {'CUSUM': '#dc2626', 'Trend': '#16a34a', 'MAD': '#d97706',
                   'SSA': '#9333ea', 'ACF': '#b45309', 'Correlation': '#db2777',
                   'PredictionInterval': '#7c3aed', 'RuntimeTrend': '#65a30d'}
DASHES = ('solid', 'dash', 'dot', 'dashdot')


def _panel_figures(entities, *, bounds_ns=None, key='result'):
    timestamps = [stamp for e in entities for stamp in e['times']]
    bounds = bounds_ns or ([min(timestamps), max(timestamps)] if timestamps else None)
    axis_range = [pd.Timestamp(t, unit='ns', tz='UTC').tz_convert('Europe/Moscow') for t in bounds] if bounds else None
    figures = []
    for kind, title in (('metric', 'Исходные метрики'), ('reaction', 'Реакции детекторов')):
        series = [e for e in entities if e['kind'] == kind]
        if not series:
            continue
        chart, counts = go.Figure(), {}
        for index, entity in enumerate(series):
            algorithm = entity['labels'].get('detector', '') if kind == 'reaction' else ''
            variant = counts.get(algorithm, 0)
            counts[algorithm] = variant + 1
            color = REACTION_COLORS.get(algorithm, '#dc2626') if kind == 'reaction' else SOURCE_COLORS[index % len(SOURCE_COLORS)]
            labels = {k: v for k, v in entity['labels'].items() if k not in {'detector', 'detector_instance'}}
            suffix = ', '.join(f'{k}={v}' for k, v in sorted(labels.items()))
            name = entity['name'] + (' · ' + suffix if suffix else '')
            present = [value is not None for value in entity['values']]
            isolated = [available and not (index > 0 and present[index - 1])
                        and not (index + 1 < len(present) and present[index + 1])
                        for index, available in enumerate(present)]
            chart.add_trace(go.Scattergl(
                x=pd.to_datetime(entity['times'], unit='ns', utc=True).tz_convert('Europe/Moscow'),
                y=entity['values'], name=name, mode='lines+markers' if any(isolated) else 'lines', connectgaps=False,
                marker=dict(size=[8 if point else 0 for point in isolated], color=color),
                line=dict(color=color, width=2, dash=DASHES[variant % len(DASHES)] if kind == 'reaction' else 'solid'),
                hovertemplate='%{y:.5g}<extra>%{fullData.name}</extra>'))
        chart.update_layout(height=430, hovermode='x unified', uirevision=key + kind,
                            margin=dict(l=70, r=20, t=15, b=140, autoexpand=False),
                            legend=dict(orientation='h', x=0, xanchor='left', y=-.30, yanchor='top',
                                        maxheight=100, itemclick='toggle', itemdoubleclick='toggleothers'),
                            xaxis=dict(title='Время · Europe/Moscow', range=axis_range, automargin=False),
                            yaxis=dict(automargin=False))
        if kind == 'reaction' and all(v is None or 0 <= v <= 1 for e in series for v in e['values']):
            chart.update_yaxes(range=[-.03, 1.03])
        figures.append((kind, title, chart))
    return figures


def result_figures(entities, *, bounds_ns=None, key='result'):
    """Stack both panels on the SAME x axis to share native hover and zoom."""
    panels = _panel_figures(entities, bounds_ns=bounds_ns, key=key)
    if len(panels) != 2:
        return panels
    chart = go.Figure()
    for index, (_, _, panel) in enumerate(panels):
        for original in panel.data:
            trace = go.Scattergl(original)
            trace.update(xaxis='x', yaxis='y' if index == 0 else 'y2',
                         legend='legend' if index == 0 else 'legend2')
            chart.add_trace(trace)
    source_axis = panels[0][2].layout.yaxis.to_plotly_json()
    reaction_axis = panels[1][2].layout.yaxis.to_plotly_json()
    source_axis.update(domain=[.62, 1], anchor='x', title='Значение')
    reaction_axis.update(domain=[0, .38], anchor='x', title='Реакция')
    # An invisible hover target keeps the crosshair available during warmup
    # and gaps. It has no legend/tooltip and never enters result data/exports.
    # Plotly otherwise omits the spike when the hovered panel has no value.
    cursor_times = sorted({stamp for entity in entities for stamp in entity['times']})
    chart.add_trace(go.Scattergl(
        x=pd.to_datetime(cursor_times, unit='ns', utc=True).tz_convert('Europe/Moscow'),
        y=[.5] * len(cursor_times), xaxis='x', yaxis='y2', mode='lines',
        opacity=0, hoverinfo='none', showlegend=False, name='',
        line=dict(width=0), meta=dict(role='cursor-guide'),
    ))
    legend = dict(orientation='h', x=0, xanchor='left', yanchor='top',
                  maxheight=60, itemclick='toggle', itemdoubleclick='toggleothers')
    chart.update_layout(
        height=900, uirevision=key + '_paired', hovermode='x unified',
        hoversubplots='axis', hoverdistance=-1, spikedistance=-1,
        grid=dict(rows=2, columns=1, xaxes=['x'], yaxes=['y', 'y2']),
        margin=dict(l=70, r=20, t=55, b=160, autoexpand=False),
        xaxis=dict(title='Время · Europe/Moscow', range=panels[0][2].layout.xaxis.range,
                   anchor='y2', automargin=False, showticklabels=True, showspikes=True,
                   spikemode='across', spikesnap='cursor', spikedash='dot',
                   spikethickness=1, spikecolor='#94a3b8'),
        yaxis=source_axis, yaxis2=reaction_axis,
        legend={**legend, 'y': .58}, legend2={**legend, 'y': -.135},
        annotations=[
            dict(text='<b>Исходные метрики</b>', x=0, y=1.03, xref='paper', yref='paper',
                 xanchor='left', yanchor='bottom', showarrow=False, font=dict(size=22)),
            dict(text='<b>Реакции детекторов</b>', x=0, y=.43, xref='paper', yref='paper',
                 xanchor='left', yanchor='bottom', showarrow=False, font=dict(size=22)),
        ],
    )
    return [('paired', 'Метрики и реакции', chart)]
