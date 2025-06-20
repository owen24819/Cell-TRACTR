# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
"""
Various positional encodings for the transformer.
"""
import math
import torch
from torch import nn

from ..util.misc import NestedTensor


class PositionEmbeddingSine3D(nn.Module):
    """
    This is a more standard version of the position embedding, very similar to the one
    used by the Attention is all you need paper, generalized to work on images.
    """
    # def __init__(self, num_pos_feats=64, temperature=10000, normalize=False, scale=None):
    def __init__(self, num_pos_feats=64, num_frames=2, temperature=10000, normalize=False, scale=None):
        super().__init__()
        self.num_pos_feats = num_pos_feats
        self.temperature = temperature
        self.normalize = normalize
        self.frames = num_frames

        if scale is not None and normalize is False:
            raise ValueError("normalize should be True if scale is passed")
        if scale is None:
            scale = 2 * math.pi
        self.scale = scale

    def forward(self, tensor_list: NestedTensor):
        x = tensor_list.tensors
        mask = tensor_list.mask
        n, h, w = mask.shape
        # assert n == 1
        # mask = mask.reshape(1, 1, h, w)
        mask = mask.view(n, 1, h, w)
        mask = mask.expand(n, self.frames, h, w)

        assert mask is not None
        not_mask = ~mask
        # y_embed = not_mask.cumsum(1, dtype=torch.float32)
        # x_embed = not_mask.cumsum(2, dtype=torch.float32)

        z_embed = not_mask.cumsum(1, dtype=torch.float32)
        y_embed = not_mask.cumsum(2, dtype=torch.float32)
        x_embed = not_mask.cumsum(3, dtype=torch.float32)

        if self.normalize:
            eps = 1e-6
            # y_embed = y_embed / (y_embed[:, -1:, :] + eps) * self.scale
            # x_embed = x_embed / (x_embed[:, :, -1:] + eps) * self.scale

            z_embed = z_embed / (z_embed[:, -1:, :, :] + eps) * self.scale
            y_embed = y_embed / (y_embed[:, :, -1:, :] + eps) * self.scale
            x_embed = x_embed / (x_embed[:, :, :, -1:] + eps) * self.scale

        dim_t = torch.arange(self.num_pos_feats, dtype=torch.float32, device=x.device)
        dim_t = self.temperature ** (2 * (torch.div(dim_t,2,rounding_mode='floor')) / self.num_pos_feats)

        # pos_x = x_embed[:, :, :, None] / dim_t
        # pos_y = y_embed[:, :, :, None] / dim_t
        # pos_x = torch.stack((
        #     pos_x[:, :, :, 0::2].sin(),
        #     pos_x[:, :, :, 1::2].cos()), dim=4).flatten(3)
        # pos_y = torch.stack((
        #     pos_y[:, :, :, 0::2].sin(),
        #     pos_y[:, :, :, 1::2].cos()), dim=4).flatten(3)
        # pos = torch.cat((pos_y, pos_x), dim=3).permute(0, 3, 1, 2)

        pos_x = x_embed[:, :, :, :, None] / dim_t
        pos_y = y_embed[:, :, :, :, None] / dim_t
        pos_z = z_embed[:, :, :, :, None] / dim_t
        pos_x = torch.stack((pos_x[:, :, :, :, 0::2].sin(), pos_x[:, :, :, :, 1::2].cos()), dim=5).flatten(4)
        pos_y = torch.stack((pos_y[:, :, :, :, 0::2].sin(), pos_y[:, :, :, :, 1::2].cos()), dim=5).flatten(4)
        pos_z = torch.stack((pos_z[:, :, :, :, 0::2].sin(), pos_z[:, :, :, :, 1::2].cos()), dim=5).flatten(4)
        # pos_w = torch.zeros_like(pos_z)
        # pos = torch.cat((pos_w, pos_z, pos_y, pos_x), dim=4).permute(0, 1, 4, 2, 3)
        pos = torch.cat((pos_z, pos_y, pos_x), dim=4).permute(0, 1, 4, 2, 3)

        return pos


class PositionEmbeddingSine(nn.Module):
    """
    This is a more standard version of the position embedding, very similar to the one
    used by the Attention is all you need paper, generalized to work on images.
    """
    def __init__(self, num_pos_feats=64, temperature=10000, normalize=False, scale=None):
        super().__init__()
        self.num_pos_feats = num_pos_feats
        self.temperature = temperature
        self.normalize = normalize
        if scale is not None and normalize is False:
            raise ValueError("normalize should be True if scale is passed")
        if scale is None:
            scale = 2 * math.pi
        self.scale = scale

    def deterministic_cumsum(self, t, dim):
        output = torch.zeros_like(t).float()
        for i in range(1, t.size(dim)):
            output.select(dim, i).add_(output.select(dim, i-1)).add_(t.select(dim, i))
        return output

    def forward(self, tensor_list: NestedTensor):
        x = tensor_list.tensors
        mask = tensor_list.mask
        assert mask is not None
        not_mask = ~mask
        # y_embed = not_mask.cumsum(1, dtype=torch.float32)
        # x_embed = not_mask.cumsum(2, dtype=torch.float32)

        y_embed = self.deterministic_cumsum(not_mask,1)
        x_embed = self.deterministic_cumsum(not_mask,2)

        if self.normalize:
            eps = 1e-6
            y_embed = (y_embed - 0.5) / (y_embed[:, -1:, :] + eps) * self.scale
            x_embed = (x_embed - 0.5) / (x_embed[:, :, -1:] + eps) * self.scale

        dim_t = torch.arange(self.num_pos_feats, dtype=torch.float32, device=x.device)
        dim_t = self.temperature ** (2 * (torch.div(dim_t,2,rounding_mode='floor')) / self.num_pos_feats)

        pos_x = x_embed[:, :, :, None] / dim_t
        pos_y = y_embed[:, :, :, None] / dim_t
        pos_x = torch.stack((pos_x[:, :, :, 0::2].sin(), pos_x[:, :, :, 1::2].cos()), dim=4).flatten(3)
        pos_y = torch.stack((pos_y[:, :, :, 0::2].sin(), pos_y[:, :, :, 1::2].cos()), dim=4).flatten(3)
        pos = torch.cat((pos_y, pos_x), dim=3).permute(0, 3, 1, 2)
        return pos


class PositionEmbeddingLearned(nn.Module):
    """
    Absolute pos embedding, learned.
    """
    def __init__(self, num_pos_feats=256):
        super().__init__()
        self.row_embed = nn.Embedding(50, num_pos_feats)
        self.col_embed = nn.Embedding(50, num_pos_feats)
        self.reset_parameters()

    def reset_parameters(self):
        nn.init.uniform_(self.row_embed.weight)
        nn.init.uniform_(self.col_embed.weight)

    def forward(self, tensor_list: NestedTensor):
        x = tensor_list.tensors
        h, w = x.shape[-2:]
        i = torch.arange(w, device=x.device)
        j = torch.arange(h, device=x.device)
        x_emb = self.col_embed(i)
        y_emb = self.row_embed(j)
        pos = torch.cat([
            x_emb.unsqueeze(0).repeat(h, 1, 1),
            y_emb.unsqueeze(1).repeat(1, w, 1),
        ], dim=-1).permute(2, 0, 1).unsqueeze(0).repeat(x.shape[0], 1, 1, 1)
        return pos


def build_position_encoding(args):
    # n_steps = args.hidden_dim // 2
    # n_steps = args.hidden_dim // 4
    if args.multi_frame_attention and args.multi_frame_encoding:
        n_steps = args.hidden_dim // 3
        # n_steps = args.hidden_dim // 3
        sine_emedding_func = PositionEmbeddingSine3D
    else:
        n_steps = args.hidden_dim // 2
        sine_emedding_func = PositionEmbeddingSine

    if args.position_embedding in ('v2', 'sine'):
        # TODO find a better way of exposing other arguments
        position_embedding = sine_emedding_func(n_steps, normalize=True)
    elif args.position_embedding in ('v3', 'learned'):
        position_embedding = PositionEmbeddingLearned(n_steps)
    else:
        raise ValueError(f"not supported {args.position_embedding}")

    return position_embedding
