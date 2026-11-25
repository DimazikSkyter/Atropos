FROM maven:3.9.9-eclipse-temurin-21 AS build
WORKDIR /src
COPY pom.xml .
RUN mvn -q dependency:go-offline
COPY src src
RUN mvn -q package -DskipTests
FROM eclipse-temurin:21-jre-jammy
WORKDIR /app
COPY --from=build /src/target/cache-sharding-1.0.jar app.jar
USER 10001
ENTRYPOINT ["java","-Xms128m","-Xmx384m","-Dorg.slf4j.simpleLogger.defaultLogLevel=warn","-jar","/app/app.jar"]
