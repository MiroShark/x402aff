# Live test endpoint (test_endpoint/app.py) for Railway.
# Explicit so the build never guesses: install the endpoint's superset deps,
# copy the kit, run uvicorn from repo root so `import payto` resolves.
FROM python:3.12-slim

WORKDIR /app

# Deps first for layer caching.
COPY test_endpoint/requirements.txt ./requirements-server.txt
RUN pip install --no-cache-dir -r requirements-server.txt

# The kit modules (payto.py, settler.py, split.py, resolver.py, builder_code.py)
# plus test_endpoint/. .dockerignore keeps .env, fork-test/, and caches out.
COPY . .

ENV PYTHONUNBUFFERED=1

# Railway injects $PORT.
CMD ["sh", "-c", "uvicorn test_endpoint.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
