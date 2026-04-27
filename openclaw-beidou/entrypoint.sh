#!/usr/bin/env bash
set -e

# Proxy relay: forward loopback port to host proxy
# OpenClaw requires Discord proxy to target loopback
socat TCP-LISTEN:7891,bind=127.0.0.1,fork,reuseaddr TCP:host.docker.internal:7890 &
PROXY_PID=$!
trap "kill $PROXY_PID 2>/dev/null" EXIT

exec "$@"
