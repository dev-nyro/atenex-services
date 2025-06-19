# embedding-service/benchmarking/measure_performance.py
import time
import random
import string
import requests
import numpy as np
import argparse
import json
import os
from datetime import datetime

def generate_random_texts(num_texts: int, avg_words_per_text: int = 8, word_length: int = 5) -> list[str]:
    """Generates a list of random texts."""
    texts = []
    for _ in range(num_texts):
        num_words = random.randint(max(1, avg_words_per_text - avg_words_per_text // 2), avg_words_per_text + avg_words_per_text // 2)
        words = ["".join(random.choices(string.ascii_lowercase, k=word_length)) for _ in range(num_words)]
        texts.append(" ".join(words))
    return texts

def create_markdown_summary(summary_data: dict, filepath: str):
    """Creates a Markdown summary from the performance data."""
    md_content = f"# Performance Benchmark Results: {summary_data.get('model_name_from_health', 'N/A')}\n\n"
    md_content += f"- **Service URL:** {summary_data['service_url']}\n"
    md_content += f"- **Model Name (from /health):** {summary_data.get('model_name_from_health', 'N/A')}\n"
    md_content += f"- **Model Dimension:** {summary_data['model_dimension']}\n"
    md_content += f"- **Timestamp:** {summary_data['timestamp']}\n"
    md_content += f"- **Benchmark Tool:** `measure_performance.py`\n"
    md_content += "\n## Test Configuration\n"
    md_content += f"- **Batch Size Tested:** {summary_data['batch_size_tested']}\n"
    md_content += f"- **Total Iterations Planned:** {summary_data['num_total_iterations']}\n"
    md_content += f"- **Successful Iterations:** {summary_data['num_successful_iterations']}\n"
    md_content += f"- **Average Words per Text:** {summary_data['avg_words_per_text']}\n"

    if summary_data['num_successful_iterations'] > 0:
        md_content += "\n## Performance Metrics\n"
        md_content += "| Metric                      | Value         |\n"
        md_content += "|-----------------------------|---------------|\n"
        md_content += f"| Average Latency (s)         | {summary_data['avg_latency_seconds']:.3f}       |\n"
        md_content += f"| Median Latency (s)          | {summary_data['median_latency_seconds']:.3f}      |\n"
        md_content += f"| StdDev Latency (s)          | {summary_data['stddev_latency_seconds']:.3f}      |\n"
        md_content += f"| Min Latency (s)             | {summary_data['min_latency_seconds']:.3f}       |\n"
        md_content += f"| Max Latency (s)             | {summary_data['max_latency_seconds']:.3f}       |\n"
        md_content += f"| Average Throughput (texts/s)| {summary_data['avg_throughput_texts_per_sec']:.2f}      |\n"
        md_content += f"| Median Throughput (texts/s) | {summary_data['median_throughput_texts_per_sec']:.2f}   |\n"
        md_content += f"| StdDev Throughput (texts/s) | {summary_data['stddev_throughput_texts_per_sec']:.2f}   |\n"
        md_content += f"| Min Throughput (texts/s)    | {summary_data['min_throughput_texts_per_sec']:.2f}      |\n"
        md_content += f"| Max Throughput (texts/s)    | {summary_data['max_throughput_texts_per_sec']:.2f}      |\n"

        md_content += "\n### Raw Data per Iteration\n"
        md_content += "**Latencies (seconds):**\n```\n"
        md_content += json.dumps(summary_data['all_latencies_seconds'], indent=2) + "\n```\n\n"
        md_content += "**Throughputs (texts/second):**\n```\n"
        md_content += json.dumps(summary_data['all_throughputs_texts_per_sec'], indent=2) + "\n```\n"
    else:
        md_content += "\n## Performance Metrics\n"
        md_content += "No successful iterations to report detailed metrics.\n"
        if "error_message" in summary_data:
            md_content += f"Error: {summary_data['error_message']}\n"

    md_content += "\n---\n"
    md_content += "*Note on VRAM: To measure server-side VRAM usage accurately, "
    md_content += "please monitor tools like `nvidia-smi` on the machine "
    md_content += "running the embedding microservice while these tests are active.*\n"

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(md_content)
        print(f"Markdown performance summary saved to: {filepath}")
    except IOError as e:
        print(f"Error saving Markdown performance summary to file: {e}")


def measure_service_performance(
    service_url: str,
    num_texts_in_batch: int = 64,
    num_iterations: int = 10,
    avg_words_per_text: int = 25,
    output_base_dir: str = "performance_benchmark_results" # Directorio base para todos los resultados de este script
):
    embed_endpoint = f"{service_url.rstrip('/')}/api/v1/embed"
    health_endpoint = f"{service_url.rstrip('/')}/health"
    
    model_name_for_file = "unknown_model"
    model_dim_for_file = "unknown_dim"
    raw_model_name_from_health = "N/A" # Default display name
    service_provider = "N/A"
    device_used = "N/A"

    print(f"--- Measuring Performance for Service at: {service_url} ---")
    
    try:
        health_response = requests.get(health_endpoint, timeout=10)
        health_response.raise_for_status()
        health_info = health_response.json()
        raw_model_name_from_health = health_info.get("model_name", "Unknown_Model_From_Health")
        # Sanitize for filename (re-using your existing logic)
        model_name_for_file = "".join(c if c.isalnum() or c in ('-', '_', '.') else '_' for c in raw_model_name_from_health)
        model_dim_for_file = str(health_info.get("model_dimension", "Unknown_Dim"))
        # Attempt to get provider and device info if your /health exposes more details
        # Based on your current /health, it gives model_name and model_dimension
        # If you add 'provider' and 'device' to /health get_model_info(), you can use them.
        # For now, we'll rely on the known configuration.
        # Example of what you could get if health_info was richer:
        # service_provider = health_info.get("model_info", {}).get("provider", "N/A")
        # device_used = health_info.get("model_info", {}).get("device", "N/A")

        print(f"Service Health OK. Model: {raw_model_name_from_health}, Dimension: {model_dim_for_file}")
    except requests.exceptions.RequestException as e:
        print(f"Error connecting to health endpoint {health_endpoint}: {e}. Using fallback names for results.")
        model_name_for_file = f"service_at_{service_url.replace('http://','').replace(':','_').replace('/','_')}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    except json.JSONDecodeError:
        print(f"Error decoding JSON from health endpoint {health_endpoint}. Using fallback names for results.")
        model_name_for_file = f"service_at_{service_url.replace('http://','').replace(':','_').replace('/','_')}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    latencies = []
    throughputs = []
    successful_iterations = 0

    print(f"Will run {num_iterations} iterations with batch size {num_texts_in_batch}.")
    print(f"Generating texts with average {avg_words_per_text} words.")

    for i in range(num_iterations):
        print(f"\nIteration {i + 1}/{num_iterations}...")
        texts_to_embed = generate_random_texts(num_texts_in_batch, avg_words_per_text=avg_words_per_text)
        
        start_time = time.perf_counter()
        try:
            response = requests.post(embed_endpoint, json={"texts": texts_to_embed}, timeout=180) 
            response.raise_for_status()
            response_data = response.json()
            embeddings = response_data.get("embeddings", [])
            
            if not embeddings or len(embeddings) != num_texts_in_batch:
                print(f"  Warning: Received {len(embeddings)} embeddings, expected {num_texts_in_batch}. Skipping this iteration's metrics.")
                continue

            end_time = time.perf_counter()
            latency_seconds = end_time - start_time
            latencies.append(latency_seconds)
            
            throughput_texts_per_sec = len(texts_to_embed) / latency_seconds if latency_seconds > 0 else 0
            throughputs.append(throughput_texts_per_sec)
            successful_iterations += 1

            print(f"  Batch of {len(texts_to_embed)} texts embedded in {latency_seconds:.3f}s")
            print(f"  Throughput: {throughput_texts_per_sec:.2f} texts/s")

        except requests.exceptions.Timeout:
            print(f"  Timeout during embedding request for iteration {i+1}. Skipping this iteration's metrics.")
        except requests.exceptions.RequestException as e:
            print(f"  Request error during embedding for iteration {i+1}: {e}. Skipping this iteration's metrics.")
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  Error decoding JSON or key error in response for iteration {i+1}: {e}")
            response_text = response.text if response else "No response object"
            print(f"  Response text (first 200 chars): {response_text[:200]}. Skipping this iteration's metrics.")
            
        if i < num_iterations - 1:
            time.sleep(1) 

    print("\n--- Performance Summary ---")
    summary_data = {
        "service_url": service_url,
        "model_name_from_health": raw_model_name_from_health,
        "model_dimension": model_dim_for_file,
        "batch_size_tested": num_texts_in_batch,
        "num_total_iterations": num_iterations,
        "num_successful_iterations": successful_iterations,
        "avg_words_per_text": avg_words_per_text,
        "timestamp": datetime.now().isoformat(),
        "device_targetted_in_service_config": "cuda" if "cuda" in service_url or "gpu" in service_url else "cpu (assumed, or check service config)" # Basic assumption
    }

    if successful_iterations > 0:
        avg_lat = float(np.mean(latencies))
        median_lat = float(np.median(latencies))
        std_lat = float(np.std(latencies))
        min_lat = float(np.min(latencies))
        max_lat = float(np.max(latencies))

        avg_thr = float(np.mean(throughputs))
        median_thr = float(np.median(throughputs))
        std_thr = float(np.std(throughputs))
        min_thr = float(np.min(throughputs))
        max_thr = float(np.max(throughputs))

        summary_data.update({
            "avg_latency_seconds": round(avg_lat, 3),
            "stddev_latency_seconds": round(std_lat, 3),
            "min_latency_seconds": round(min_lat, 3),
            "max_latency_seconds": round(max_lat, 3),
            "median_latency_seconds": round(median_lat, 3),
            "avg_throughput_texts_per_sec": round(avg_thr, 2),
            "stddev_throughput_texts_per_sec": round(std_thr, 2),
            "min_throughput_texts_per_sec": round(min_thr, 2),
            "max_throughput_texts_per_sec": round(max_thr, 2),
            "median_throughput_texts_per_sec": round(median_thr, 2),
            "all_latencies_seconds": [round(l, 3) for l in latencies],
            "all_throughputs_texts_per_sec": [round(t, 2) for t in throughputs]
        })
        print(f"Model: {raw_model_name_from_health} (Dim: {model_dim_for_file})")
        print(f"Batch Size: {num_texts_in_batch}")
        print(f"Successful Iterations: {successful_iterations}/{num_iterations}")
        print(f"Average Latency: {summary_data['avg_latency_seconds']:.3f}s (Median: {summary_data['median_latency_seconds']:.3f}s, StdDev: {summary_data['stddev_latency_seconds']:.3f}s)")
        print(f"Average Throughput: {summary_data['avg_throughput_texts_per_sec']:.2f} texts/s (Median: {summary_data['median_throughput_texts_per_sec']:.2f} texts/s, StdDev: {summary_data['stddev_throughput_texts_per_sec']:.2f} texts/s)")
    else:
        print("No successful iterations to report performance metrics.")
        summary_data["error_message"] = "No successful iterations were completed."

    os.makedirs(output_base_dir, exist_ok=True)
    
    sanitized_model_name = model_name_for_file.replace('/', '_').replace('\\', '_')
    base_filename = f"{sanitized_model_name}_perf_b{num_texts_in_batch}_i{num_iterations}_w{avg_words_per_text}"
    
    json_filepath = os.path.join(output_base_dir, f"{base_filename}.json")
    md_filepath = os.path.join(output_base_dir, f"{base_filename}.md")
    
    try:
        with open(json_filepath, 'w') as f:
            json.dump(summary_data, f, indent=4)
        print(f"Performance summary (JSON) saved to: {json_filepath}")
    except IOError as e:
        print(f"Error saving JSON performance summary to file: {e}")
        
    create_markdown_summary(summary_data, md_filepath)

    print("\nNote on VRAM: To measure server-side VRAM usage accurately,")
    print("please monitor tools like 'nvidia-smi' on the machine")
    print("running the embedding microservice while these tests are active.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measure performance of a remote embedding service.")
    parser.add_argument(
        "--url",
        type=str,
        required=True,
        help="Base URL of the embedding service (e.g., http://localhost:8003)."
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=64,
        help="Number of texts per batch for performance testing."
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=10,
        help="Number of iterations for performance testing."
    )
    parser.add_argument(
        "--words",
        type=int,
        default=25, 
        help="Average number of words per randomly generated text."
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="performance_benchmark_results",
        help="Directory to save performance results JSON and Markdown files."
    )
    args = parser.parse_args()

    measure_service_performance(
        service_url=args.url,
        num_texts_in_batch=args.batch_size,
        num_iterations=args.iterations,
        avg_words_per_text=args.words,
        output_base_dir=args.output_dir
    )