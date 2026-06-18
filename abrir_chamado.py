import base64
import logging
import os
import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()


class APIWtmh():
    def __init__(self):
        self.url_api_abertura_chamado = os.getenv("URL_API_WTMH")
        self.__user_wtmh = os.getenv("USER_WTMH")
        self.__pwd_wtmh = os.getenv("PWD_WTMH")

    def abrir_chamado_wtmh(self, mensagem=None, codigo_entrada=None, debug=False):
        token = f"{self.__user_wtmh}:{self.__pwd_wtmh}"
        token_encode = base64.b64encode(token.encode('utf-8')).decode('utf-8')

        cabecalho = {
            "Content-Type": "application/json",
            "Authorization": f"Basic {token_encode}"
        }

        if debug:
            cabecalho["wtmh-debug"] = "true"

        body = {
            "tipo_chamado": 997,
            "titulo": "FAO.104 - TROCA NOTA MILHO/CAROÇO DE ALGODÃO",
            "mensagem": mensagem,
            "empresa_relacionada": 2,
            "destinatario": "c11",
            "form": {"fields": {"c2": str(codigo_entrada)}},
        }

        try:
            response = requests.post(
                self.url_api_abertura_chamado, headers=cabecalho, json=body, timeout=30
            )
            logger.debug("Resposta da API WTMH: %s", response.json())

            if response.status_code == 200:
                logger.info("Chamado aberto com sucesso.")
            else:
                logger.error("Erro ao abrir chamado. Status: %d. Body: %s", response.status_code, response.text)

            return response
        except requests.exceptions.RequestException as e:
            logger.error("Erro de conexão ao abrir chamado: %s", e)
            return None
