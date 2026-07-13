import os
import requests
import base64
import json
import sys
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
import logging

load_dotenv()

# importado após load_dotenv() para que as variáveis de ambiente já estejam disponíveis
from gemini_analyzer import analisar_e_validar, GeminiAnalyzerError, GEMINI_API_KEY
from abrir_chamado import APIWtmh

# =========================
# CONFIGURAÇÃO
# =========================
CLIENT_ID = os.environ["GEMINI_CREDENTIALS_USUARIO"]
CLIENT_SECRET = os.environ["GEMINI_CREDENTIALS_SENHA"]

if not CLIENT_ID or not CLIENT_SECRET:
    raise Exception("INPASA_CLIENT_ID ou INPASA_CLIENT_SECRET não definidos no .env")

TOKEN_URL = "https://aplicativo.inpasa.com.br/apex/inpasa/oauth/token"
BASE_URL = "https://aplicativo.inpasa.com.br/ords/apex/inpasa/v1/api/roberty"

TIMEOUT = 120
LOG_DIR = Path(__file__).parent / "logs"
ABRIR_CHAMADO = os.getenv("ABRIR_CHAMADO", "true").strip().lower() == "true"
_MAX_PREVIEW_CHARS = 120
_CHAMADOS_FILE = Path(__file__).parent / "chamados_abertos.json"
_PROCESSAMENTOS_FILE = Path(__file__).parent / "processamentos_enviados.json"
_CONFIRMACOES_FILE = Path(__file__).parent / "confirmacoes_enviadas.json"


# =========================
# EXCEPTIONS
# =========================
class InpasaApiError(Exception):
    pass


class InpasaEmptyResult(Exception):
    pass



# =========================
# LOGGING
# =========================
LOG_FILE: Path | None = None
_log_buffer: list = []
_buffering = False


def init_log():
    global LOG_FILE
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d")
    LOG_FILE = LOG_DIR / f"inpasa_{date}.log"


def _write_to_file(msg: str):
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


def flush_buffer():
    global _log_buffer, _buffering
    for line in _log_buffer:
        print(line)
        _write_to_file(line)
    _log_buffer = []
    _buffering = False


def discard_buffer(summary: str):
    global _log_buffer, _buffering
    _log_buffer = []
    _buffering = False
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write_to_file(f"[{ts}] {summary}")


def redact_large_fields(data):
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            if k in ("anexo_ticket", "anexo_laudo"):
                cleaned[k] = f"<BASE64 OMITIDO - {len(v)} caracteres>" if isinstance(v, str) else "<BINARIO OMITIDO>"
            elif k == "anexo_laudo_classif":
                if isinstance(v, str):
                    cleaned[k] = f"{v[:_MAX_PREVIEW_CHARS]}... <TOTAL {len(v)} caracteres>"
                else:
                    cleaned[k] = "<FORMATO INVALIDO>"
            else:
                cleaned[k] = redact_large_fields(v)
        return cleaned

    if isinstance(data, list):
        return [redact_large_fields(item) for item in data]

    return data


def log_line(msg: str):
    if _buffering:
        _log_buffer.append(msg)
    else:
        print(msg)
        _write_to_file(msg)


def log_json(title: str, data):
    log_line(title)
    formatted = json.dumps(redact_large_fields(data), indent=2, ensure_ascii=False)
    log_line(formatted)


# =========================
# CONTROLE DE CHAMADOS
# =========================
def _carregar_chamados() -> dict:
    if _CHAMADOS_FILE.exists():
        try:
            return json.loads(_CHAMADOS_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def chamado_ja_aberto(id_hist) -> bool:
    return str(id_hist) in _carregar_chamados()


def registrar_chamado(id_hist, numero_chamado, motivo: str = ""):
    chamados = _carregar_chamados()
    chamados[str(id_hist)] = {
        "numero": numero_chamado,
        "data": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "motivo": motivo,
    }
    _CHAMADOS_FILE.write_text(json.dumps(chamados, indent=2, ensure_ascii=False), encoding="utf-8")


def motivo_chamado_anterior(id_hist) -> str:
    entrada = _carregar_chamados().get(str(id_hist))
    return entrada.get("motivo", "") if entrada else ""


def remover_chamado(id_hist):
    chamados = _carregar_chamados()
    chamados.pop(str(id_hist), None)
    _CHAMADOS_FILE.write_text(json.dumps(chamados, indent=2, ensure_ascii=False), encoding="utf-8")


# =========================
# CONTROLE DE PROCESSAMENTOS
# =========================
def _carregar_processamentos() -> dict:
    if _PROCESSAMENTOS_FILE.exists():
        try:
            return json.loads(_PROCESSAMENTOS_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def processamento_ja_enviado(id_hist) -> bool:
    return str(id_hist) in _carregar_processamentos()


def registrar_processamento(id_hist):
    procs = _carregar_processamentos()
    procs[str(id_hist)] = {"data": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    _PROCESSAMENTOS_FILE.write_text(json.dumps(procs, indent=2, ensure_ascii=False), encoding="utf-8")


# =========================
# CONTROLE DE CONFIRMAÇÕES
# =========================
def _carregar_confirmacoes() -> dict:
    if _CONFIRMACOES_FILE.exists():
        try:
            return json.loads(_CONFIRMACOES_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def confirmacao_ja_enviada(id_hist) -> bool:
    return str(id_hist) in _carregar_confirmacoes()


def registrar_confirmacao(id_hist):
    confs = _carregar_confirmacoes()
    confs[str(id_hist)] = {"data": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    _CONFIRMACOES_FILE.write_text(json.dumps(confs, indent=2, ensure_ascii=False), encoding="utf-8")


# =========================
# ABERTURA DE CHAMADOS
# =========================
def _tentar_abrir_chamado(id_hist, cod_entrada, mensagem: str, motivo_key: str):
    if not ABRIR_CHAMADO:
        log_line("  ⚠ Abertura de chamado desabilitada (ABRIR_CHAMADO=false)")
        return
    if chamado_ja_aberto(id_hist) and motivo_chamado_anterior(id_hist) == motivo_key:
        log_line(f"  ⚠ Chamado já aberto com mesmo motivo para ID {id_hist}, ignorando")
        return
    resp_chamado = APIWtmh().abrir_chamado_wtmh(mensagem=mensagem, codigo_entrada=cod_entrada)
    if resp_chamado is not None and resp_chamado.status_code == 200:
        numero = resp_chamado.json().get("numero", "")
        log_line(f"  ✔ Chamado aberto com sucesso: #{numero}")
        registrar_chamado(id_hist, numero, motivo=motivo_key)
    elif resp_chamado is not None:
        log_line(f"  ✖ Erro ao abrir chamado ({resp_chamado.status_code}): {resp_chamado.text}")


# =========================
# LIMPEZA DE ARQUIVOS DE CONTROLE
# =========================
_TTL_DIAS = 7


def _limpar_por_ttl(arquivo: Path):
    if not arquivo.exists():
        return
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    limite = datetime.now() - timedelta(days=_TTL_DIAS)
    limpos = {
        k: v for k, v in dados.items()
        if datetime.strptime(v.get("data", "1970-01-01 00:00:00"), "%Y-%m-%d %H:%M:%S") > limite
    }
    if len(limpos) < len(dados):
        removidos = len(dados) - len(limpos)
        arquivo.write_text(json.dumps(limpos, indent=2, ensure_ascii=False), encoding="utf-8")
        log_line(f"[LIMPEZA] {arquivo.name}: {removidos} entradas removidas por TTL ({_TTL_DIAS} dias)")


def _limpar_ausentes_da_lista(arquivo: Path, ids_ativos: set):
    if not arquivo.exists():
        return
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    limpos = {k: v for k, v in dados.items() if k in ids_ativos}
    if len(limpos) < len(dados):
        removidos = len(dados) - len(limpos)
        arquivo.write_text(json.dumps(limpos, indent=2, ensure_ascii=False), encoding="utf-8")
        log_line(f"[LIMPEZA] {arquivo.name}: {removidos} entradas removidas (IDs fora da lista de pendências)")


# =========================
# HELPERS
# =========================
def parse_error_response(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return data.get("message") or data.get("error") or json.dumps(data, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False)
    except ValueError:
        return resp.text.strip() or f"HTTP {resp.status_code}"


def detectar_extensao_por_bytes(data: bytes) -> str | None:
    if data[:4] == b"%PDF":
        return ".pdf"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tiff"
    return None


def get_file_extension(content_type: str | None, fallback: str = ".pdf") -> str:
    if not content_type:
        return fallback
    mapping = {
        "application/pdf": ".pdf",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "text/plain": ".txt",
    }
    return mapping.get(content_type.lower(), fallback)


def sanitize_filename(name: str) -> str:
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name.strip()



def auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }


_logger = logging.getLogger(__name__)

@retry(
    retry=retry_if_exception_type(requests.Timeout),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=5, max=30),
    before_sleep=before_sleep_log(_logger, logging.WARNING),
    reraise=True,
)
def _executar_request(method: str, url: str, headers, params, json_body) -> requests.Response:
    return requests.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        json=json_body,
        timeout=TIMEOUT,
    )


def request_json(method: str, url: str, *, headers=None, params=None, json_body=None):
    try:
        resp = _executar_request(method, url, headers, params, json_body)
    except requests.Timeout:
        raise InpasaApiError(f"Timeout ao conectar em {url} após 3 tentativas (>{TIMEOUT}s cada)")
    except requests.ConnectionError as e:
        raise InpasaApiError(f"Erro de conexão em {url}: {e}")

    log_line(f"[HTTP] {method} {url} -> {resp.status_code}")

    if resp.status_code == 406:
        raise InpasaEmptyResult(parse_error_response(resp) or "vazio")

    if resp.status_code >= 500:
        log_line(f"  body bruto: {resp.text[:1000]}")
        raise InpasaApiError(
            f"Erro interno da API ({resp.status_code}): {parse_error_response(resp)}"
        )

    if resp.status_code < 200 or resp.status_code >= 300:
        raise InpasaApiError(
            f"Erro HTTP {resp.status_code}: {parse_error_response(resp)}"
        )

    if not resp.text.strip():
        return {}

    try:
        return resp.json()
    except ValueError:
        raise InpasaApiError(
            f"Resposta inválida da API. Esperado JSON, recebido: {resp.text[:500]}"
        )


# =========================
# AUTH
# =========================
def get_access_token() -> str:
    token_raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    encoded = base64.b64encode(token_raw).decode()

    headers = {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }

    resp = requests.post(
        TOKEN_URL,
        headers=headers,
        params={"grant_type": "client_credentials"},
        timeout=TIMEOUT,
    )

    log_line(f"[AUTH] POST {TOKEN_URL}?grant_type=client_credentials -> {resp.status_code}")

    if resp.status_code >= 500:
        raise InpasaApiError(
            f"Erro interno ao obter token ({resp.status_code}): {parse_error_response(resp)}"
        )

    if resp.status_code < 200 or resp.status_code >= 300:
        raise InpasaApiError(
            f"Erro ao obter token ({resp.status_code}): {parse_error_response(resp)}"
        )

    try:
        data = resp.json()
    except ValueError:
        raise InpasaApiError(f"Resposta inválida ao obter token: {resp.text[:500]}")

    access_token = data.get("access_token")
    if not access_token:
        raise InpasaApiError(f"Token não encontrado na resposta: {data}")

    log_line("[AUTH] Token obtido com sucesso")
    return access_token


# =========================
# API CALLS
# =========================
def listar_pendencias(token: str) -> list:
    try:
        data = request_json(
            "GET",
            f"{BASE_URL}/pendencia_troca_nota",
            headers=auth_headers(token),
        )
        if not isinstance(data, list):
            raise InpasaApiError(f"Resposta inesperada em pendências: {data}")
        return data
    except InpasaEmptyResult:
        return []


def obter_ticket(token: str, id_hist: int | str) -> dict | None:
    try:
        data = request_json(
            "GET",
            f"{BASE_URL}/anexo_ticket",
            headers=auth_headers(token),
            params={"pn_id_hist_entrc": id_hist},
        )
        if not isinstance(data, dict):
            raise InpasaApiError(f"Resposta inesperada no ticket do ID {id_hist}: {data}")
        return data
    except InpasaEmptyResult:
        return None


def obter_laudo(token: str, id_hist: int | str) -> dict | None:
    try:
        data = request_json(
            "GET",
            f"{BASE_URL}/anexo_laudo",
            headers=auth_headers(token),
            params={"pn_id_hist_entrc": id_hist},
        )
        if not isinstance(data, dict):
            raise InpasaApiError(f"Resposta inesperada no laudo do ID {id_hist}: {data}")
        return data
    except InpasaEmptyResult:
        return None


def processar_entrada(token: str, id_hist: int | str, id_rateio: int | str | None = None) -> dict:
    url = f"{BASE_URL}/processar_entrada"
    params = {"pn_id_hist_entrc": str(id_hist)}
    if id_rateio is not None:
        params["pn_id_rateioentradamp"] = str(id_rateio)
    log_line(f"[POST] {url}")
    log_line(f"  params: pn_id_hist_entrc={id_hist}, pn_id_rateioentradamp={id_rateio}")
    return request_json("POST", url, headers=auth_headers(token), params=params, json_body={})


def confirmar_entrada(token: str, id_hist: int | str) -> dict:
    url = f"{BASE_URL}/confirmar_entrada"
    params = {"pn_id_hist_entrc": str(id_hist)}
    log_line(f"[POST] {url}")
    log_line(f"  params: pn_id_hist_entrc={id_hist}")
    return request_json("POST", url, headers=auth_headers(token), params=params, json_body={})


def finalizar(token: str, id_hist: int | str, id_rateio: int | str | None = None) -> dict:
    url = f"{BASE_URL}/finaliza_sequencia"
    params = {"pn_id_hist_entrc": str(id_hist)}
    if id_rateio is not None:
        params["pn_id_rateioentradamp"] = str(id_rateio)
    log_line(f"[POST] {url}")
    log_line(f"  params: pn_id_hist_entrc={id_hist}, pn_id_rateioentradamp={id_rateio}")
    return request_json("POST", url, headers=auth_headers(token), params=params, json_body={})

# =========================
# EXTRAÇÃO DE CAMPOS
# =========================
def extrair_id_hist(p: dict) -> int | str | None:
    return (
        p.get("id_hist_entrc")
        or p.get("pn_id_hist_entrc")
        or p.get("ID_HIST_ENTRC")
    )


def extrair_cod_entrada(p: dict) -> int | str | None:
    return (
        p.get("cod_entrada")
        or p.get("COD_ENTRADA")
        or p.get("codigo_entrada")
        or p.get("CODIGO_ENTRADA")
    )


def extrair_id_rateio(p: dict) -> int | str | None:
    return (
        p.get("id_rateioentradamp")
        or p.get("pn_id_rateioentradamp")
        or p.get("ID_RATEIOENTRADAMP")
    )


def extrair_status(p: dict) -> str:
    valor = (
        p.get("status")
        or p.get("STATUS")
        or p.get("situacao")
        or p.get("SITUACAO")
        or ""
    )
    return str(valor).strip().upper()


# =========================
# SALVAMENTO DE ARQUIVOS
# =========================
def _decodificar_conteudo(conteudo: str) -> bytes:
    _HEX_PREFIXES = ("FFD8FF", "25504446", "255044", "89504E47", "49492A00", "4D4D002A")
    if conteudo[:16].upper().startswith(_HEX_PREFIXES):
        return bytes.fromhex(conteudo)
    return base64.b64decode(conteudo + "=" * (-len(conteudo) % 4))


def salvar_ticket(ticket: dict, dir_saida: str, id_hist: int | str) -> str | None:
    conteudo = ticket.get("anexo_ticket")
    if not conteudo:
        return None

    nome_original = ticket.get("nome_arquivo_ticket")
    ext = get_file_extension(ticket.get("tipo_arquivo_ticket"), fallback=".pdf")
    nome_arquivo = sanitize_filename(nome_original) if nome_original else f"ticket_{id_hist}{ext}"

    caminho = str(Path(dir_saida) / nome_arquivo)
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)

    try:
        dados = _decodificar_conteudo(conteudo)

        ext_real = detectar_extensao_por_bytes(dados)
        if ext_real and Path(caminho).suffix.lower() != ext_real:
            caminho = str(Path(caminho).with_suffix(ext_real))

        try:
            with open(caminho, "wb") as f:
                f.write(dados)
        except PermissionError:
            p = Path(caminho)
            caminho = str(p.with_stem(p.stem + f"_{id_hist}"))
            with open(caminho, "wb") as f:
                f.write(dados)
    except Exception as e:
        raise InpasaApiError(f"Erro ao salvar ticket ID {id_hist}: {e}") from e

    return caminho


def salvar_laudo(laudo: dict, dir_saida: str, id_hist: int | str) -> str | None:
    conteudo = (
        laudo.get("anexo_laudo")
        or laudo.get("anexo")
        or laudo.get("anexo_laudo_classif")
    )
    if not conteudo:
        return None

    ext_laudo = get_file_extension(
        laudo.get("tipo_arquivo_laudo_classif")
        or laudo.get("tipo_arquivo_laudo")
        or laudo.get("tipo_arquivo"),
        fallback=".pdf"
    )
    nome_arquivo = (
        laudo.get("nome_arquivo_laudo_classif")
        or laudo.get("nome_arquivo_laudo")
        or laudo.get("nome_arquivo")
        or f"laudo_{id_hist}{ext_laudo}"
    )
    caminho = str(Path(dir_saida) / sanitize_filename(nome_arquivo))
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)

    try:
        dados = _decodificar_conteudo(conteudo)

        ext_real = detectar_extensao_por_bytes(dados)
        if ext_real and Path(caminho).suffix.lower() != ext_real:
            caminho = str(Path(caminho).with_suffix(ext_real))

        try:
            with open(caminho, "wb") as f:
                f.write(dados)
        except PermissionError:
            p = Path(caminho)
            caminho = str(p.with_stem(p.stem + f"_{id_hist}"))
            with open(caminho, "wb") as f:
                f.write(dados)
    except Exception as e:
        raise InpasaApiError(f"Erro ao salvar laudo ID {id_hist}: {e}") from e

    return caminho


# =========================
# COMANDOS
# =========================
def cmd_listar():
    token = get_access_token()
    pendencias = listar_pendencias(token)

    if not pendencias:
        log_line("Nenhuma pendência encontrada.")
        return

    log_line(f"Total de pendências: {len(pendencias)}\n")
    for i, p in enumerate(pendencias, start=1):
        log_json(f"Pendência #{i}", p)
        log_line("-" * 80)


def cmd_processar(dir_saida: str, finalizar_seq: bool):
    global _buffering
    _buffering = True

    try:
        token = get_access_token()
        pendencias = listar_pendencias(token)
    except Exception:
        flush_buffer()
        raise

    if not pendencias:
        flush_buffer()
        log_line("Sem pendências encontradas.")
        log_line(f"\nLog salvo em: {LOG_FILE}")
        return

    flush_buffer()

    ids_ativos = {str(extrair_id_hist(p)) for p in pendencias if extrair_id_hist(p)}
    for arquivo in (_PROCESSAMENTOS_FILE, _CONFIRMACOES_FILE):
        _limpar_ausentes_da_lista(arquivo, ids_ativos)
        _limpar_por_ttl(arquivo)

    Path(dir_saida).mkdir(parents=True, exist_ok=True)
    log_line(f"Total de pendências: {len(pendencias)}")

    for p in pendencias:
        id_hist = extrair_id_hist(p)
        id_rateio = extrair_id_rateio(p)
        cod_entrada = extrair_cod_entrada(p)

        if not id_hist:
            log_json("\n⚠ Pendência sem ID válido:", p)
            continue

        log_line(f"\n▶ Processando ID {id_hist}")
        log_json("Dados da pendência:", p)

        caminho_ticket = None
        caminho_laudo = None

        status = extrair_status(p)

        if not status:
            log_line(f"  ✖ Status ausente ou nulo para ID {id_hist}")
            mensagem = f"ID {id_hist} (entrada {cod_entrada}) retornou status vazio ou nulo"
            _tentar_abrir_chamado(id_hist, cod_entrada, mensagem, "STATUS_VAZIO")
            continue

        if status == "PENDENTE":
            # Ticket
            try:
                ticket = obter_ticket(token, id_hist)
                if ticket:
                    log_json("Retorno bruto do ticket:", ticket)
                    caminho_ticket = salvar_ticket(ticket, dir_saida, id_hist)
                    if caminho_ticket:
                        log_line(f"  ✔ Ticket salvo em: {caminho_ticket}")
                    else:
                        log_line("  ⚠ Ticket retornado sem campo anexo_ticket")
                else:
                    log_line("  ⚠ Ticket não encontrado")
            except Exception as e:
                log_line(f"  ✖ Erro ao obter/salvar ticket: {e}")

            # Laudo
            try:
                laudo = obter_laudo(token, id_hist)
                if laudo:
                    log_json("Retorno bruto do laudo:", laudo)
                    caminho_laudo = salvar_laudo(laudo, dir_saida, id_hist)
                    if caminho_laudo:
                        log_line(f"  ✔ Laudo salvo em: {caminho_laudo}")
                    else:
                        log_line("  ⚠ Laudo retornado sem campo de anexo esperado")
                else:
                    log_line("  ⚠ Laudo não encontrado")
            except Exception as e:
                log_line(f"  ✖ Erro ao obter/salvar laudo: {e}")

        if status == "AGUARDANDOFINALIZAR":
            try:
                resp_finaliza = finalizar(token, id_hist, id_rateio)
                log_line("  ✔ finaliza_sequencia enviado com sucesso")
                if resp_finaliza:
                    log_json("Retorno da API:", resp_finaliza)
            except Exception as e:
                log_line(f"  ✖ Erro ao chamar finaliza_sequencia: {e}")
        elif status == "FINALIZADO":
            if confirmacao_ja_enviada(id_hist):
                log_line(f"  ⚠ confirmar_entrada já enviado anteriormente para ID {id_hist}, ignorando")
            else:
                try:
                    resp_confirmacao = confirmar_entrada(token, id_hist)
                    log_line("  ✔ confirmar_entrada enviado com sucesso")
                    registrar_confirmacao(id_hist)
                    if resp_confirmacao:
                        log_json("Retorno da API:", resp_confirmacao)
                except Exception as e:
                    log_line(f"  ✖ Erro ao chamar confirmar_entrada: {e}")
        elif status == "INCONSISTENTE":
            erro_retornado = p.get("inconsistencia") or json.dumps(p, ensure_ascii=False)
            log_line(f"  ✖ Status INCONSISTENTE: {erro_retornado}")
            mensagem = f"ID {id_hist} com status INCONSISTENTE:\n{erro_retornado}"
            motivo_key = "INCONSISTENTE:" + erro_retornado.strip().splitlines()[0].strip()
            _tentar_abrir_chamado(id_hist, cod_entrada, mensagem, motivo_key)
        elif status == "PENDENTE":
            analise_aprovada = True
            motivos_reprovacao: list[str] = []
            if not GEMINI_API_KEY:
                log_line("  ⚠ Análise Gemini ignorada: tokenAPIGoogleGemini não configurado")
            elif caminho_ticket and caminho_laudo:
                try:
                    resultado = analisar_e_validar(caminho_ticket, caminho_laudo, p)
                    log_json("  Análise Gemini:", resultado["dados_extraidos"])
                    for aviso in resultado["validacao"]["avisos"]:
                        log_line(f"  ⚠ {aviso}")
                    if resultado["aprovado"]:
                        log_line("  ✔ Análise Gemini: APROVADO")
                    else:
                        analise_aprovada = False
                        motivos_reprovacao = resultado["validacao"]["erros"]
                        log_line("  ✖ Análise Gemini: REPROVADO")
                        for erro in motivos_reprovacao:
                            log_line(f"    • {erro}")
                        motivos_formatados = [e.replace("!=", "diferente de") for e in motivos_reprovacao]
                        mensagem = f"Entrada {cod_entrada} reprovado:\n" + "\n".join(f"• {m}" for m in motivos_formatados)
                        motivo_key = "GEMINI:" + "|".join(sorted(motivos_reprovacao))
                        _tentar_abrir_chamado(id_hist, cod_entrada, mensagem, motivo_key)
                except GeminiAnalyzerError as e:
                    analise_aprovada = False
                    motivos_reprovacao = [str(e)]
                    log_line(f"  ✖ Erro na análise Gemini: {e}")
                except Exception as e:
                    analise_aprovada = False
                    motivos_reprovacao = [f"Erro inesperado: {e}"]
                    log_line(f"  ✖ Erro inesperado na análise Gemini: {e}")
            else:
                log_line("  ⚠ Análise Gemini ignorada: ticket ou laudo não disponíveis")

            if finalizar_seq:
                if processamento_ja_enviado(id_hist):
                    log_line(f"  ⚠ processar_entrada já enviado anteriormente para ID {id_hist}, ignorando")
                elif not analise_aprovada:
                    log_line("  ✖ Processamento bloqueado. Motivo(s):")
                    for motivo in motivos_reprovacao:
                        log_line(f"    • {motivo}")
                else:
                    try:
                        resp_processamento = processar_entrada(token, id_hist, id_rateio)
                        log_line("  ✔ processar_entrada enviado com sucesso")
                        registrar_processamento(id_hist)
                        remover_chamado(id_hist)
                        if resp_processamento:
                            log_json("Retorno da API:", resp_processamento)
                    except Exception as e:
                        log_line(f"  ✖ Erro ao chamar processar_entrada: {e}")

    log_line(f"\nLog salvo em: {LOG_FILE}")


def cmd_finalizar(id_hist, id_rateio=None):
    token = get_access_token()
    resp = finalizar(token, id_hist, id_rateio)
    log_line(f"✔ Solicitação de finalização enviada para o ID {id_hist}")
    if resp:
        log_json("Retorno da finalização:", resp)


# =========================
# CLI
# =========================
def build_parser():
    parser = argparse.ArgumentParser(description="API Troca de Nota - INPASA")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    subparsers.add_parser("listar", help="Lista pendências de troca de nota")

    parser_processar = subparsers.add_parser(
        "processar",
        help="Baixa ticket/laudo de todas as pendências e opcionalmente finaliza",
    )
    parser_processar.add_argument(
        "--dir",
        default=str(Path(__file__).parent / "ticket_laudo"),
        help="Diretório de saída dos arquivos",
    )
    parser_processar.add_argument(
        "--finalizar",
        action="store_true",
        help="Envia solicitação de finalização após baixar os anexos",
    )

    parser_finalizar = subparsers.add_parser(
        "finalizar",
        help="Finaliza uma pendência específica pelo ID do histórico",
    )
    parser_finalizar.add_argument(
        "--id",
        required=True,
        type=int,
        help="Valor de pn_id_hist_entrc",
    )
    parser_finalizar.add_argument(
        "--id-rateio",
        type=int,
        default=None,
        dest="id_rateio",
        help="Valor de pn_id_rateioentradamp",
    )

    return parser


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        init_log()

        if len(sys.argv) == 1:
            cmd_processar(str(Path(__file__).parent / "ticket_laudo"), True)
        else:
            parser = build_parser()
            args = parser.parse_args()

            if args.cmd == "listar":
                cmd_listar()
            elif args.cmd == "processar":
                cmd_processar(args.dir, args.finalizar)
            elif args.cmd == "finalizar":
                cmd_finalizar(args.id, args.id_rateio)

    except KeyboardInterrupt:
        log_line("\nExecução cancelada pelo usuário.")
    except Exception as e:
        log_line(f"Erro: {e}")
        raise
