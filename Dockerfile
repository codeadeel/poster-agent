# One Dockerfile, two images:
#   service : the fake poster service and the MCP server
#   lab     : JupyterLab with the source, the tests and the notebooks
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app/poster-mcp
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/poster-mcp \
    PYTHONUNBUFFERED=1
COPY poster-mcp/pyproject.toml poster-mcp/uv.lock ./
COPY poster-mcp/mock_backend ./mock_backend
COPY poster-mcp/mcp_server ./mcp_server

FROM base AS service
RUN uv sync --frozen --no-dev --no-install-project
CMD ["uvicorn", "mock_backend.app:createApp", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS lab
# Includes the dev group (pytest) so the notebooks and the Jupyter terminal can run the test suite
RUN uv sync --frozen --group demo --no-install-project
COPY poster-mcp/agent ./agent
COPY poster-mcp/tests ./tests
COPY lab /app/lab
WORKDIR /app/lab
# The token comes from JUPYTER_TOKEN. Empty means no login at all, so set one if the port is reachable by others.
CMD ["sh", "-c", "exec jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --allow-root --ServerApp.token=\"$JUPYTER_TOKEN\" --ServerApp.password= --LabApp.default_url=/lab/tree/01_mcp_tour.ipynb"]
