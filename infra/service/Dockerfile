FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/app/ui/.venv/bin:$PATH \
    HOME=/tmp/atropos

RUN python -m pip install --no-cache-dir uv==0.12.10 \
    && useradd --uid 10001 --no-create-home atropos
WORKDIR /app
COPY ui/pyproject.toml ui/uv.lock ui/app.py /app/ui/
COPY ui/src /app/ui/src
COPY detectors/apps /app/detectors/apps
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --project ui
USER 10001:10001

FROM runtime AS server
EXPOSE 8000
CMD ["python", "-m", "atropos_ui.runtime_server", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS ui
EXPOSE 8501
CMD ["python", "-m", "atropos_ui.serve", "--host", "0.0.0.0", "--port", "8501"]
