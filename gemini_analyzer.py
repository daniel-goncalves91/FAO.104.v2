import os
import re
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

try:
    from google import genai
    from google.genai import types
    GEMINI_DISPONIVEL = True
except ImportError:
    GEMINI_DISPONIVEL = False

GEMINI_API_KEY = os.environ["GEMINI_KEY_FAO_104"]
API_SIMPLES_USERNAME = os.environ["GEMINI_CREDENTIALS_USUARIO"]
API_SIMPLES_PASSWORD = os.environ["GEMINI_CREDENTIALS_SENHA"]
GEMINI_MODEL = os.getenv("modelo", "gemini-3.1-pro-preview")
GEMINI_MODEL_FALLBACK = os.getenv("modelo_fallback", "gemini-3.1-pro-preview")

_PROMPT_PATH = Path(__file__).parent / "prompt_analise.txt"
try:
    PROMPT_EXTRACAO = _PROMPT_PATH.read_text(encoding="utf-8")
except FileNotFoundError:
    raise FileNotFoundError(
        f"Arquivo de prompt não encontrado: {_PROMPT_PATH}\n"
        "Crie o arquivo 'prompt_analise.txt' na mesma pasta do script."
    )


class GeminiAnalyzerError(Exception):
    pass


# =========================
# FUNÇÕES UTILITÁRIAS
# =========================

def _parsear_json(texto: str) -> dict:
    texto = texto.strip()
    if "```" in texto:
        for bloco in texto.split("```"):
            candidato = bloco.lstrip("json").strip()
            if candidato.startswith("{"):
                texto = candidato
                break
    try:
        return json.loads(texto)
    except json.JSONDecodeError as e:
        raise GeminiAnalyzerError(
            f"Resposta do Gemini não é JSON válido: {e}\nResposta recebida: {texto[:500]}"
        ) from e


# =========================
# CLASSE GEMINI
# =========================

class GeminiAI:
    def __init__(self, token_gemini: str, modelo_primario: str, modelo_fallback: str | None = None):
        if not GEMINI_DISPONIVEL:
            raise GeminiAnalyzerError(
                "Pacote google-genai não instalado. Execute: pip install google-genai"
            )
        self._client = genai.Client(api_key=token_gemini)
        self._modelo_primario = modelo_primario
        self._modelo_fallback = modelo_fallback

    _MIME_TYPES = {
        ".pdf":  "application/pdf",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif":  "image/gif",
        ".webp": "image/webp",
        ".tiff": "image/tiff",
        ".tif":  "image/tiff",
    }

    @staticmethod
    def _detectar_mime(path: str) -> str:
        ext = Path(path).suffix.lower()
        return GeminiAI._MIME_TYPES.get(ext, "application/pdf")

    @staticmethod
    def _file_part(path: str) -> "types.Part":
        mime = GeminiAI._detectar_mime(path)
        with open(path, "rb") as f:
            return types.Part.from_bytes(data=f.read(), mime_type=mime)

    def _gerar_conteudo(self, partes: list) -> str:
        try:
            return self._client.models.generate_content(
                model=self._modelo_primario,
                contents=partes,
            ).text
        except Exception as erro_primario:
            if self._modelo_fallback is None:
                raise GeminiAnalyzerError(f"Falha no modelo primário: {erro_primario}") from erro_primario
            try:
                return self._client.models.generate_content(
                    model=self._modelo_fallback,
                    contents=partes,
                ).text
            except Exception as erro_fallback:
                raise GeminiAnalyzerError(
                    f"Falha nos dois modelos. "
                    f"Primário: {erro_primario} | Fallback: {erro_fallback}"
                ) from erro_fallback

    def analisar_arquivo(self, path: str, prompt: str) -> dict:
        return _parsear_json(self._gerar_conteudo([prompt, self._file_part(path)]))

    def analisar_dois_arquivos(self, path_ticket: str, path_laudo: str, prompt: str) -> dict:
        partes = [prompt, self._file_part(path_ticket), self._file_part(path_laudo)]
        return _parsear_json(self._gerar_conteudo(partes))


# =========================
# FUNÇÕES DE INTEGRAÇÃO
# =========================

_cliente_cache: GeminiAI | None = None


def _criar_cliente() -> GeminiAI:
    global _cliente_cache
    if _cliente_cache is None:
        if not GEMINI_API_KEY:
            raise GeminiAnalyzerError("tokenAPIGoogleGemini não definido no .env")
        _cliente_cache = GeminiAI(GEMINI_API_KEY, GEMINI_MODEL, GEMINI_MODEL_FALLBACK)
    return _cliente_cache


def extrair_dados_documentos(caminho_ticket: str, caminho_laudo: str) -> dict:
    return _criar_cliente().analisar_dois_arquivos(caminho_ticket, caminho_laudo, PROMPT_EXTRACAO)


def _apenas_digitos(valor) -> str:
    return "".join(ch for ch in str(valor) if ch.isdigit())


def _normalizar_numero(valor) -> float | None:
    if valor is None:
        return None
    try:
        return float(str(valor).replace(",", "."))
    except (ValueError, TypeError):
        return None


def _precisao_decimal(valor) -> int:
    s = str(valor).replace(",", ".")
    if "." in s:
        return len(s.split(".")[1])
    return 0


def _truncar(valor: float, casas: int) -> float:
    import math
    fator = 10 ** casas
    return math.trunc(valor * fator) / fator


def _normalizar_placa(valor) -> str | None:
    if not valor:
        return None
    return re.sub(r"[-\s]", "", str(valor)).upper()


def validar_contra_pendencia(dados: dict, pendencia: dict) -> dict:
    erros = []
    avisos = []

    # Número do contrato extraído do laudo deve corresponder a id_contrato OU contrato_original.
    # Inválido somente se diferir dos dois.
    id_contrato = pendencia.get("id_contrato") or pendencia.get("ID_CONTRATO")
    contrato_original = pendencia.get("contrato_original") or pendencia.get("CONTRATO_ORIGINAL")
    contrato_laudo = dados.get("laudo", {}).get("numero_contrato")

    if contrato_laudo is None:
        avisos.append("Contrato: não encontrado no laudo (validação ignorada)")
    elif id_contrato is None and contrato_original is None:
        avisos.append("Contrato: campos id_contrato e contrato_original não disponíveis na API (validação ignorada)")
    else:
        contrato_laudo_d = _apenas_digitos(contrato_laudo)
        bate_id = id_contrato is not None and _apenas_digitos(id_contrato) == contrato_laudo_d
        bate_original = contrato_original is not None and _apenas_digitos(contrato_original) == contrato_laudo_d
        if not bate_id and not bate_original:
            erros.append(
                f"Contrato divergente: laudo={contrato_laudo!r} "
                f"(esperado id_contrato={id_contrato!r} ou contrato_original={contrato_original!r})"
            )

    apenas_qtd = dados.get("apenas_valida_quantidade", False)
    if apenas_qtd:
        tipo = dados.get("laudo", {}).get("tipo_produto") or "produto especial"
        avisos.append(f"Produto '{tipo}' identificado como especial: validando apenas quantidade")
        return {"erros": erros, "avisos": avisos, "apenas_valida_quantidade": True}

    ticket = dados.get("ticket", {})
    laudo = dados.get("laudo", {})
    analise = pendencia.get("analise") or {}

    if ticket.get("ibf_agro_pecuaria"):
        avisos.append(
            f"Ticket emitido por IBF AGRO PECUARIA "
            f"(emissor: {ticket.get('emissor')}): campo Motorista não validado"
        )

    # Comparação dos valores extraídos dos PDFs contra os dados registrados no sistema
    comparacoes = [
        (ticket, "peso_tara",                  "PESO TARA",  "Peso Tara"),
        (ticket, "peso_bruto",                 "PESO BRUTO", "Peso Bruto"),
        (laudo,  "avariados",                  "AVARIADOS",  "Avariados"),
        (laudo,  "quebrados",                  "QUEBRADOS",  "Quebrados"),
        (laudo,  "materias_estranhas_impurezas","IMPUREZA",   "Impureza"),
        (laudo,  "umidade",                    "UMIDADE",    "Umidade"),
    ]

    for secao, campo, chave_analise, label in comparacoes:
        val_extraido = _normalizar_numero(secao.get(campo))
        val_sistema_raw = analise.get(chave_analise)
        val_sistema = _normalizar_numero(val_sistema_raw)

        if val_sistema is None:
            avisos.append(f"{label}: não informado pela API (validação ignorada)")
        elif val_extraido is None:
            erros.append(f"{label}: não extraído do documento (esperado: {val_sistema})")
        else:
            precisao = _precisao_decimal(val_sistema_raw)
            if _truncar(val_extraido, precisao) != val_sistema:
                erros.append(
                    f"{label} divergente: documento={val_extraido} != sistema={val_sistema}"
                )

    # Peso líquido: calculado do sistema e comparado com o que está nos documentos
    peso_bruto_sistema = _normalizar_numero(analise.get("PESO BRUTO"))
    peso_tara_sistema  = _normalizar_numero(analise.get("PESO TARA"))
    if peso_bruto_sistema is not None and peso_tara_sistema is not None:
        pl_sistema = peso_bruto_sistema - peso_tara_sistema
        for doc, campo, label in [
            (ticket, "peso_liquido", "Peso Líquido (ticket)"),
            (laudo,  "peso_liquido", "Peso Líquido (laudo)"),
        ]:
            pl_doc = _normalizar_numero(doc.get(campo))
            if pl_doc is None:
                avisos.append(f"{label}: não encontrado no documento (sistema: {pl_sistema:.0f} kg)")
            elif pl_doc != pl_sistema:
                erros.append(
                    f"Peso Líquido divergente ({label.split('(')[1].rstrip(')')}): "
                    f"documento={pl_doc:.0f} kg != sistema={pl_sistema:.0f} kg"
                )

    placa_ticket = _normalizar_placa(ticket.get("placa"))
    placa_laudo  = _normalizar_placa(laudo.get("placa"))

    if placa_ticket is None and placa_laudo is None:
        avisos.append("Placa: não encontrada em nenhum dos documentos")
    elif placa_ticket is None:
        avisos.append(f"Placa: não extraída do ticket (laudo: {laudo.get('placa')})")
    elif placa_laudo is None:
        avisos.append(f"Placa: não encontrada no laudo (ticket: {ticket.get('placa')})")
    elif placa_ticket != placa_laudo:
        erros.append(
            f"Placa divergente: ticket={ticket.get('placa')!r} != laudo={laudo.get('placa')!r}"
        )

    return {"erros": erros, "avisos": avisos, "apenas_valida_quantidade": False}


def analisar_e_validar(caminho_ticket: str, caminho_laudo: str, pendencia: dict) -> dict:
    dados = extrair_dados_documentos(caminho_ticket, caminho_laudo)
    validacao = validar_contra_pendencia(dados, pendencia)
    return {
        "aprovado": len(validacao["erros"]) == 0,
        "dados_extraidos": dados,
        "validacao": validacao,
    }
