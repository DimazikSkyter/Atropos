FROM maven:3.9.11-eclipse-temurin-21 AS build
WORKDIR /build
COPY pom.xml ./
RUN mvn -q -B dependency:go-offline
COPY src ./src
RUN mvn -q -B package
RUN java -jar target/gc-distribution-experiment-1.0.jar --self-test

FROM eclipse-temurin:21-jre
WORKDIR /app
RUN mkdir -p /results && (command -v wget >/dev/null || (apt-get update && apt-get install -y --no-install-recommends wget && rm -rf /var/lib/apt/lists/*))
COPY --from=build /build/target/gc-distribution-experiment-1.0.jar /app/service.jar
ENV JAVA_TOOL_OPTIONS="-Xms768m -Xmx768m -Xmn192m -XX:+UseParallelGC -XX:ParallelGCThreads=1 -XX:SurvivorRatio=2 -XX:MaxTenuringThreshold=15 -XX:-UseAdaptiveSizePolicy -Xlog:gc*,safepoint:file=/results/jvm-gc.log:time,uptime,level,tags:filecount=3,filesize=20M"
ENV RESULTS_DIR=/results PORT=8080 BASELINE_SECONDS=180 RAMP_SECONDS=900 HOLD_SECONDS=180 ALLOC_START_MIB=16 ALLOC_END_MIB=640 RETAIN_MILLIS=60 FIXED_WORK_MILLIS=8
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/service.jar"]
