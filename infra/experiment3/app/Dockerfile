FROM golang:1.26.1-alpine AS build
WORKDIR /src
COPY go.mod main.go main_test.go ./
RUN go test ./... && go vet ./... && CGO_ENABLED=0 go build -trimpath -o /experiment3 .
FROM alpine:3.23
COPY --from=build /experiment3 /experiment3
USER 65534:65534
ENTRYPOINT ["/experiment3"]
