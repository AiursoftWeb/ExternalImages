"""Strict, read-only loading of the local Keras HDF5 numeric checkpoint."""

import os
from pathlib import Path

os.environ["HDF5_PLUGIN_PRELOAD"] = "::"

import h5py
import numpy as np
import torch
from torch import nn

from .models.mbcnn import MBCNN, Bandpass, ResidualScale

EXPECTED_SHA256 = "605b00d46e6fac107caef8b3fcab04269241db8b6b4983c8e4ede4d43c7b5154"


def _datasets(handle):
    found = {}
    visited = set()

    def walk(group):
        address = h5py.h5o.get_info(group.id).addr
        if address in visited:
            raise ValueError("权重文件包含重复或循环组链接")
        visited.add(address)
        for name in group:
            if not isinstance(group.get(name, getlink=True), h5py.HardLink):
                raise ValueError("权重文件不允许外部或软链接")  # noqa: TRY004
            item = group[name]
            if isinstance(item, h5py.Group):
                walk(item)
            elif isinstance(item, h5py.Dataset):
                if item.is_virtual or item.external:
                    raise ValueError("权重文件不允许虚拟或外部数据存储")
                plist = item.id.get_create_plist()
                if any(
                    plist.get_filter(i)[0] not in (1, 2, 3, 4, 5, 6)
                    for i in range(plist.get_nfilters())
                ):
                    raise ValueError("权重文件包含非标准压缩过滤器")
                found[item.name.lstrip("/")] = item
            else:
                raise ValueError("权重文件包含不支持的对象")  # noqa: TRY004

    walk(handle)
    return found


def load_mbcnn(path: str | Path, device="cpu"):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"未找到 MBCNN 权重：{path}")
    # Ada's cuDNN TF32 shortcut changes this deep residual network noticeably.
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cuda.matmul.allow_tf32 = False
    model = MBCNN()
    consumed = set()
    try:
        with h5py.File(path, "r") as handle, torch.no_grad():
            datasets = _datasets(handle)

            def copy(layer_name, weight_name, parameter, transform=None):
                name = f"{layer_name}/{layer_name}/{weight_name}:0"
                if name not in datasets:
                    raise ValueError(f"权重缺失：{name}")
                dataset = datasets[name]
                expected = tuple(parameter.shape)
                if transform == "conv":
                    expected = (expected[2], expected[3], expected[1], expected[0])
                elif transform == "dense":
                    expected = (expected[1], expected[0])
                elif transform == "frequency":
                    expected = (1, 1, 64, 1)
                if dataset.shape != expected or dataset.dtype != np.dtype("float32"):
                    raise ValueError(
                        f"权重形状或类型不匹配：{name}，需要{expected}/float32，实际{dataset.shape}/{dataset.dtype}"
                    )
                array = dataset[()]
                if not np.isfinite(array).all():
                    raise ValueError(f"权重含有 NaN/Inf：{name}")
                if transform == "conv":
                    array = array.transpose(3, 2, 0, 1)
                elif transform == "dense":
                    array = array.T
                elif transform == "frequency":
                    array = array.reshape(64)
                parameter.copy_(torch.from_numpy(np.ascontiguousarray(array)))
                consumed.add(name)

            for name, layer in model.layers.items():
                if isinstance(layer, nn.Conv2d):
                    copy(name, "kernel", layer.weight, "conv")
                    copy(name, "bias", layer.bias)
                elif isinstance(layer, nn.Linear):
                    copy(name, "kernel", layer.weight, "dense")
                    copy(name, "bias", layer.bias)
                elif isinstance(layer, Bandpass):
                    copy(name, "ait_conv", layer.frequency, "frequency")
                elif isinstance(layer, ResidualScale):
                    copy(name, "scale", layer.scale)
            unused = set(datasets) - consumed
            if unused:
                raise ValueError(f"权重包含未映射的数据集：{sorted(unused)}")
            if len(consumed) != 192:
                raise ValueError(f"权重映射数量异常：{len(consumed)}")
    except OSError as exc:
        raise ValueError(f"无法读取 HDF5 权重：{path}：{exc}") from exc
    model.loaded_dataset_count = len(consumed)
    return model.to(device).eval()
