# Autonomous research executor

This source snapshot records the independent Ubuntu 24.04 executor used with the Windows workbench. Keep its Python environment, dependencies and isolated Docker runtime in the executor project. The identically named Windows modules under `src/` implement the local bridge and protocol contract.

`source-manifest.json` maps the executor source, build recipe and locks to their SHA-256 digests. It also records the identity of the previously installed host wheel and guest image. The package source is suitable for rebuilding a wheel. Build outputs can have different binary identities and need fresh deployment bindings.

Use the root `pyproject.toml` and `uv.lock` for the host package. The separate `guest/` project and locks describe the guest. The `Dockerfile` expects the reviewed Python base image through `EXECUTOR_BASE` and a separately prepared wheelhouse matching `guest/requirements.guest.lock`. Installation also requires the local isolation configuration and deployment identity. A checkout supplies the build inputs while local credentials, runtime state, images and the wheelhouse remain on the host. Verify the resulting deployment through the app's reported capabilities before starting research.

Windows 主应用与 Ubuntu 自主研究执行器分别使用各自的环境、依赖和运行目录。这里保留源码、锁文件、构建配方及已安装版本的摘要。部署时需要在本机准备隔离的构建依赖、基础镜像与运行配置，应用会验证实际安装的身份。
