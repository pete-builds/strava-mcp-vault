FROM python:3.13-slim AS builder

WORKDIR /app

# Copy only what the build backend needs to resolve metadata, so a source-only
# edit does not invalidate the dependency layer.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.13-slim

WORKDIR /app

COPY --from=builder /install /usr/local

RUN useradd -r -m appuser && \
    mkdir -p /app/data && \
    chown appuser:appuser /app/data

USER appuser

EXPOSE 18201

# Console script from [project.scripts]. The package is installed into
# site-packages, so nothing is imported from the working directory.
CMD ["strava-mcp-vault"]
