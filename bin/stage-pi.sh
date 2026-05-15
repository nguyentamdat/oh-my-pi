#!/usr/bin/env bash
# Stage a build-context-shaped slice of $PI_ROOT under .pi-context/.
#
# We can't feed the whole pi checkout to docker — `target/` alone is >100 GB on
# a developer machine. Use rsync to pull only the files the pi-natives build
# (and the omp-rpc wheel build) actually touch.
set -euo pipefail

PI_ROOT=${PI_ROOT:-/work/pi}
STAGE=${1:-.pi-context}

if [ ! -d "$PI_ROOT" ]; then
  echo "stage-pi: PI_ROOT=$PI_ROOT does not exist" >&2
  exit 2
fi

mkdir -p "$STAGE"

# `--delete` keeps the stage faithful when pi files are removed upstream.
#
# Excludes split into three groups so the rules are easy to read:
#   1. Heavy build artifacts (target/, node_modules/, dist/, runs/, …) —
#      these would explode the context size if shipped.
#   2. Noise that drifts independently of any code change. Finder rewrites
#      `.DS_Store` on every folder peek; profilers drop `CPU.*.md` / .cpuprofile
#      blobs into the tree at random times. Letting these into `.pi-context/`
#      invalidates BuildKit's bind-mount fingerprint for the `pi` context and
#      forces the natives-builder stage to recompile every build — even when
#      no pi file the build actually reads has changed.
#   3. Pre-built natives output / per-host artifacts that the in-image build
#      regenerates anyway.
rsync -a --delete --info=stats0 \
  --exclude='target/' \
  --exclude='runs/' \
  --exclude='node_modules/' \
  --exclude='.fallow/' \
  --exclude='.worktrees/' \
  --exclude='dist/' \
  --exclude='.git/' \
  --exclude='.DS_Store' \
  --exclude='CPU.*' \
  --exclude='*.cpuprofile' \
  --exclude='*.heapprofile' \
  --exclude='*.heapsnapshot' \
  --exclude='*.swp' \
  --exclude='*.swo' \
  --exclude='*.log' \
  --exclude='packages/natives/native/.build/' \
  --exclude='packages/natives/native/pi_natives.darwin-*.node' \
  --exclude='packages/natives/native/pi_natives.dev.node' \
  --exclude='**/__pycache__/' \
  --exclude='**/*.tsbuildinfo' \
  "$PI_ROOT/" "$STAGE/"

du -sh "$STAGE" | awk '{print "stage-pi: prepared "$0}'
