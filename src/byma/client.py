"""
Cliente HTTP para la API pública de BYMA Open Data.
Endpoints /free/ no requieren autenticación.
"""

import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free"

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "on-radar/1.0",
}


class BYMAClient:
    def __init__(self, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.timeout = timeout

    def get(self, endpoint: str, params: Optional[dict] = None) -> object:
        url = f"{BASE_URL}/{endpoint}"
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def post(self, endpoint: str, payload: Optional[dict] = None) -> object:
        url = f"{BASE_URL}/{endpoint}"
        response = self.session.post(url, json=payload or {}, timeout=self.timeout)
        response.raise_for_status()
        return response.json()
