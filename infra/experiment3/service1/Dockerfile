FROM eclipse-temurin:21-jdk-alpine AS build
WORKDIR /src
COPY Main.java MainTest.java ./
RUN javac --add-modules jdk.httpserver Main.java MainTest.java && java -XX:+UseParallelGC --add-modules jdk.httpserver MainTest
FROM eclipse-temurin:21-jre-alpine
WORKDIR /app
COPY --from=build /src/*.class ./
ENTRYPOINT ["java", "-Xms64m", "-Xmx128m", "-XX:+UseParallelGC", "-XX:+ExitOnOutOfMemoryError", "-Xlog:gc:file=/logs/service1-gc.log:time,uptime,level,tags:filecount=0", "--add-modules", "jdk.httpserver", "Main"]
