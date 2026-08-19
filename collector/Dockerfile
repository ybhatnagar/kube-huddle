# Collector image — multi-stage: build a static Go binary, ship on distroless.
# The pure-Go SQLite/pgx drivers mean CGO_ENABLED=0 → a fully static binary that
# runs on the tiny distroless/static base (no libc, non-root).
FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/collector ./cmd/collector

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/collector /usr/local/bin/collector
USER nonroot:nonroot
ENTRYPOINT ["/usr/local/bin/collector"]
CMD ["--help"]
