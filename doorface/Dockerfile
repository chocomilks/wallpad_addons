# Supervisor의 BUILD_FROM(알파인)을 쓰지 않고 데비안 slim 고정 (onnxruntime 휠이 glibc 필요)
FROM docker.io/library/python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 LANG=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client libgomp1 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app /app
WORKDIR /

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099", "--log-level", "warning"]
