# Performance Benchmark Results: hkunlp/instructor-large

- **Service URL:** http://localhost:8003
- **Model Name (from /health):** hkunlp/instructor-large
- **Model Dimension:** 768
- **Timestamp:** 2025-06-12T16:10:27.364798
- **Benchmark Tool:** `measure_performance.py`

## Test Configuration
- **Batch Size Tested:** 64
- **Total Iterations Planned:** 10
- **Successful Iterations:** 10
- **Average Words per Text:** 30

## Performance Metrics
| Metric                      | Value         |
|-----------------------------|---------------|
| Average Latency (s)         | 3.957       |
| Median Latency (s)          | 3.969      |
| StdDev Latency (s)          | 0.076      |
| Min Latency (s)             | 3.779       |
| Max Latency (s)             | 4.060       |
| Average Throughput (texts/s)| 16.18      |
| Median Throughput (texts/s) | 16.12   |
| StdDev Throughput (texts/s) | 0.32   |
| Min Throughput (texts/s)    | 15.76      |
| Max Throughput (texts/s)    | 16.94      |

### Raw Data per Iteration
**Latencies (seconds):**
```
[
  3.779,
  4.06,
  3.937,
  3.97,
  3.959,
  3.875,
  3.994,
  4.03,
  3.996,
  3.969
]
```

**Throughputs (texts/second):**
```
[
  16.94,
  15.76,
  16.26,
  16.12,
  16.16,
  16.52,
  16.03,
  15.88,
  16.02,
  16.13
]
```

---
*Note on VRAM: To measure server-side VRAM usage accurately, please monitor tools like `nvidia-smi` on the machine running the embedding microservice while these tests are active.*
