# Multi-stage: compile a static binary, ship it on distroless — a few MB, runs as
# nonroot, no shell or package manager (nothing to exploit or patch). Bases pinned
# by multi-arch index digest (reproducible on amd64 CI + arm64 dev).
# Bump: docker buildx imagetools inspect <image>

FROM golang:1.25-alpine@sha256:523c3effe300580ed375e43f43b1c9b091b68e935a7c3a92bfcc4e7ed55b18c2 AS build
WORKDIR /src

# No third-party deps (stdlib only), so there's nothing to `go mod download`.
# Copying go.mod first still lets Docker cache the module-resolution layer.
COPY go.mod ./
COPY main.go ./

# CGO disabled -> fully static binary that runs on the distroless base.
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /incident-generator .

FROM gcr.io/distroless/static:nonroot@sha256:963fa6c544fe5ce420f1f54fb88b6fb01479f054c8056d0f74cc2c6000df5240
COPY --from=build /incident-generator /incident-generator
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/incident-generator"]
