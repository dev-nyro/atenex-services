# Performance Benchmark Results: intfloat/e5-large-v2

- **Service URL:** http://localhost:8003
- **Model Name (from /health):** intfloat/e5-large-v2
- **Model Dimension:** 1024
- **Timestamp:** 2025-06-13T08:29:18.853004
- **Benchmark Tool:** `measure_performance.py`

## Test Configuration
- **Batch Size Tested:** 64
- **Total Iterations Planned:** 10
- **Successful Iterations:** 10
- **Average Words per Text:** 30

## Performance Metrics
| Metric                      | Value         |
|-----------------------------|---------------|
| Average Latency (s)         | 2.639       |
| Median Latency (s)          | 2.645      |
| StdDev Latency (s)          | 0.017      |
| Min Latency (s)             | 2.606       |
| Max Latency (s)             | 2.667       |
| Average Throughput (texts/s)| 24.25      |
| Median Throughput (texts/s) | 24.20   |
| StdDev Throughput (texts/s) | 0.16   |
| Min Throughput (texts/s)    | 24.00      |
| Max Throughput (texts/s)    | 24.56      |

### Raw Data per Iteration
**Latencies (seconds):**
```
[
  2.657,
  2.667,
  2.643,
  2.647,
  2.627,
  2.606,
  2.647,
  2.631,
  2.619,
  2.646
]
```

**Throughputs (texts/second):**
```
[
  24.09,
  24.0,
  24.21,
  24.18,
  24.37,
  24.56,
  24.18,
  24.32,
  24.44,
  24.19
]
```

---
*Note on VRAM: To measure server-side VRAM usage accurately, please monitor tools like `nvidia-smi` on the machine running the embedding microservice while these tests are active.*
