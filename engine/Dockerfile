# syntax=docker/dockerfile:1
# Engine + API image — install into a venv on python-slim, then copy the venv to a
# clean slim runtime. Includes the Postgres driver (the production backend). numpy/
# pandas/networkx install from manylinux wheels, so no compiler is needed.
FROM python:3.12-slim AS build
WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY pyproject.toml ./
COPY engine ./engine
RUN --mount=type=cache,target=/root/.cache/pip python -m venv /venv && /venv/bin/pip install ".[postgres]"

FROM python:3.12-slim
ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    KUBEHUDDLE_DB_DRIVER=postgres
COPY --from=build /venv /venv
# Non-root runtime (numeric UID so Kubernetes runAsNonRoot can verify it).
RUN useradd --uid 10001 --create-home app
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "-m", "engine.cli"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
