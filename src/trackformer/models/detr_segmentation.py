# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

import fvcore.nn.weight_init as weight_init

import torch
import torch.nn as nn

from ..util.misc import NestedTensor, MLP

from .deformable_detr import DeformableDETR, _get_clones
from .deformable_transformer import DeformableTransformer
from .detr_tracking import DETRTrackingBase


class DETRSegmBase(nn.Module):
    def __init__(self, freeze_detr=False,return_intermediate_masks=False, mask_dim = 288):
        
        if freeze_detr:
            for param in self.parameters():
                param.requires_grad_(False)

        self.return_intermediate_masks = return_intermediate_masks
        self.mask_dim = mask_dim

        self.decoder_norm  = nn.LayerNorm(self.hidden_dim)
        self.mask_embed = MLP(self.hidden_dim, self.hidden_dim, self.mask_dim*2, 3)

        num_pred = 1

        if self.two_stage:
            num_pred += 1

        if self.return_intermediate_masks:
            num_pred += (self.decoder.num_layers-1)

            if self.num_OD_layers > 0:
                self.OD_mask_embed_index = self.decoder.num_layers

        elif self.num_OD_layers > 0:
            num_pred += 1

            if self.return_intermediate_masks:
                self.OD_mask_embed_index = self.decoder.num_layers
            else:
                self.OD_mask_embed_index = 1

        if self.share_bbox_layers:
            self.mask_embed = nn.ModuleList([self.mask_embed for _ in range(num_pred)])
        else:
            self.mask_embed = _get_clones(self.mask_embed, num_pred)    

        if self.return_intermediate_masks:
            self.final_mask_embed_index = self.decoder.num_layers - 1
        else:
            self.final_mask_embed_index = 0

        mask_num_feature_levels = self.num_feature_levels

        if self.use_img_for_mask:
            mask_num_feature_levels += 1

        # use 1x1 conv instead
        self.mask_features = nn.Conv2d(
            self.d_model*mask_num_feature_levels,
            self.mask_dim,
            kernel_size=1,
            stride=1,
            padding=0,
        )

        if self.use_img_for_mask:
            self.img_encoder = nn.Sequential(
                nn.Conv2d(3, self.d_model, kernel_size=3, stride=1, padding='same', bias=True),
                nn.GroupNorm(self.d_model // 8, self.d_model),
                nn.ReLU(),
                nn.Conv2d(self.d_model, self.d_model, kernel_size=3, stride=1, padding='same', bias=True),
                nn.ReLU(),
                nn.GroupNorm(self.d_model // 8, self.d_model),
                )

        weight_init.c2_xavier_fill(self.mask_features)

        self.lateral_layers = []
        self.output_layers = []

        feature_channels = self.backbone.num_channels
        in_channels = feature_channels[:self.num_feature_levels][::-1]

        # if self.use_img_for_mask:
        #     in_channels.append(self.d_model)

        for in_channel in in_channels:

            lateral_layer = nn.Sequential(
                nn.Conv2d(in_channel, self.d_model, kernel_size=1, bias=True),
                nn.GroupNorm(self.d_model // 8, self.d_model)
                )

            output_layer = nn.Sequential(
                nn.Conv2d(self.d_model, self.d_model, kernel_size=3, stride=1, padding=1, bias=True),
                nn.ReLU(),
                nn.GroupNorm(self.d_model // 8, self.d_model),
                )

            weight_init.c2_xavier_fill(lateral_layer[0])
            weight_init.c2_xavier_fill(output_layer[0])

            self.lateral_layers.append(lateral_layer)
            self.output_layers.append(output_layer)

        self.lateral_layers = nn.ModuleList(self.lateral_layers).to(self.device)
        self.output_layers = nn.ModuleList(self.output_layers).to(self.device)

    def forward_prediction_heads(self, output, i):
        decoder_output = self.decoder_norm(output.transpose(0,1))
        decoder_output = decoder_output.transpose(0, 1)

        mask_embed = self.mask_embed[i](decoder_output)

        outputs_mask_1 = torch.einsum("bqc,bchw->bqhw", mask_embed[:,:,:self.mask_dim], self.all_mask_features)
        outputs_mask_2 = torch.einsum("bqc,bchw->bqhw", mask_embed[:,:,self.mask_dim:], self.all_mask_features)

        outputs_mask = torch.stack((outputs_mask_1,outputs_mask_2),axis=2)

        return outputs_mask

    def forward(self, samples: NestedTensor, targets: list = None):

        # If model is not used for tracking, 
        if not self.tracking and self.training:
            for target in targets:
                target['main']['cur_target']['track_queries_mask'] = torch.zeros((self.num_queries)).bool().to(self.device)

        out, targets, features, memory, hs = super().forward(samples, targets)

        pred_masks = self.forward_prediction_heads(hs[-1],self.final_mask_embed_index)
        out["pred_masks"] = pred_masks

        if self.return_intermediate_masks:
            for i in range(len(hs) - 1):
                pred_masks = self.forward_prediction_heads(hs[i],i)
                out["aux_outputs"][i]['pred_masks'] = pred_masks

        if 'OD' in out:
            pred_masks = self.forward_prediction_heads(out['OD']['hs_embed'],self.OD_mask_embed_index)
            out['OD']['pred_masks'] = pred_masks

        return out, targets, features, memory, hs
    
class DeformableDETRSegm(DETRSegmBase, DeformableDETR, DeformableTransformer):
    def __init__(self, mask_kwargs, detr_kwargs, transformer_kwargs):
        DeformableTransformer.__init__(self, **transformer_kwargs)
        DeformableDETR.__init__(self, **detr_kwargs)
        DETRSegmBase.__init__(self, **mask_kwargs)

class DeformableDETRSegmTracking(DETRSegmBase, DETRTrackingBase, DeformableDETR, DeformableTransformer):
    def __init__(self, mask_kwargs, tracking_kwargs, detr_kwargs, transformer_kwargs):
        DeformableTransformer.__init__(self, **transformer_kwargs)
        DeformableDETR.__init__(self, **detr_kwargs)
        DETRTrackingBase.__init__(self, **tracking_kwargs)
        DETRSegmBase.__init__(self, **mask_kwargs)