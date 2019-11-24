from fairseq import options, utils
from fairseq.models import (
    FairseqEncoder,
)

from fairseq.modules import PositionalEmbedding

import torch
from torch.nn import functional as F
from torch import nn
import math
DEFAULT_MAX_SOURCE_POSITIONS = 1024
DEFAULT_MAX_TARGET_POSITIONS = 1024


def compute_conv_mask(lengths, stride):
    # lengths: B
    # we use odd-number kernel
    valid_lengths = (lengths - 1) / stride + 1
    max_length = torch.max(valid_lengths).item()
    mask = torch.arange(max_length, device=lengths.device).type_as(lengths).expand(len(lengths), max_length)
    mask = mask < valid_lengths.unsqueeze(1)
    return valid_lengths, mask  # mask -> batch x T'


class ConvEncoder(nn.Module):
    def __init__(self, input_channel, kernels, strides, latent_dim):
        super().__init__()
        self.strides = strides
        self.conv_blocks = nn.ModuleList([])
        self.conv_blocks.extend([nn.Conv1d(input_channel, input_channel, kernel_size=k, padding=k//2, stride=s)
                                 for k, s in zip(kernels, strides)])
        self.quant_conv = nn.Conv1d(input_channel, latent_dim, 1)

    def forward(self, input, length):
        # input: batch x C x T
        new_mask = None
        for ii, (conv_layer, s) in enumerate(zip(self.conv_blocks, self.strides)):
            input = F.relu(conv_layer(input))
            length, new_mask = compute_conv_mask(length, s)
            input = input * new_mask.type_as(input).unsqueeze(1)
        output = self.quant_conv(input)
        # output: batch x C' x T' -> T' x batch x C'
        # new_mask: batch x T'
        return output.permute(2, 0, 1), new_mask


class ConvBlock(nn.Module):
    def __init__(self, input_channel, kernel, stride, output_channel=None):
        super().__init__()
        self.stride = stride
        if output_channel is None:
            output_channel = input_channel
        self.conv1 = nn.Conv1d(input_channel, output_channel, kernel_size=kernel, padding=kernel//2, stride=stride)
        self.bn1 = nn.BatchNorm1d(output_channel)
        self.conv2 = nn.Conv1d(input_channel, output_channel, kernel_size=kernel, padding=kernel//2)
        self.bn2 = nn.BatchNorm1d(output_channel)
        self.downsample = nn.Sequential(nn.Conv1d(input_channel, output_channel, 1, stride=stride),
                                        nn.BatchNorm1d(output_channel))

    def forward(self, x, input_length):
        # input: batch x C x T

        new_length, new_mask = compute_conv_mask(input_length, self.stride)
        residual = self.downsample(x)
        mask = new_mask.type_as(residual)

        out = self.conv1(x)
        out = self.bn1(out)
        out = F.relu(out)

        out = out * mask.unsqueeze(1)
        out = self.conv2(out)
        out = self.bn2(out)

        out = out + residual
        out = F.relu(out)

        out = out * mask.unsqueeze(1)
        return out, new_length, new_mask


class MultiKernelConvBlock(nn.Module):
    def __init__(self, input_channel, kernels, stride, single_output_channel):
        super().__init__()
        self.stride = stride
        self.kernels = kernels

        output_channel = single_output_channel * len(kernels)
        self.conv_block_1 = nn.ModuleList([])
        self.conv_block_1.extend([nn.Conv1d(input_channel, single_output_channel, kernel_size=k,
                                            padding=k//2 if k % 2 != 0 else 0, stride=stride) for k in kernels])
        self.bn1 = nn.BatchNorm1d(output_channel)

        self.conv_block_2 = nn.ModuleList([])
        self.conv_block_2.extend([nn.Conv1d(output_channel, single_output_channel, kernel_size=k,
                                            padding=k//2 if k % 2 != 0 else 0, stride=1) for k in kernels])
        self.bn2 = nn.BatchNorm1d(output_channel)

        self.downsample = nn.Sequential(nn.Conv1d(input_channel, output_channel, 1, stride=stride),
                                        nn.BatchNorm1d(output_channel))

    def forward(self, x, input_length):
        # input: batch x C x T

        new_length, new_mask = compute_conv_mask(input_length, self.stride)
        residual = self.downsample(x)
        mask = new_mask.type_as(residual)

        outs = [self.conv_block_1[ii](x if k % 2 != 0 else F.pad(x, (k//2, k//2-1), 'constant', 0))
                for ii, k in enumerate(self.kernels)]
        out = torch.cat(outs, dim=1)
        out = self.bn1(out)
        out = F.relu(out)

        out = out * mask.unsqueeze(1)
        outs = [self.conv_block_2[ii](out if k % 2 != 0 else F.pad(out, (k//2, k//2-1), 'constant', 0))
                for ii, k in enumerate(self.kernels)]
        out = torch.cat(outs, dim=1)
        out = self.bn2(out)

        out = out + residual
        out = F.relu(out)

        out = out * mask.unsqueeze(1)
        return out, new_length, new_mask


class FullConvEncoder(FairseqEncoder):
    def __init__(self, args, input_channel, kernels, strides, latent_dim, embed_tokens, dictionary):
        # kernels: [[3,4,5], [2,3,4]]
        super().__init__(dictionary)
        # input_channel = embed_dim
        self.padding_idx = embed_tokens.padding_idx
        self.embed_tokens = embed_tokens
        self.embed_scale = math.sqrt(input_channel)
        self.embed_positions = PositionalEmbedding(DEFAULT_MAX_SOURCE_POSITIONS, input_channel, self.padding_idx, learned=args.encoder_learned_pos)
        self.strides = strides

        single_kernel_channel = 256
        input_channels = [input_channel] + [single_kernel_channel * len(kk) for kk in kernels]
        self.conv_blocks = nn.ModuleList([])
        self.conv_blocks.extend([MultiKernelConvBlock(d, k, s, single_kernel_channel)
                                 for k, s, d in zip(kernels, strides, input_channels[:-1])])
        self.quant_conv = nn.Conv1d(input_channels[-1], latent_dim, 1)
        self.dropout = args.dropout

        self.pad_index = dictionary.pad_index
        self.bos_index = dictionary.bos_index

    def forward_embedding(self, src_tokens):
        # embed tokens and positions
        embed = self.embed_scale * self.embed_tokens(src_tokens)
        if self.embed_positions is not None:
            x = embed + self.embed_positions(src_tokens)
        x = F.dropout(x, p=self.dropout, training=self.training)
        return x, embed

    def forward(self, src_tokens, length):
        x, encoder_embedding = self.forward_embedding(src_tokens)
        encoding_mask = (~(src_tokens.eq(self.pad_index) | src_tokens.eq(self.bos_index))).type_as(x)
        x = x * (encoding_mask.unsqueeze(-1))  # B x T x C
        x = x.transpose(1, 2)
        for block in self.conv_blocks:
            x, length, mask = block(x, length)
        x = self.quant_conv(x)  # B x C x T'
        return x.permute(2, 0, 1), mask