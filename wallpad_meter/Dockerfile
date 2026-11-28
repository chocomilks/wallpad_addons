# doorface와 같은 베이스(데비안 slim) — ssh 클라이언트만 있으면 되는 가벼운 애드온
FROM docker.io/library/python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 LANG=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app /app
WORKDIR /

CMD ["python3", "-m", "app.main"]
