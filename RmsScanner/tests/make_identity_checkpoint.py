"""Build an untrained HDF5 checkpoint for the offline CPU image smoke test."""
import sys

import h5py
from torch import nn

from more_clean.models.mbcnn import Bandpass, MBCNN, ResidualScale


def write_checkpoint(path):
    # Route the twelve space-to-depth input channels through the fine-scale
    # branch. The dense projection prepends five lots of 64 feature channels.
    passthrough = {1: 0, 56: 0, 58: 0, 59: 0, 68: 0, 69: 0, 75: 320, 76: 0}
    with h5py.File(path, "w") as checkpoint:
        for name, layer in MBCNN().layers.items():
            group = checkpoint.create_group(f"{name}/{name}")
            if isinstance(layer, nn.Conv2d):
                height, width = layer.kernel_size
                kernel = group.create_dataset(
                    "kernel:0", (height, width, layer.in_channels, layer.out_channels),
                    dtype="float32", compression="gzip",
                )
                index = int(name.removeprefix("conv2d_"))
                if index in passthrough:
                    for channel in range(12):
                        kernel[height // 2, width // 2, passthrough[index] + channel, channel] = 1
                group.create_dataset("bias:0", (layer.out_channels,), dtype="float32")
            elif isinstance(layer, nn.Linear):
                group.create_dataset("kernel:0", (layer.in_features, layer.out_features), dtype="float32")
                # The two fine-scale global tone gates multiply the features.
                gain = 1 if name in {"dense_12", "dense_15"} else 0
                group.create_dataset("bias:0", (layer.out_features,), dtype="float32", fillvalue=gain)
            elif isinstance(layer, Bandpass):
                group.create_dataset("ait_conv:0", (1, 1, 64, 1), dtype="float32")
            elif isinstance(layer, ResidualScale):
                group.create_dataset("scale:0", tuple(layer.scale.shape), dtype="float32")
            else:
                raise ValueError(f"Unexpected MBCNN layer {name}")


if __name__ == "__main__":
    write_checkpoint(sys.argv[1])
