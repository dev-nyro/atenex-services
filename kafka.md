```mermaid
graph TD
    Frontend["Frontend (Next.js)"] -->|HTTPS| APIGW["API Gateway"]

    subgraph KubernetesCluster [Kubernetes Namespace: nyro-develop]
        APIGW --> IngestAPI["Ingest Service API (FastAPI)"]

        IngestAPI -->|1 Guarda archivo| GCS[(Google Cloud Storage)]
        IngestAPI -->|2 Crea registro inicial| PostgresDB[(PostgreSQL)]
        IngestAPI -->|3 Publica Mensaje| KafkaTopic[("Kafka Topic: document_events")]

        KafkaConsumerSvc["Kafka Ingest Consumer Service"] -->|4 Lee Mensaje| KafkaTopic
        KafkaConsumerSvc -->|5 Encola Tarea Celery| CeleryBroker([Redis])

        IngestWorker["Ingest Service Worker (Celery)"] -->|6 Toma Tarea| CeleryBroker
        IngestWorker -->|7 Procesa Documento| DocProcSvc["DocProc Service"]
        IngestWorker -->|...resto del pipeline| EmbeddingSvc["Embedding Service"]
        IngestWorker -->|... | MilvusDB[(Milvus/Zilliz Cloud)]
        IngestWorker -->|...actualiza metadatos| PostgresDB
    end
