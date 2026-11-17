# ClusterFuzzLite's builder for the harnesses in fuzz/ (docs/agent/security.md, "Fuzzing"). The
# builder image brings Python, Atheris and PyInstaller; build.sh installs lakana-core's
# dependencies from uv.lock. Both images pinned by tag and digest; Dependabot refreshes them.
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv
FROM gcr.io/oss-fuzz-base/base-builder-python:latest@sha256:21f6fab17ace4dc76e1c56de91ee95d3d03da021df94b41056ba036f6c6f55e9
COPY --from=uv /uv /usr/local/bin/uv
# What goes into the context is listed in Dockerfile.dockerignore (the repository's own
# .dockerignore admits only the release wheels).
COPY . $SRC/lakana
WORKDIR $SRC/lakana
COPY .clusterfuzzlite/build.sh $SRC/
