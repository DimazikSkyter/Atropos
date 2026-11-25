FROM alpine:3.21 AS compiler
RUN apk add --no-cache build-base
COPY disk-stall.c /src/disk-stall.c
RUN cc -shared -fPIC -O2 -Wall -Wextra -Werror -o /disk-stall.so /src/disk-stall.c -ldl
FROM apache/kafka:4.0.1
USER root
COPY --from=compiler /disk-stall.so /opt/disk-stall.so
RUN mkdir -p /faults /var/lib/kafka/metadata && chown -R appuser:appuser /faults /var/lib/kafka
USER appuser
