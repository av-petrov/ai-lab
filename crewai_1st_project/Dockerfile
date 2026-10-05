FROM python:3.12-slim

RUN pip install --no-cache-dir uv \
    && uv tool install crewai

ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /workspace

CMD ["crewai", "run"]