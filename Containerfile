FROM docker.io/library/python:3.13-slim
COPY src/smartfritz /app/smartfritz
# A named volume mounted here starts out as this directory: writable by the app's user.
RUN mkdir /data && chown 1000:1000 /data
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SMARTFRITZ_DB=/data/smartfritz.db
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]
CMD ["python", "-m", "smartfritz"]
