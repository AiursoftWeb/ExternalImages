"""Inference implementation of the multiscale bandpass CNN (CVPR 2020).

Layer labels match the supplied Keras checkpoint, not PyTorch shuffle ordering.
"""

import math

import torch
from torch import nn
from torch.nn import functional as F


def space_to_depth(x, block=2):
    n, c, h, w = x.shape
    return (
        x.reshape(n, c, h // block, block, w // block, block)
        .permute(0, 3, 5, 1, 2, 4)
        .reshape(n, block * block * c, h // block, w // block)
    )


def depth_to_space(x, block=2):
    n, channels, h, w = x.shape
    c = channels // (block * block)
    return (
        x.reshape(n, block, block, c, h, w)
        .permute(0, 3, 4, 1, 5, 2)
        .reshape(n, c, h * block, w * block)
    )


def idct_kernel():
    kernel = torch.empty(64, 64, 1, 1)
    for y in range(8):
        for x in range(8):
            for u in range(8):
                for v in range(8):
                    value = math.cos((2 * y + 1) * u * math.pi / 16)
                    value *= math.cos((2 * x + 1) * v * math.pi / 16)
                    value *= math.sqrt(1 / 8) if u == 0 else math.sqrt(2 / 8)
                    value *= math.sqrt(1 / 8) if v == 0 else math.sqrt(2 / 8)
                    kernel[y * 8 + x, u * 8 + v, 0, 0] = value
    return kernel


def same_stride2(x, convolution):
    # TF SAME puts the extra padding at the bottom/right, including after pre-pad.
    height, width = x.shape[-2:]
    ph = max(0, ((height + 1) // 2 - 1) * 2 + 3 - height)
    pw = max(0, ((width + 1) // 2 - 1) * 2 + 3 - width)
    return convolution(F.pad(x, (pw // 2, pw - pw // 2, ph // 2, ph - ph // 2)))


class Bandpass(nn.Module):
    def __init__(self):
        super().__init__()
        self.frequency = nn.Parameter(torch.ones(64))

    def forward(self, x, kernel):
        return F.conv2d(x, kernel * self.frequency[None, :, None, None])


class ResidualScale(nn.Module):
    def __init__(self):
        super().__init__()
        self.scale = nn.Parameter(torch.full((1,), 0.1))

    def forward(self, x):
        return x * self.scale


class MBCNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.layers = nn.ModuleDict()
        self.register_buffer("idct", idct_kernel(), persistent=False)
        conv_index = dense_index = band_index = 0

        def conv(cin, cout, kernel=3, dilation=1, stride=1, padding=None):
            nonlocal conv_index
            conv_index += 1
            if padding is None:
                padding = dilation if kernel == 3 else 0
            self.layers[f"conv2d_{conv_index}"] = nn.Conv2d(
                cin, cout, kernel, stride=stride, padding=padding, dilation=dilation
            )

        def pre(rates):
            nonlocal band_index
            for index, dilation in enumerate(rates):
                conv(128 + 64 * index, 64, dilation=dilation)
            conv(448, 64)
            band_index += 1
            self.layers[f"adaptive_implicit_trans2_{band_index}"] = Bandpass()
            conv(64, 128, 1)
            self.layers[f"scale_layer_{band_index}"] = ResidualScale()

        def global_tone():
            nonlocal dense_index
            conv(128, 256, stride=2, padding=0)
            for cin, cout in ((256, 1024), (1024, 512), (512, 256)):
                dense_index += 1
                self.layers[f"dense_{dense_index}"] = nn.Linear(cin, cout)
            conv(128, 256, 1)
            conv(256, 128, 1)

        def post(rates):
            for index, dilation in enumerate(rates):
                conv(128 + 64 * index, 64, dilation=dilation)
            conv(448, 128, 1)

        fine = (1, 2, 3, 2, 1)
        coarse = (1, 2, 2, 2, 1)
        conv(12, 128)
        pre(fine)
        conv(128, 128, stride=2)
        pre(fine)
        conv(128, 128, stride=2)
        pre(coarse)
        global_tone()
        post(coarse)
        conv(128, 12)
        conv(131, 128, 1)
        global_tone()
        pre(fine)
        global_tone()
        post(fine)
        conv(128, 12)
        conv(131, 128, 1)
        global_tone()
        pre(fine)
        global_tone()
        post(fine)
        conv(128, 12)
        assert (conv_index, dense_index, band_index) == (76, 15, 5)

    def _conv(self, x, index, relu=True):
        y = self.layers[f"conv2d_{index}"](x)
        return F.relu(y, inplace=not torch.is_grad_enabled()) if relu else y

    def _dense_stack(self, x, start, rates):
        for offset in range(len(rates)):
            new = self._conv(x, start + offset)
            x = torch.cat((new, x), dim=1)
        return x

    def _project_dense(self, x, start, rates, final_relu):
        # In inference, linearity avoids materializing the growing 448-channel cat.
        # Training retains the ordinary graph and its original summation order.
        features = [x]
        for index in range(len(rates) + 1):
            layer = self.layers[f"conv2d_{start + index}"]
            result = None
            offset = 0
            for feature in features:
                width = feature.shape[1]
                partial = F.conv2d(
                    feature,
                    layer.weight[:, offset : offset + width],
                    None,
                    layer.stride,
                    layer.padding,
                    layer.dilation,
                    layer.groups,
                )
                if result is None:
                    result = partial
                else:
                    result.add_(partial)
                offset += width
            result.add_(layer.bias[None, :, None, None])
            if index < len(rates) or final_relu:
                result.relu_()
            if index == len(rates):
                return result
            features.insert(0, result)

    def _pre(self, x, start, band, rates):
        if torch.is_grad_enabled():
            y = self._dense_stack(x, start, rates)
            y = self._conv(y, start + 5, relu=False)
        else:
            y = self._project_dense(x, start, rates, False)
        y = self.layers[f"adaptive_implicit_trans2_{band}"](y, self.idct)
        y = self._conv(y, start + 6, relu=False)
        if torch.is_grad_enabled():
            return x + self.layers[f"scale_layer_{band}"](y)
        y.mul_(self.layers[f"scale_layer_{band}"].scale).add_(x)
        return y

    def _global(self, x, conv_start, dense_start):
        y = F.relu(
            same_stride2(F.pad(x, (1, 1, 1, 1)), self.layers[f"conv2d_{conv_start}"])
        )
        y = y.mean(dim=(2, 3))
        for index in range(3):
            y = self.layers[f"dense_{dense_start + index}"](y)
            if index < 2:
                y = F.relu(y)
        local = self._conv(x, conv_start + 1)
        return self._conv(local * y[:, :, None, None], conv_start + 2)

    def _post(self, x, start, rates):
        if torch.is_grad_enabled():
            return self._conv(self._dense_stack(x, start, rates), start + 5)
        return self._project_dense(x, start, rates, True)

    def forward(self, x):
        if x.ndim != 4 or x.shape[1] != 3 or any(s % 8 for s in x.shape[-2:]):
            raise ValueError("MBCNN expects BGR NCHW with height/width divisible by 8")
        fine = (1, 2, 3, 2, 1)
        coarse = (1, 2, 2, 2, 1)
        t1 = self._pre(self._conv(space_to_depth(x), 1), 2, 1, fine)
        t2 = self._pre(self._conv(t1, 9), 10, 2, fine)
        t3 = self._pre(self._conv(t2, 17), 18, 3, coarse)
        t3 = self._post(self._global(t3, 25, 1), 28, coarse)
        quarter = depth_to_space(self._conv(t3, 34, relu=False))
        del t3
        t2 = self._conv(torch.cat((quarter, t2), dim=1), 35)
        t2 = self._pre(self._global(t2, 36, 4), 39, 4, fine)
        t2 = self._post(self._global(t2, 46, 7), 49, fine)
        half = depth_to_space(self._conv(t2, 55, relu=False))
        del t2
        t1 = self._conv(torch.cat((t1, half), dim=1), 56)
        t1 = self._pre(self._global(t1, 57, 10), 60, 5, fine)
        t1 = self._post(self._global(t1, 67, 13), 70, fine)
        full = depth_to_space(self._conv(t1, 76, relu=False))
        return quarter, half, full
