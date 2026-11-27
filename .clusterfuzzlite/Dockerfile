# ClusterFuzzLite's builder for the harnesses in fuzz/ (docs/agent/security.md, "Fuzzing"). The
# builder image brings Python, Atheris and PyInstaller; build.sh installs lakana-core's
# dependencies from uv.lock. Both images pinned by tag and digest; Dependabot refreshes them.
FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv
FROM gcr.io/oss-fuzz-base/base-builder-python:latest@sha256:0aa4f12ee4e9a4aaa1e511b3ded82d662475150b82a786478f22d5c41adbacbd
COPY --from=uv /uv /usr/local/bin/uv
# What goes into the context is listed in Dockerfile.dockerignore (the repository's own
# .dockerignore admits only the release wheels).
COPY . $SRC/lakana
WORKDIR $SRC/lakana
COPY .clusterfuzzlite/build.sh $SRC/
