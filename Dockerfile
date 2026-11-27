# tool-router — self-contained routing service.
#
#   docker build -t tool-router .
#   docker run -p 127.0.0.1:8080:8080 tool-router                # empty registry
#   docker run -p 127.0.0.1:8080:8080 -v ./catalog.json:/app/catalog.json \
#              tool-router serve --catalog /app/catalog.json      # pre-loaded
#
# The LLM key is injected at RUN time only (never baked into the image):
#   docker run -e GEMINI_API_KEY=... -e TOOL_ROUTER_LLM_PROVIDER=gemini ...      (live-verified)

FROM python:3.13-slim

WORKDIR /app

# Install the package first for layer caching, then the source.
COPY pyproject.toml README.md ./
COPY tool_router ./tool_router
RUN pip install --no-cache-dir ".[api,mcp]"

# Non-root user (production hygiene).
RUN useradd --create-home router && chown -R router /app
USER router

EXPOSE 8080
ENTRYPOINT ["tool-router"]
# 0.0.0.0 is required inside a container. The API has no authentication, so publish
# the port on loopback only (-p 127.0.0.1:8080:8080) unless the network is trusted.
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
