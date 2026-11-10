# Учёт проездов через шлагбаум - образ Docker
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Tashkent \
    DB_JOURNAL=DELETE

# tzdata - местное время в журнале; libglib - нужна OpenCV
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

USER app
# Модели распознавания скачиваются при сборке и хранятся в образе - при запуске интернет не нужен
RUN python -c "from fast_alpr import ALPR; ALPR(detector_model='yolo-v9-t-640-license-plate-end2end', ocr_model='cct-s-v2-global-model')"

COPY --chown=app:app app/ app/
COPY --chown=app:app templates/ templates/
COPY --chown=app:app static/ static/
COPY --chown=app:app run.py .

# Настройки (.env) и данные (база, фото, копии, логи) подключаются снаружи - см. docker-compose.yml
VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=4)" || exit 1

CMD ["python", "run.py"]
