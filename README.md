# CairnMesh

433 MHz LoRa 多跳网络的协议栈与设备固件。

- `cairnmesh/`：Python 参照实现——控制端、设备端点、安全层、受控泛洪、按需会话、节点发现、唤醒，以及可复现的模拟器。
- `firmware/`：同一线上格式与行为向量的 TinyGo 实现。目标：STM32WLE5 节点；STM32U083 + 双 SX1262 网关/台架。
- `tests/`：单元、对拍（conformance）、性质、场景与硬件台架测试；`tests/vectors/` 是 Python 与固件共用的向量。

## 开发

需要 Python 3.10 及以上：

```bash
python -m pip install -e ".[secure,serial,dev]"
python -m pytest
python -m cairnmesh.demo
```

## 许可

AGPL-3.0，见 `LICENSE`。
