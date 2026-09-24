FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY fr_outreach ./fr_outreach
RUN pip install --no-cache-dir . && useradd --create-home outreach && mkdir -p data outbox && chown outreach data outbox
USER outreach
# config.yaml, templates/, data/ and outbox/ are mounted by docker-compose.yml
CMD ["fr-outreach", "agent"]
