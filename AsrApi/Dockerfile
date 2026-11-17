FROM node:24-alpine AS web-build

ARG NPM_REGISTRY=https://registry.npmjs.org/
WORKDIR /web

COPY web/package.json web/package-lock.json web/.npmrc ./
RUN npm ci --registry "$NPM_REGISTRY"

COPY web .
RUN npm run build

FROM golang:1.24-alpine AS build

WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY . .
COPY --from=web-build /web/dist ./web/dist
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/asr-api .

FROM gcr.io/distroless/static-debian12:nonroot

ENV GIN_MODE=release

COPY --from=build --chown=nonroot:nonroot /out/asr-api /asr-api

WORKDIR /

EXPOSE 8080

USER nonroot:nonroot

ENTRYPOINT ["/asr-api"]
