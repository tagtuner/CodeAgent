# llama.cpp speed checklist (main 14B / Opus)

CodeAgent only calls HTTP `POST /v1/chat/completions`. Throughput and time-to-first-token are almost entirely decided by the **llama.cpp server** processes (e.g. ports **8080** and **8085** on your host).

Use this as a **plain order** when tuning; exact flags depend on your `llama-server` / `server` build and docs for that version.

## 1. GPU offload

- Map as many layers to GPU as VRAM allows (`-ngl` / `--gpu-layers` or the equivalent in your launcher).
- CPU-only large models feel “stuck” on long prompts.

## 2. Quantization

- Prefer a smaller GGUF quant for production throughput (e.g. `Q4_K_M`, `IQ4_XS`) if quality is acceptable.
- Larger quants = slower decode, more VRAM.

## 3. Context length

- Set **context slot** only as large as you need (large `ctx` = heavier prefill).
- Align with CodeAgent `models.*.ctx_size` in `config.yaml` only for documentation; the server must actually be started with a matching or larger physical context.

## 4. Batch / threads (server-side)

- Tune batch / microbatch per **llama.cpp server** documentation for your GPU (often `-b` / `-ub` or ini keys).
- Wrong batch can hurt latency or throughput; measure after changes.

## 5. Load isolation

- Running **two heavy** models on one GPU simultaneously splits VRAM and bandwidth; peak latency may spike.
- If possible: separate GPUs, staggered usage, or smaller secondary model.

## 6. After server changes

- Restart the llama server(s), then hit OpenAI-compatible `GET`/`POST` health or a tiny `chat/completions` smoke test.
- In CodeAgent UI, retry the same prompt and compare wall time and tokens/sec in the stats bar.

---

Reference: upstream [llama.cpp](https://github.com/ggerganov/llama.cpp) server docs for your tag.
