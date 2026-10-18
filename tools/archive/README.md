# 历史实验工具

以下工具仅保留研究记录，不在发布镜像中，也不是当前后端的依赖：

- `agy_dump_proxy.py`：早期 Cloud Code 中转、请求记录与替换实验。它会接收鉴权请求头并可能保存完整业务正文；只适合受控测试，不用于真实业务流量。
- `agyhook/`：特定 Windows 二进制的 DLL 注入实验，地址与 ABI 绑定当时样本，未作为可部署方案验证。

当前原生后端与 Linux 观测工具位于 [agy_native_wire_probe](../agy_native_wire_probe/README.md)。已脱敏的历史报告见 [文档归档](../../docs/archive/README.md)；原始捕获、凭据、二进制均留在被 Git 忽略的本地目录。
