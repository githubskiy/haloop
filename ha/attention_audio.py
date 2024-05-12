import math
import torch
import torch.nn as nn
import torch.nn.functional as F

from attention import LayerNorm, Block
from conv import DWConv1d


def sinusoids(length, channels, max_timescale=10000):
    """Returns sinusoids for positional embedding"""
    assert channels % 2 == 0
    scales = torch.arange(channels // 2) / (channels // 2 - 1)
    inv_timescales = torch.exp(-math.log(max_timescale) * scales)
    scaled_time = torch.arange(length)[:, None] * inv_timescales[None, :]
    return torch.cat([torch.sin(scaled_time), torch.cos(scaled_time)], dim=1)


class StridingAudioEncoder(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.config = config

        conv = [nn.Conv1d(config.d_input, config.d_conv, kernel_size=3, stride=config.conv_strides[0], padding=1)]
        for stride in config.conv_strides[1:-1]:
            conv.append(DWConv1d(config.d_conv, config.d_conv, kernel_size=3, stride=stride, padding=1))
        conv.append(DWConv1d(config.d_conv, config.n_embd, kernel_size=3, stride=config.conv_strides[-1], padding=1))
        self.conv = nn.ModuleList(conv)

        assert config.rotary_emb_dim
        self.transformer = nn.ModuleDict(dict(
            drop = nn.Dropout(config.dropout),
            h = nn.ModuleList([Block(config) for _ in range(config.n_layer)]),
            ln_f = LayerNorm(config.n_embd, bias=config.bias),
        ))

    def subsampled_lengths(self, input_lengths):
        # https://github.com/vdumoulin/conv_arithmetic
        o = input_lengths
        for conv in self.conv:
            p, k, s = conv.padding[0], conv.kernel_size[0], conv.stride[0]
            o = o + 2 * p - k
            o = torch.floor(o / s + 1)
        return o.int()

    def forward(self, x, input_lengths, measure_entropy=False):
        x = x.mT
        for conv in self.conv:
            x = F.gelu(conv(x))
        x = x.mT

        _, t, c = x.size()
        pos = torch.arange(0, t, dtype=torch.long, device=x.device).unsqueeze(0) # shape (1, t)
        x = self.transformer.drop(x) # shape (b, t, c)

        for i, block in enumerate(self.transformer.h):
            x, _att_entropy, _present = block(x, past=None, measure_entropy=measure_entropy)
        x = self.transformer.ln_f(x)

        return x, self.subsampled_lengths(input_lengths), {}


class AudioEncoder(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.config = config

        # whisper style convolutions
        self.conv_pre = nn.Conv1d(config.d_input, config.n_embd, kernel_size=3, stride=1, padding=1)
        self.conv_subsample = nn.Conv1d(config.n_embd, config.n_embd, kernel_size=3, stride=2, padding=1)

        if config.rotary_emb_dim:
            self.transformer = nn.ModuleDict(dict(
                drop = nn.Dropout(config.dropout),
                h = nn.ModuleList([Block(config) for _ in range(config.n_layer)]),
                ln_f = LayerNorm(config.n_embd, bias=config.bias),
            ))

        else:
            self.transformer = nn.ModuleDict(dict(
                wpe = nn.Embedding(config.block_size, config.n_embd),

                drop = nn.Dropout(config.dropout),
                h = nn.ModuleList([Block(config) for _ in range(config.n_layer)]),
                ln_f = LayerNorm(config.n_embd, bias=config.bias),
            ))

            self.transformer.wpe.weight.data = sinusoids(config.block_size, config.n_embd)
            self.transformer.wpe.requires_grad_(False)

    def subsampled_lengths(self, input_lengths):
        # https://github.com/vdumoulin/conv_arithmetic
        p, k, s = self.conv_subsample.padding[0], self.conv_subsample.kernel_size[0], self.conv_subsample.stride[0]
        o = input_lengths + 2 * p - k
        o = torch.floor(o / s + 1)
        return o.int()

    def forward(self, x, input_lengths, measure_entropy=False):
        x = x.mT
        x = F.gelu(self.conv_pre(x))
        x = F.gelu(self.conv_subsample(x))
        x = x.mT

        _, t, c = x.size()
        pos = torch.arange(0, t, dtype=torch.long, device=x.device).unsqueeze(0) # shape (1, t)
        if self.config.rotary_emb_dim:
            x = self.transformer.drop(x) # shape (b, t, c)
        else:
            pe = self.transformer.wpe(pos)
            x = self.transformer.drop(x + pe) # shape (b, t, c)

        for i, block in enumerate(self.transformer.h):
            x, _att_entropy, _present = block(x, past=None, measure_entropy=measure_entropy)
        x = self.transformer.ln_f(x)

        return x, self.subsampled_lengths(input_lengths), {}


if __name__ == '__main__':
    from ha.init import AudioEncoderConfig
    config = AudioEncoderConfig()
    encoder = AudioEncoder(config)
    print(encoder(torch.randn(1, config.block_size, config.d_input)))
