# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
"""
Transforms and data augmentation for both image + bbox.
"""
import random
from typing import Union
import numpy as np
from scipy import interpolate as scipy_interpolate

import PIL
from PIL import ImageFilter
import torch
import torchvision.transforms as T
import torchvision.transforms.functional as F

from ..util.box_ops import box_xyxy_to_cxcywh, masks_to_boxes
from ..util.misc import interpolate

def crop(image, target, region, overflow_boxes=False):
    i, j, h, w = region
    target = target.copy()

    if isinstance(image, torch.Tensor):
        cropped_image = image[:, j:j + w, i:i + h]
    else:
        cropped_image = F.crop(image, *region)

    # should we do something wrt the original size?
    target["size"] = torch.tensor([h, w])

    fields = ["labels", "boxes", "area", "iscrowd", "ignore", "track_ids", "flexible_divisions"]
    
    if "masks" in target:

        fields.append("masks")
        area = target['masks'].sum(dim=(1,2))
        target["area"] = area

        cropped_boxes = masks_to_boxes(target['masks'])
        target["boxes"] = cropped_boxes

    else:

        boxes = target["boxes"]
        max_size = torch.as_tensor([w, h], dtype=torch.float32)
        cropped_boxes = boxes -  torch.as_tensor([j, i, j, i])

        if overflow_boxes:
            for i, box in enumerate(cropped_boxes):
                l, t, r, b = box
                if l < 0 and r < 0:
                    l = r = 0
                if l > w and r > w:
                    l = r = w
                if t < 0 and b < 0:
                    t = b = 0
                if t > h and b > h:
                    t = b = h
                cropped_boxes[i] = torch.tensor([l, t, r, b], dtype=box.dtype)
            cropped_boxes = cropped_boxes.reshape(-1, 2, 2)
        else:
            cropped_boxes = torch.min(cropped_boxes.reshape(-1, 2, 2), max_size)
            cropped_boxes = cropped_boxes.clamp(min=0)

            area = (cropped_boxes[:, 1, :] - cropped_boxes[:, 0, :]).prod(dim=1)
            target["boxes"] = cropped_boxes.reshape(-1, 4)
            target["area"] = area

    keep = area > 10

    if 'masks' in target:
        area_boxes = ((target['boxes'][:,2] - target['boxes'][:,0]) * (target['boxes'][:,3] - target['boxes'][:,1]))
        keep *= (area_boxes > 10)

    if (~keep).sum() > 0:
        for field in fields:
            if field in target:
                target[field] = target[field][keep]

    assert target['boxes'].shape[0] == (target['boxes'][:,2] - target['boxes'][:,0] > 0).sum() == (target['boxes'][:,3] - target['boxes'][:,1] > 0).sum()

    return cropped_image, target


def hflip(image, target):
    if isinstance(image, torch.Tensor):
        flipped_image = image.flip(-1)
        _, width, _ = image.size()
    else:
        flipped_image = F.hflip(image)
        width, _ = image.size

    target = target.copy()

    if "boxes" in target:
        boxes = target["boxes"]
        boxes = boxes[:, [2, 1, 0, 3,6,5,4,7]] \
            * torch.as_tensor([-1, 1, -1, 1, -1, 1, -1, 1]) \
            + torch.as_tensor([width, 0, width, 0,width,0,width,0])
        target["boxes"] = boxes

    if "boxes_ignore" in target:
        boxes = target["boxes_ignore"]
        boxes = boxes[:, [2, 1, 0, 3]] \
            * torch.as_tensor([-1, 1, -1, 1]) \
            + torch.as_tensor([width, 0, width, 0])
        target["boxes_ignore"] = boxes

    if "masks" in target:
        target['masks'] = target['masks'].flip(-1)

    return flipped_image, target


def resize(image, target, size, max_size=None):
    # size can be min_size (scalar) or (w, h) tuple

    def get_size_with_aspect_ratio(image_size, size, max_size=None):
        w, h = image_size
        if max_size is not None:
            min_original_size = float(min((w, h)))
            max_original_size = float(max((w, h)))
            if max_original_size / min_original_size * size > max_size:
                size = int(round(max_size * min_original_size / max_original_size))

        if (w <= h and w == size) or (h <= w and h == size):
            return (h, w)

        if w < h:
            ow = size
            oh = int(size * h / w)
        else:
            oh = size
            ow = int(size * w / h)

        return (oh, ow)

    def get_size(image_size, size, max_size=None):
        if isinstance(size, (list, tuple)):
            return size[::-1]
        else:
            return get_size_with_aspect_ratio(image_size, size, max_size)

    size = get_size(image.size, size, max_size)
    rescaled_image = F.resize(image, size)

    if target is None:
        return rescaled_image, None

    ratios = tuple(float(s) / float(s_orig) for s, s_orig in zip(rescaled_image.size, image.size))
    ratio_width, ratio_height = ratios

    target = target.copy()
    if "boxes" in target:
        boxes = target["boxes"]
        scaled_boxes = boxes \
            * torch.as_tensor([ratio_width, ratio_height, ratio_width, ratio_height])
        target["boxes"] = scaled_boxes

    if "area" in target:
        area = target["area"]
        scaled_area = area * (ratio_width * ratio_height)
        target["area"] = scaled_area

    h, w = size
    target["size"] = torch.tensor([h, w])

    if "masks" in target:
        target['masks'] = interpolate(
            target['masks'][:, None].float(), size, mode="nearest")[:, 0] > 0.5

    return rescaled_image, target


def pad(image, target, padding):
    # pad_left, pad_top, pad_right, pad_bottom
    padded_image = F.pad(image, padding)
    if target is None:
        return padded_image, None
    target = target.copy()
    # should we do something wrt the original size?
    w, h = padded_image.size

    if "boxes" in target:
        # correct xyxy from left and right paddings
        target["boxes"] += torch.tensor(
            [padding[0], padding[1], padding[0], padding[1]])

    target["size"] = torch.tensor([h, w])
    if "masks" in target:
        # padding_left, padding_right, padding_top, padding_bottom
        target['masks'] = torch.nn.functional.pad(
            target['masks'],
            (padding[0], padding[2], padding[1], padding[3]))
    return padded_image, target


class RandomCrop:
    def __init__(self, size, overflow_boxes=False):
        # in hxw
        self.size = size
        self.overflow_boxes = overflow_boxes
        self.region = None

    def __call__(self, img, target):
        return crop(img, target, self.region, self.overflow_boxes)


class RandomSizeCrop:
    def __init__(self,
                 min_size: Union[tuple, list, int],
                 max_size: Union[tuple, list, int] = None,
                 overflow_boxes: bool = False):
        if isinstance(min_size, int):
            min_size = (min_size, min_size)
        if isinstance(max_size, int):
            max_size = (max_size, max_size)

        self.min_size = min_size
        self.max_size = max_size
        self.overflow_boxes = overflow_boxes

    def __call__(self, img: PIL.Image.Image, target: dict):
        if self.max_size is None:
            w = random.randint(min(self.min_size[0], img.width), img.width)
            h = random.randint(min(self.min_size[1], img.height), img.height)
        else:
            w = random.randint(
                min(self.min_size[0], img.width),
                min(img.width, self.max_size[0]))
            h = random.randint(
                min(self.min_size[1], img.height),
                min(img.height, self.max_size[1]))

        region = T.RandomCrop.get_params(img, [h, w])
        return crop(img, target, region, self.overflow_boxes)


class CenterCrop:
    def __init__(self, size, overflow_boxes=False):
        self.size = size
        self.overflow_boxes = overflow_boxes

    def __call__(self, img, target):
        image_width, image_height = img.size
        crop_height, crop_width = self.size
        crop_top = int(round((image_height - crop_height) / 2.))
        crop_left = int(round((image_width - crop_width) / 2.))
        return crop(img, target, (crop_top, crop_left, crop_height, crop_width), self.overflow_boxes)


class RandomHorizontalFlip:
    def __init__(self, p=0.5):
        self.p = p

    def __call__(self, img, target):
        if random.random() < self.p:
            return hflip(img, target)
        return img, target

def gaussian_blur(img,target,radius):
    img = img.filter(ImageFilter.GaussianBlur(radius=radius))
    return img,target

class RandomGaussianBlur:
    def __init__(self, p=0.4):
        self.p = p
        self.sigma = [0.1,1.5]

    def __call__(self, img, target):
        if random.random() < self.p:
            radius = random.uniform(self.sigma[0],self.sigma[1])
            return gaussian_blur(img, target, radius)
        return img, target


def gaussian_noise(img,target,sigma):
    img = np.array(img)[:,:,0].astype(np.float32)
    img = img + np.random.normal(0, sigma, img.shape).astype(np.float32)*255
    img = np.clip(img,0,255).astype(np.uint8)
    img = np.repeat(img[:,:,None],3,axis=-1)
    img = PIL.Image.fromarray(img,'RGB')
    return img,target

class RandomGaussianNoise:
    def __init__(self, p=0.4):
        self.p = p
        self.sigma = 0.05

    def __call__(self, img, target):
        if random.random() < self.p:
            sigma = random.random() * self.sigma
            return gaussian_noise(img, target, sigma)
        return img, target



def illumination_voodoo(img,target,num_control_points):
    img = np.array(img)[:,:,0].astype(np.float32)
    # Create a random curve along the length of the chamber:
    control_points = np.linspace(0, img.shape[0] - 1, num=num_control_points)
    random_points = np.random.uniform(low=0.1, high=0.9, size=num_control_points)
    mapping = scipy_interpolate.PchipInterpolator(control_points, random_points)
    curve = mapping(np.linspace(0, img.shape[0] - 1, img.shape[0]))
    # Apply this curve to the image intensity along the length of the chamebr:
    newimage = np.multiply(
        img,
        np.reshape(
            np.tile(np.reshape(curve, curve.shape + (1,)), (1, img.shape[1])),
            img.shape,
        ),
    )
    # Rescale values to original range:
    newimage = np.interp(
        newimage, (newimage.min(), newimage.max()), (img.min(), img.max())
    )

    img = np.clip(newimage,0,255).astype(np.uint8)
    img = np.repeat(img[:,:,None],3,axis=-1)
    img = PIL.Image.fromarray(img,'RGB')
    return img,target

class RandomIlluminationVoodoo:
    def __init__(self, p=0.4):
        self.p = p
        self.num_control_points = 5

    def __call__(self, img, target):
        if random.random() < self.p:
            return illumination_voodoo(img, target, self.num_control_points)
        return img, target

class RepeatUntilMaxObjects:
    def __init__(self, transforms, num_max_objects):
        self._num_max_objects = num_max_objects
        self._transforms = transforms

    def __call__(self, img, target):
        num_objects = None
        while num_objects is None or num_objects > self._num_max_objects:
            img_trans, target_trans = self._transforms(img, target)
            num_objects = len(target_trans['boxes'])
        return img_trans, target_trans


class RandomResize:
    def __init__(self, sizes, max_size=None):
        assert isinstance(sizes, (list, tuple))
        self.sizes = sizes
        self.max_size = max_size

    def __call__(self, img, target=None):
        size = random.choice(self.sizes)
        return resize(img, target, size, self.max_size)


class RandomResizeTargets:
    def __init__(self, scale=0.5):
        self.scalce = scale

    def __call__(self, img, target=None):
        img = F.to_tensor(img)
        img_c, img_w, img_h = img.shape

        rescaled_boxes = []
        rescaled_box_images = []
        for box in target['boxes']:
            y1, x1, y2, x2 = box.int().tolist()
            w = x2 - x1
            h = y2 - y1

            box_img = img[:, x1:x2, y1:y2]
            random_scale = random.uniform(0.5, 2.0)
            scaled_width = int(random_scale * w)
            scaled_height = int(random_scale * h)

            box_img = F.to_pil_image(box_img)
            rescaled_box_image = F.resize(
                box_img,
                (scaled_width, scaled_height))
            rescaled_box_images.append(F.to_tensor(rescaled_box_image))
            rescaled_boxes.append([y1, x1, y1 + scaled_height, x1 + scaled_width])

        for box in target['boxes']:
            y1, x1, y2, x2 = box.int().tolist()
            w = x2 - x1
            h = y2 - y1

            erase_value = torch.empty(
                [img_c, w, h],
                dtype=torch.float32).normal_()

            img = F.erase(
                img, x1, y1, w, h, erase_value, True)

        for box, rescaled_box_image in zip(target['boxes'], rescaled_box_images):
            y1, x1, y2, x2 = box.int().tolist()
            w = x2 - x1
            h = y2 - y1
            _, scaled_width, scaled_height = rescaled_box_image.shape

            rescaled_box_image = rescaled_box_image[
                :,
                :scaled_width - max(x1 + scaled_width - img_w, 0),
                :scaled_height - max(y1 + scaled_height - img_h, 0)]

            img[:, x1:x1 + scaled_width, y1:y1 + scaled_height] = rescaled_box_image

        target['boxes'] = torch.tensor(rescaled_boxes).float()
        img = F.to_pil_image(img)
        return img, target


class RandomPad:
    def __init__(self, max_size):
        if isinstance(max_size, int):
            max_size = (max_size, max_size)

        self.max_size = max_size

    def __call__(self, img, target):
        w, h = img.size
        pad_width = random.randint(0, max(self.max_size[0] - w, 0))
        pad_height = random.randint(0, max(self.max_size[1] - h, 0))

        pad_left = random.randint(0, pad_width)
        pad_right = pad_width - pad_left
        pad_top = random.randint(0, pad_height)
        pad_bottom = pad_height - pad_top

        padding = (pad_left, pad_top, pad_right, pad_bottom)

        return pad(img, target, padding)


class RandomSelect:
    """
    Randomly selects between transforms1 and transforms2,
    with probability p for transforms1 and (1 - p) for transforms2
    """
    def __init__(self, transforms1, transforms2, p=0.5):
        self.transforms1 = transforms1
        self.transforms2 = transforms2
        self.p = p

    def __call__(self, img, target):
        if random.random() < self.p:
            return self.transforms1(img, target)
        return self.transforms2(img, target)


class ToTensor:
    def __call__(self, img, target=None):
        return F.to_tensor(img), target


class RandomErasing:

    def __init__(self, p=0.5, scale=(0.02, 0.33), ratio=(0.3, 3.3), value=0, inplace=False):
        self.eraser = T.RandomErasing()
        self.p = p
        self.scale = scale
        self.ratio = ratio
        self.value = value
        self.inplace = inplace

    def __call__(self, img, target):
        if random.uniform(0, 1) < self.p:
            img = F.to_tensor(img)

            x, y, h, w, v = self.eraser.get_params(
                img, scale=self.scale, ratio=self.ratio, value=self.value)

            img = F.erase(img, x, y, h, w, v, self.inplace)
            img = F.to_pil_image(img)

            # target
            fields = ['boxes', "labels", "area", "iscrowd", "ignore", "track_ids"]

            if 'boxes' in target:
                erased_box = torch.tensor([[y, x, y + w, x + h]]).float()

                lt = torch.max(erased_box[:, None, :2], target['boxes'][:, :2])  # [N,M,2]
                rb = torch.min(erased_box[:, None, 2:], target['boxes'][:, 2:])  # [N,M,2]
                wh = (rb - lt).clamp(min=0)  # [N,M,2]
                inter = wh[:, :, 0] * wh[:, :, 1]  # [N,M]

                keep = inter[0] <= 0.7 * target['area']

                left = torch.logical_and(
                    target['boxes'][:, 0] < erased_box[:, 0],
                    target['boxes'][:, 2] > erased_box[:, 0])
                left = torch.logical_and(left, inter[0].bool())

                right = torch.logical_and(
                    target['boxes'][:, 0] < erased_box[:, 2],
                    target['boxes'][:, 2] > erased_box[:, 2])
                right = torch.logical_and(right, inter[0].bool())

                top = torch.logical_and(
                    target['boxes'][:, 1] < erased_box[:, 1],
                    target['boxes'][:, 3] > erased_box[:, 1])
                top = torch.logical_and(top, inter[0].bool())

                bottom = torch.logical_and(
                    target['boxes'][:, 1] < erased_box[:, 3],
                    target['boxes'][:, 3] > erased_box[:, 3])
                bottom = torch.logical_and(bottom, inter[0].bool())

                only_one_crop = (top.float() + bottom.float() + left.float() + right.float()) > 1
                left[only_one_crop] = False
                right[only_one_crop] = False
                top[only_one_crop] = False
                bottom[only_one_crop] = False

                target['boxes'][:, 2][left] = erased_box[:, 0]
                target['boxes'][:, 0][right] = erased_box[:, 2]
                target['boxes'][:, 3][top] = erased_box[:, 1]
                target['boxes'][:, 1][bottom] = erased_box[:, 3]

                for field in fields:
                    if field in target:
                        target[field] = target[field][keep]

        return img, target


class Normalize:
    def __init__(self, mean, std):
        self.mean = mean
        self.std = std

    def __call__(self, image, target=None):
        image = F.normalize(image, mean=self.mean, std=self.std)
        if target is None:
            return image, None

        if not target['empty']:
            target = target.copy()
            h, w = image.shape[-2:]
            if "boxes" in target:
                boxes = target["boxes"]
                boxes = torch.cat((box_xyxy_to_cxcywh(boxes[:,:4]),box_xyxy_to_cxcywh(boxes[:,4:])),axis=1)
                boxes = boxes / torch.tensor([w, h, w, h, w, h, w, h], dtype=torch.float32)
                target["boxes"] = boxes

                if 'boxes_orig' in target:
                    target['boxes_orig'] = boxes.clone()

                assert target['boxes'].shape[0] == (target['boxes'][:,2] > 0).sum() == (target['boxes'][:,3] > 0).sum()

        return image, target


class Compose:
    def __init__(self, transforms):
        self.transforms = transforms

    def __call__(self, image, target=None):
        for t in self.transforms:
            image, target = t(image, target)
        return image, target

    def __repr__(self):
        format_string = self.__class__.__name__ + "("
        for t in self.transforms:
            format_string += "\n"
            format_string += "    {0}".format(t)
        format_string += "\n)"
        return format_string
