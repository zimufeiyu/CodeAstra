# PPU 运维产物

本目录不包含推测或手工填写的设备性能结论。

在目标 ZW810E 服务器完成 live 测试和完整压测后，运行：

```bash
python scripts/render_inference_contract.py \
  --benchmark output/ppu-benchmark.json \
  --output docs/operations/ppu-inference-contract.md
```

生成的 `ppu-inference-contract.md` 才是后续上下文预算、并发上限和超时配置的依据。
当前服务器已在两张 PPU 上完成 Qwen3-8B 双实例实测；以下参数是已验证配置。

## 启动 SGLang 实例

每个实例独占一张卡，分别使用 device/port `0/30000` 和 `1/30001`：

```bash
export PATH=/usr/local/bin:/usr/bin:/bin
export CUDA_PATH=/usr/local/cuda
export PPU_SDK=/usr/local/PPU_SDK
export CUDA_VISIBLE_DEVICES=<device>

/usr/local/bin/python3 -m sglang.launch_server \
  --model-path /LLM/qwen3 \
  --served-model-name Qwen3-8B \
  --host 127.0.0.1 \
  --port <port> \
  --tensor-parallel-size 1 \
  --dtype bfloat16 \
  --context-length 40960 \
  --reasoning-parser qwen3
```

代码中的同一套默认参数位于 `default_ppu_launch_config()`。设备隔离使用
`CUDA_VISIBLE_DEVICES`；本机 SGLang 的 PPU 兼容层沿用该变量名。

## 验收

```bash
curl -f http://127.0.0.1:30000/health
curl -f http://127.0.0.1:30001/health
/usr/local/bin/python3 scripts/probe_model_gateway.py
RUN_LIVE_PPU_TESTS=1 /usr/local/bin/python3 -m pytest tests/live/test_qwen3_ppu.py -v
```
