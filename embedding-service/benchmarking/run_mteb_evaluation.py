# embedding-service/benchmarking/run_mteb_evaluation.py
import argparse
import os
import json 
import torch
from mteb import MTEB, get_task 
from benchmark_remote import RemoteEncoder # Asegúrate que este archivo está en la misma carpeta o en PYTHONPATH
from datetime import datetime

DEFAULT_SERVICE_URL = "http://localhost:8003"

def run_evaluation(
    model_url: str, 
    model_custom_name: str | None = None, 
    tasks_to_run_names: list[str] | None = None, 
    batch_size: int = 32, # Este batch_size se pasará al RemoteEncoder via encode_kwargs
    output_base_dir: str = "mteb_eval_results"
):
    print(f"--- Starting MTEB Evaluation for Model at: {model_url} ---")

    # RemoteEncoder es inicializado con el batch_size, y MTEB usará esto si no se pasan encode_kwargs,
    # pero para seguir las mejores prácticas de MTEB, pasaremos encode_kwargs a evaluation.run()
    # El batch_size en RemoteEncoder se usa internamente para sus bucles.
    model_evaluator = RemoteEncoder(base_url=model_url, batch_size=batch_size) 
    
    if model_custom_name:
        display_name = model_custom_name
        sanitized_folder_name = "".join(c if c.isalnum() or c in ('-', '_', '.') else '_' for c in display_name)
    elif hasattr(model_evaluator, 'name') and model_evaluator.name: 
        display_name = model_evaluator.name
        sanitized_folder_name = model_evaluator.name
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        clean_url_part = model_url.replace('http://','').replace('https://','').replace(':','_').replace('/','_')
        display_name = f"unknown_model_at_{clean_url_part}_{timestamp}"
        sanitized_folder_name = display_name
        print(f"Warning: Model name could not be determined from /health. Using generated name: {display_name}")
        
    if model_evaluator.dimension is None:
        print("Critical Error: Model dimension could not be determined from the health endpoint. MTEB evaluation cannot proceed.")
        return

    print(f"Effective model name for display: {display_name}")
    print(f"Sanitized model name for output folder: {sanitized_folder_name}")
    print(f"Model dimension from service: {model_evaluator.dimension}")

    if tasks_to_run_names is None:
        tasks_to_run_names = [
            "FiQA2018",                         
            "FinancialPhrasebankClassification", 
        ]
        print(f"No specific tasks provided, using default list: {tasks_to_run_names}")
    
    print(f"MTEB task names to be evaluated: {tasks_to_run_names}")

    loaded_tasks = []
    for task_name_str_input in tasks_to_run_names:
        try:
            if isinstance(task_name_str_input, str) and task_name_str_input.strip():
                print(f"Attempting to load MTEB task: {task_name_str_input}...")
                task_object = get_task(task_name_str_input, languages=["en"]) 
                loaded_tasks.append(task_object)
                actual_task_name_loaded = task_name_str_input # Default
                if hasattr(task_object, 'metadata') and task_object.metadata and hasattr(task_object.metadata, 'name') and task_object.metadata.name:
                    actual_task_name_loaded = task_object.metadata.name
                elif hasattr(task_object, 'name') and task_object.name:
                    actual_task_name_loaded = task_object.name
                print(f"Successfully loaded MTEB task: {actual_task_name_loaded} (Type: {type(task_object).__name__})")
            else:
                print(f"Warning: Invalid task name: '{task_name_str_input}'. Skipping.")
        except ValueError as e: 
            print(f"Warning: Could not load MTEB task '{task_name_str_input}'. Error: {e}. Skipping.")
        except Exception as e:
            print(f"Unexpected error loading MTEB task '{task_name_str_input}': {e}. Skipping.")
            import traceback
            traceback.print_exc()
            
    if not loaded_tasks:
        print("No valid MTEB tasks were loaded. Aborting evaluation.")
        return

    evaluation = MTEB(tasks=loaded_tasks) 
    output_folder_model_specific = os.path.join(output_base_dir, sanitized_folder_name)
    print(f"MTEB evaluation results will be saved under: {output_folder_model_specific}")
    os.makedirs(output_folder_model_specific, exist_ok=True)

    try:
        if torch.cuda.is_available(): 
            print(f"Client-side: Initial CUDA memory allocated: {torch.cuda.memory_allocated(0) / 1024**2:.2f} MB")
            print(f"Client-side: Initial CUDA memory reserved:  {torch.cuda.memory_reserved(0) / 1024**2:.2f} MB")
    except Exception as e:
        print(f"Client-side: Could not get CUDA memory stats: {e}")

    evaluation_start_time = datetime.now()
    task_names_for_log_display = []
    for t in loaded_tasks:
        name_to_log = "UnknownTask"
        if hasattr(t, 'metadata') and t.metadata and hasattr(t.metadata, 'name') and t.metadata.name:
            name_to_log = t.metadata.name
        elif hasattr(t, 'name') and t.name:
            name_to_log = t.name
        else:
            name_to_log = f"UnnamedTask(Type:{type(t).__name__})"
        task_names_for_log_display.append(name_to_log)
    print(f"Starting MTEB .run() at {evaluation_start_time.isoformat()} for tasks: {task_names_for_log_display}")

    try:
        # LLM_FLAG_MTEB_RUN_KWARGS_UPDATE_START
        # Usar encode_kwargs para pasar batch_size según las recomendaciones de MTEB
        evaluation.run(
            model_evaluator,
            output_folder=output_folder_model_specific, 
            eval_splits=["test"], 
            encode_kwargs={'batch_size': batch_size} # Argumento preferido por MTEB
            # El argumento `batch_size` directo en `run()` está obsoleto.
        )
        # LLM_FLAG_MTEB_RUN_KWARGS_UPDATE_END
    except Exception as e:
        print(f"CRITICAL ERROR during MTEB evaluation.run(): {e}")
        import traceback
        traceback.print_exc() 
    
    evaluation_end_time = datetime.now()
    print(f"MTEB .run() finished at {evaluation_end_time.isoformat()}")
    print(f"Total MTEB .run() duration: {evaluation_end_time - evaluation_start_time}")

    print(f"--- MTEB Evaluation Finished for Model: {display_name} ---")
    print(f"Results saved in: {output_folder_model_specific}")
    
    try:
        if torch.cuda.is_available():
            print(f"Client-side: Final CUDA memory allocated: {torch.cuda.memory_allocated(0) / 1024**2:.2f} MB")
            print(f"Client-side: Final CUDA memory reserved:  {torch.cuda.memory_reserved(0) / 1024**2:.2f} MB")
    except Exception as e:
        print(f"Client-side: Could not get final CUDA memory stats: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run MTEB Evaluation for a remote embedding service.")
    parser.add_argument(
        "--name",
        type=str,
        default=None, 
        help="Custom display name for the model, used for naming the output subfolder. If not provided, name is fetched from service /health."
    )
    parser.add_argument(
        "--tasks",
        nargs="+",
        default=None,
        help=(
            "List of MTEB task names (e.g., FiQA2018 FinancialPhrasebankClassification). "
            "If not provided, a default list is used. Use MTEB official task names."
        )
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=32, 
        help="Batch size for encoding sentences by the remote service (passed via encode_kwargs)."
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="mteb_benchmark_results", 
        help="Base directory to save MTEB evaluation results."
    )

    args = parser.parse_args()

    run_evaluation(
        model_url=DEFAULT_SERVICE_URL, 
        model_custom_name=args.name,
        tasks_to_run_names=args.tasks,
        batch_size=args.batch_size,
        output_base_dir=args.output_dir
    )