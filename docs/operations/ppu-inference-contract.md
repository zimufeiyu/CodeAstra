# Verified PPU Inference Contract

Benchmark date: 2026-08-04

| Property | Verified value |
|---|---|
| Model | Qwen3-8B |
| Environment | ASLLM 1.10.6-poc, PyTorch 2.9.0, Ubuntu 24.04, SAIL 2.1.0, CUDA 12.9, SGLang 0.5.10, Python 3.12, PPU SDK 2.1.0 |
| SGLang request path | `/v1/chat/completions` |
| Response content path | `choices[0].message.content` |
| Instance mapping | `instance-0 -> ZW810E 0`; `instance-1 -> ZW810E 1` |
| Verified prompt ceiling | 28,138 tokens |
| Recommended prompt ceiling | 22,510 tokens |
| Maximum stable concurrency | 8 |
| Gateway request timeout | 600 seconds |
| JSON Schema success rate | 100.00% |

## Measured cases

| Instance | Prompt chars | Concurrency | p50 | p95 | First delta p95 | Tok/s | Success |
|---|---:|---:|---:|---:|---:|---:|---:|
| instance-0 | 24000 | 1 | 788 ms | 794 ms | 14908 ms | 5984.83 | 100.00% |
| instance-0 | 24000 | 2 | 205 ms | 209 ms | 1934 ms | 45152.53 | 100.00% |
| instance-0 | 24000 | 4 | 242 ms | 246 ms | 1060 ms | 76356.67 | 100.00% |
| instance-0 | 24000 | 8 | 317 ms | 334 ms | 721 ms | 104370.83 | 100.00% |
| instance-0 | 72000 | 1 | 1974 ms | 1980 ms | 37457 ms | 7127.44 | 100.00% |
| instance-0 | 72000 | 2 | 242 ms | 246 ms | 2257 ms | 114049.83 | 100.00% |
| instance-0 | 72000 | 4 | 330 ms | 370 ms | 1456 ms | 168022.96 | 100.00% |
| instance-0 | 72000 | 8 | 478 ms | 941 ms | 1119 ms | 197195.68 | 100.00% |
| instance-0 | 144000 | 1 | 4355 ms | 6681 ms | 91404 ms | 5731.33 | 100.00% |
| instance-0 | 144000 | 2 | 13204 ms | 13209 ms | 125534 ms | 4263.21 | 100.00% |
| instance-0 | 144000 | 4 | 26202 ms | 26218 ms | 124442 ms | 4296.32 | 100.00% |
| instance-0 | 144000 | 8 | 36967 ms | 39322 ms | 95876 ms | 5492.48 | 100.00% |
| instance-1 | 24000 | 1 | 788 ms | 790 ms | 14908 ms | 5986.85 | 100.00% |
| instance-1 | 24000 | 2 | 206 ms | 212 ms | 1940 ms | 45076.23 | 100.00% |
| instance-1 | 24000 | 4 | 244 ms | 248 ms | 1068 ms | 76251.28 | 100.00% |
| instance-1 | 24000 | 8 | 318 ms | 328 ms | 720 ms | 104773.74 | 100.00% |
| instance-1 | 72000 | 1 | 1975 ms | 1982 ms | 37474 ms | 7124.80 | 100.00% |
| instance-1 | 72000 | 2 | 242 ms | 249 ms | 2266 ms | 113861.85 | 100.00% |
| instance-1 | 72000 | 4 | 311 ms | 365 ms | 1456 ms | 168249.49 | 100.00% |
| instance-1 | 72000 | 8 | 467 ms | 941 ms | 1139 ms | 196864.50 | 100.00% |
| instance-1 | 144000 | 1 | 4363 ms | 6692 ms | 91495 ms | 5722.12 | 100.00% |
| instance-1 | 144000 | 2 | 13208 ms | 13229 ms | 125604 ms | 4261.14 | 100.00% |
| instance-1 | 144000 | 4 | 26186 ms | 26213 ms | 111549 ms | 4765.21 | 100.00% |
| instance-1 | 144000 | 8 | 39320 ms | 45771 ms | 104669 ms | 5058.96 | 100.00% |

The deployed capacity profile uses request concurrency 8 per instance plus
prompt-token admission control. Small chunks can use the measured concurrency;
large chunks are serialized when concurrent prefill would reduce throughput.

This contract contains aggregate measurements only. It contains no prompt,
response, source code, credential, or internal endpoint URL.
