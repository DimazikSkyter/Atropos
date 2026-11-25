ARG CASSANDRA_IMAGE=cassandra:4.1.12
FROM ${CASSANDRA_IMAGE}

ARG JMX_EXPORTER_VERSION=1.6.0

USER root
# Starting with 1.1.0 the shaded agent is distributed as a GitHub release asset,
# not through Maven Central.
ADD https://github.com/prometheus/jmx_exporter/releases/download/${JMX_EXPORTER_VERSION}/jmx_prometheus_javaagent-${JMX_EXPORTER_VERSION}.jar /opt/jmx-exporter/jmx_prometheus_javaagent.jar
COPY monitoring/jmx-exporter.yaml /opt/jmx-exporter/config.yaml
COPY scripts/fs-exporter.py /opt/lab/fs-exporter.py
RUN chmod 0444 /opt/jmx-exporter/jmx_prometheus_javaagent.jar /opt/jmx-exporter/config.yaml \
    && chmod 0555 /opt/lab/fs-exporter.py \
    && printf '\n-javaagent:/opt/jmx-exporter/jmx_prometheus_javaagent.jar=7070:/opt/jmx-exporter/config.yaml\n' >> /etc/cassandra/jvm-server.options
