# embedding-service/benchmark_remote.py
import requests
import json
import numpy as np
import time
from typing import List, Dict, Any

class RemoteEncoder:
    """
    A wrapper for MTEB to use a remote embedding service.
    """
    def __init__(self, base_url: str, batch_size: int = 32, request_timeout: int = 90):
        self.base_url = base_url.rstrip("/")
        self.batch_size = batch_size
        self.request_timeout = request_timeout
        self._name: str | None = None
        self._dim: int | None = None
        
        # Attempt to fetch model info from /health endpoint
        try:
            response = requests.get(f"{self.base_url}/health", timeout=10)
            response.raise_for_status() # Raise an exception for HTTP errors
            info = response.json()
            self._name = info.get("model_name")
            self._dim = info.get("model_dimension")
            if not self._name or not self._dim:
                print(f"Warning: Could not retrieve full model info from /health endpoint at {self.base_url}")
                print(f"Response: {info}")
                # Fallback or allow user to specify if needed
        except requests.exceptions.RequestException as e:
            print(f"Error connecting to health endpoint at {self.base_url}: {e}")
            print("Please ensure the embedding service is running and accessible.")
            # Optionally, re-raise or exit if health check is critical for initialization
            # For MTEB, name and dimension are important.

        if self._name is None: # Fallback name if health check fails or provides incomplete info
            self._name = f"remote_model_at_{base_url.replace('http://', '').replace('https://', '').replace(':', '_')}"
        if self._dim is None: # Fallback dimension
             print("Warning: Model dimension not found from health check. Defaulting to a common value like 768 or 1024.")
             print("MTEB might fail if this is incorrect. Consider ensuring /health provides 'model_dimension'.")
             # self._dim = 768 # Or make it a parameter

    def encode(self, sentences: List[str], **kwargs: Any) -> np.ndarray:
        """
        Encodes a list of sentences using the remote embedding service.

        Args:
            sentences: A list of strings to encode.
            **kwargs: Additional keyword arguments (ignored by this remote encoder).

        Returns:
            A numpy array of embeddings.
        """
        if not self._dim:
            raise ValueError("Model dimension is not set. Cannot proceed with encoding. Check health endpoint.")
        
        all_embeddings: List[List[float]] = []
        
        for i in range(0, len(sentences), self.batch_size):
            chunk = sentences[i:i + self.batch_size]
            payload = {"texts": chunk}
            
            try:
                response = requests.post(
                    f"{self.base_url}/api/v1/embed",
                    json=payload,
                    timeout=self.request_timeout
                )
                response.raise_for_status()
                response_data = response.json()
                
                if "embeddings" not in response_data or not response_data["embeddings"]:
                    print(f"Warning: Empty or missing embeddings in response for chunk starting at index {i}.")
                    # Create zero vectors of the expected dimension for this chunk to avoid shape errors later
                    # This is a simple way to handle it; more sophisticated error handling might be needed
                    # depending on how strictly MTEB handles missing embeddings.
                    chunk_embeddings = [np.zeros(self._dim).tolist()] * len(chunk)
                else:
                    chunk_embeddings = response_data["embeddings"]

                # Ensure all embeddings in the chunk have the correct dimension
                for emb_idx, emb in enumerate(chunk_embeddings):
                    if len(emb) != self._dim:
                        print(f"Error: Embedding dimension mismatch for text '{chunk[emb_idx][:30]}...'. Expected {self._dim}, got {len(emb)}. Replacing with zeros.")
                        chunk_embeddings[emb_idx] = np.zeros(self._dim).tolist()
                
                all_embeddings.extend(chunk_embeddings)

            except requests.exceptions.Timeout:
                print(f"Timeout error processing chunk starting at index {i}. URL: {self.base_url}/api/v1/embed")
                # Handle timeout by adding zero vectors for the timed-out chunk
                all_embeddings.extend([np.zeros(self._dim).tolist()] * len(chunk))
            except requests.exceptions.RequestException as e:
                print(f"Request error processing chunk starting at index {i}: {e}. URL: {self.base_url}/api/v1/embed")
                # Handle other request errors similarly
                all_embeddings.extend([np.zeros(self._dim).tolist()] * len(chunk))
            except (json.JSONDecodeError, KeyError) as e:
                print(f"Error decoding JSON or key error in response for chunk starting at index {i}: {e}")
                print(f"Response content: {response.text[:500] if response else 'No response'}") # Log part of the response
                all_embeddings.extend([np.zeros(self._dim).tolist()] * len(chunk))

        if not all_embeddings or len(all_embeddings) != len(sentences):
            print(f"Warning: Number of embeddings ({len(all_embeddings)}) does not match number of input sentences ({len(sentences)}). This might cause issues.")
            # Pad with zero vectors if there's a mismatch, to ensure MTEB gets an array of the correct total length.
            if len(all_embeddings) < len(sentences) and self._dim:
                 missing_count = len(sentences) - len(all_embeddings)
                 print(f"Padding with {missing_count} zero vectors of dimension {self._dim}.")
                 all_embeddings.extend([np.zeros(self._dim).tolist()] * missing_count)


        return np.asarray(all_embeddings, dtype="float32")

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def dimension(self) -> int | None:
        return self._dim

    # MTEB also checks for a method like this to determine if the model is for sentence similarity
    # For a generic remote encoder, this detail might not be known, but MTEB can infer
    # from task types. You can add it if you want to be more explicit for SentenceTransformer based models.
    # def similarity_pairwise(self, N=None, show_progress_bar=None, convert_to_numpy=None, precision=None, batch_size=None):
    #     # This method is typically for SentenceTransformer models when used directly.
    #     # For a remote API, it's usually not implemented this way.
    #     raise NotImplementedError("Pairwise similarity not directly implemented for RemoteEncoder")