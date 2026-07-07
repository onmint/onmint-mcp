# Hosted transport for the on:mint authenticity MCP server (streamable-http).
# For local use, install the package and run `onmint-mcp` over stdio instead.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV ONMINT_MCP_TRANSPORT=streamable-http
EXPOSE 8000

CMD ["onmint-mcp"]
