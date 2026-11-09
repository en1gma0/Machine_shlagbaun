"""Запуск: python run.py  ->  http://localhost:8000"""
import logging
import sys

import uvicorn

from app import config
from app.recognizer import hub
from app.web import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(config.DATA_DIR / "gate.log", encoding="utf-8")],
)

if __name__ == "__main__":
    if not config.CAMERAS:
        sys.exit("Укажите CAMERA1_URL в файле .env")
    hub.start()
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="warning")
