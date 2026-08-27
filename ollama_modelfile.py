"""Ollama 翻译模型 → GGUF 仓库映射（供 install_engine 使用）。"""

GGUF_SIZES = {
    "qwen2.5:3b": ("Qwen/Qwen2.5-3B-Instruct-GGUF",
                   "qwen2.5-3b-instruct-q4_k_m.gguf", "约 2.0GB"),
    "qwen2.5:7b": ("Qwen/Qwen2.5-7B-Instruct-GGUF",
                   "qwen2.5-7b-instruct-q3_k_m.gguf", "约 3.8GB"),
    "aya-expanse:8b": ("bartowski/aya-expanse-8b-GGUF",
                       "aya-expanse-8b-Q4_K_M.gguf", "约 5.1GB"),
}