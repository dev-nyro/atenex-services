# Script: cleanup_milvus_collection.py
from pymilvus import connections, utility, Collection, MilvusException

# --- Configuración ---
MILVUS_HOST = "milvus-standalone.nyro-develop.svc.cluster.local"  # Conectamos al servicio Milvus dentro del cluster
MILVUS_PORT = "19530"
COLLECTION_NAME = "atenex_collection" # Nombre de la colección a eliminar
CONNECTION_ALIAS = "local_cleanup"
# --- Fin Configuración ---

print(f"Intentando conectar a Milvus en {MILVUS_HOST}:{MILVUS_PORT} (via port-forward)...")

try:
    # Conectar usando el alias
    connections.connect(alias=CONNECTION_ALIAS, host=MILVUS_HOST, port=MILVUS_PORT)
    print(f"INFO: Conectado a Milvus con alias '{CONNECTION_ALIAS}'.")

    # Verificar si la colección existe
    if utility.has_collection(COLLECTION_NAME, using=CONNECTION_ALIAS):
        print(f"INFO: Colección '{COLLECTION_NAME}' encontrada.")
        try:
            collection_to_drop = Collection(name=COLLECTION_NAME, using=CONNECTION_ALIAS)
            # Opcional: Liberar la colección de memoria si está cargada (puede ayudar antes de borrar)
            try:
                print(f"INFO: Intentando liberar la colección '{COLLECTION_NAME}' de la memoria...")
                collection_to_drop.release()
                print(f"INFO: Colección '{COLLECTION_NAME}' liberada.")
            except MilvusException as release_err:
                 # No es fatal si falla la liberación, puede que no estuviera cargada
                 print(f"ADVERTENCIA: No se pudo liberar la colección (puede que no estuviera cargada): {release_err}")
            except Exception as release_err_other:
                 print(f"ADVERTENCIA: Error inesperado al liberar la colección: {release_err_other}")


            # Eliminar la colección
            print(f"INFO: Intentando eliminar la colección '{COLLECTION_NAME}'...")
            utility.drop_collection(COLLECTION_NAME, using=CONNECTION_ALIAS)
            print(f"¡ÉXITO! Colección '{COLLECTION_NAME}' eliminada correctamente.")

        except MilvusException as drop_err:
            print(f"ERROR: Falló la eliminación de la colección '{COLLECTION_NAME}'. Código: {drop_err.code}, Mensaje: {drop_err.message}")
        except Exception as e:
            print(f"ERROR: Error inesperado durante la eliminación de la colección: {e}")
    else:
        print(f"INFO: La colección '{COLLECTION_NAME}' no existe. No se requiere eliminación.")

except MilvusException as conn_err:
    print(f"ERROR: Falló la conexión a Milvus en {MILVUS_HOST}:{MILVUS_PORT}. Código: {conn_err.code}, Mensaje: {conn_err.message}")
    print("Asegúrate de que el comando 'kubectl port-forward' se esté ejecutando en otra terminal.")
except Exception as e:
    print(f"ERROR: Error inesperado durante la conexión o verificación: {e}")

finally:
    # Siempre intentar desconectar
    try:
        if CONNECTION_ALIAS in connections.list_connections()[0]: # Verificar si el alias existe
             connections.disconnect(CONNECTION_ALIAS)
             print(f"INFO: Desconectado de Milvus (alias '{CONNECTION_ALIAS}').")
    except Exception as disconn_e:
        print(f"ERROR: Error al intentar desconectar: {disconn_e}")

print("Script finalizado.")