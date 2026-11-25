FROM python:3.12.10-slim
RUN pip install --no-cache-dir prometheus-client==0.22.1 confluent-kafka==2.10.0
WORKDIR /app
COPY observer.py .
CMD ["python", "-u", "observer.py"]
