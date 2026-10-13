# Container image for the UHI GeoAI Cloud platform (the same image can run on
# Amazon ECS / AWS App Runner behind API Gateway).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt requirements-aws.txt ./
RUN pip install -r requirements-aws.txt

COPY . .
RUN useradd --create-home uhi && chown -R uhi /app
USER uhi

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/v1/health')"
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "5000", "--no-browser"]
