import os
from pathlib import Path

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
OUTPUT_MD = ROOT / "ALL_READMES.md"


def aggregate_readmes():
    with open(OUTPUT_MD, "w", encoding="utf-8") as outfile:
        outfile.write("# Documentación de Microservicios\n\n")
        for service in MICROSERVICES:
            service_dir = ROOT / service
            readme_path = service_dir / "README.md"
            if readme_path.exists():
                outfile.write(f"\n---\n\n## {service}\n\n")
                with open(readme_path, "r", encoding="utf-8") as infile:
                    outfile.write(infile.read())
                    outfile.write("\n")
            else:
                outfile.write(f"\n---\n\n## {service}\n\n*README.md no encontrado.*\n")
    print(f"Archivo '{OUTPUT_MD.name}' generado correctamente.")


if __name__ == "__main__":
    aggregate_readmes()
