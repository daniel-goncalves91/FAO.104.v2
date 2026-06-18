import time
import subprocess
from datetime import datetime

INTERVALO = 120

while True:
    inicio = time.time()

    print(f"\n[{datetime.now()}] Iniciando execução...")

    try:
        result = subprocess.run(
            ["python", "main.py", "processar", "--finalizar"],
            capture_output=True,
            text=True,
            encoding="utf-8"
        )

        print(result.stdout)

        if result.stderr:
            print("ERRO:")
            print(result.stderr)

    except Exception as e:
        print(f"Falha ao executar: {e}")

    tempo_execucao = time.time() - inicio
    tempo_espera = max(0, INTERVALO - tempo_execucao)

    print(f"[{datetime.now()}] Execução levou {tempo_execucao:.2f}s")
    print(f"Aguardando {tempo_espera:.2f}s...\n")

    time.sleep(tempo_espera)