import os
import subprocess
from pathlib import Path

# Carpetas de microservicios a buscar (puedes agregar más si es necesario)
MICROSERVICES = [
    "api-gateway",
    "embedding-service",
    "ingest-service",
    "query-service",
    "reranker-service",
    "docproc-service",
    "sparse-search-service",
]

ROOT = Path(__file__).parent.resolve()


def run_export_codebase(service_dir):
    script_path = service_dir / "export_codebase.py"
    if not script_path.exists():
        print(f"[❌] No se encontró export_codebase.py en {service_dir}")
        return False
    try:
        # Ejecutar el script con el mismo intérprete de Python
        result = subprocess.run([
            os.sys.executable, str(script_path)
        ], cwd=service_dir, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"[✅] Codebase exportado para {service_dir.name}")
        else:
            print(f"[⚠️] Error exportando {service_dir.name}:")
            print(result.stdout)
            print(result.stderr)
        return result.returncode == 0
    except Exception as e:
        print(f"[❌] Excepción ejecutando {script_path}: {e}")
        return False

def main():
    print("Actualizando codebase de todos los microservicios...\n")
    for service in MICROSERVICES:
        service_dir = ROOT / service
        if service_dir.exists() and service_dir.is_dir():
            run_export_codebase(service_dir)
        else:
            print(f"[❌] Carpeta no encontrada: {service}")
    print("\nProceso finalizado.")

if __name__ == "__main__":
    main()
