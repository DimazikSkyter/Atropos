FROM python:3.12.10-slim
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY service.py .
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
