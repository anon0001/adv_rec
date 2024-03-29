#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
from pathlib import Path

import numpy as np
import os
from PIL import Image

import torch
import torch.nn.functional as F
from torch.autograd import Variable
import torch.utils.data as data

from torchvision.models import resnet50
from torchvision import transforms

# This script uses the PyTorch's pre-trained ResNet-50 CNN to extract
#   res4f_relu convolutional features of size 1024x14x14
#   avgpool features of size 2048D
# We reproduced ImageNet val set Top1/Top5 accuracy of 76.1/92.8 %
# as reported in the following web page before extracting the features:
#   http://pytorch.org/docs/master/torchvision/models.html
#
# We save the final files as 16-bit floating point tensors to reduce
# the size by 2x. We confirmed that this does not affect the above accuracy.
#
# Organization of the image folder:
#  In order to extract features from an arbitrary set of images,
#  you need to create a folder with a file called `index.txt` in it that
#  lists the filenames of the raw images in an ordered way.
#    -f /path/to/images/train  --> train folder contains 29K images
#                                  and an index.txt with 29K lines.
#


class ImageFolderDataset(data.Dataset):
    """A variant of torchvision.datasets.ImageFolder which drops support for
    target loading, i.e. this only loads images not attached to any other
    label.

    Arguments:
        root (str): The root folder which contains a folder per each split.
        split (str): A subfolder that should exist under ``root`` containing
            images for a specific split.
        resize (int, optional): An optional integer to be given to
            ``torchvision.transforms.Resize``. Default: ``None``.
        crop (int, optional): An optional integer to be given to
            ``torchvision.transforms.CenterCrop``. Default: ``None``.
    """
    def __init__(self, index, folder, resize=None, crop=None):
        self.root = folder

        # Image list in dataset order
        self.index = index

        _transforms = []
        if resize is not None:
            _transforms.append(transforms.Resize(resize))
        if crop is not None:
            _transforms.append(transforms.CenterCrop(crop))
        _transforms.append(transforms.ToTensor())
        _transforms.append(
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225]))
        self.transform = transforms.Compose(_transforms)

        if not os.path.exists(self.root):
            raise(RuntimeError(
                "index.txt does not exist in {}".format(self.root)))

        self.image_files = []
        with open(self.index) as f:
            for fname in f:
                fname = os.path.join(self.root,fname.strip())
                assert os.path.exists(fname), "{} does not exist.".format(fname)
                self.image_files.append(str(fname))

    def read_image(self, fname):
        with open(fname, 'rb') as f:
            img = Image.open(f).convert('RGB')
            return self.transform(img)

    def __getitem__(self, idx):
        return self.read_image(self.image_files[idx])

    def __len__(self):
        return len(self.image_files)


def resnet_forward(cnn, x):
    x = cnn.conv1(x)
    x = cnn.bn1(x)
    x = cnn.relu(x)
    x = cnn.maxpool(x)

    x = cnn.layer1(x)
    x = cnn.layer2(x)
    return x


def res4f_relu(cnn, x):
    return cnn.layer3(resnet_forward(cnn, x))


def res5c_relu(cnn, x):
    return cnn.layer4(cnn.layer3(resnet_forward(cnn, x)))


def avgpool(cnn, x):
    avgp = cnn.avgpool(cnn.layer4(cnn.layer3(resnet_forward(cnn, x))))
    return avgp.view(avgp.size(0), -1)


# python bin/extract.py -i /media/jb/DATA/sub/nmtpytorch/data/image_splits/val/val.txt  \
#                   -f /media/jb/DATA/sub/nmtpytorch/data/image_splits/raw/flickr30k_images/ \
#                   -s val

# python bin/extract.py -i /media/jb/DATA/sub/nmtpytorch/data/image_splits/test_2017_mscoco/test_2017_mscoco.txt  \
#                   -f /media/jb/DATA/sub/nmtpytorch/data/image_splits/raw/ambiguous_coco \
#                   -s coco_ambiguous


if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='extract-cnn-features')
    parser.add_argument('-i', '--index', type=str, required=True,
                        help='Folder to image files i.e. /images/coco/index.txt')
    parser.add_argument('-f', '--folder', type=str, required=True,
                        help='Folder to image files i.e. /images/coco')
    parser.add_argument('-s', '--split', type=str, required=True,
                        help='train')
    parser.add_argument('-b', '--batch-size', type=int, default=256,
                        help='Batch size for forward pass.')
    parser.add_argument('-c', '--central-fraction', type=float, default=1.0,
                        help='Central fraction. If < 1, focuses on the middle.')
    parser.add_argument('-w', '--width', type=int, default=224,
                        help='Final image width and height.')
    parser.add_argument('-n', '--l2norm', action='store_true',
                        help='Apply l2 normalization.')
    parser.add_argument('-l', '--layer', default='res4f_relu',
                        help='res4f_relu/res5c_relu/avgpool')

    # Parse arguments
    args = parser.parse_args()


    bs = args.batch_size

    resize_width = int(args.width / args.central_fraction)
    print('Resize shortest side to {} then center crop {}x{}'.format(
        resize_width, args.width, args.width))

    # Create dataset
    dataset = ImageFolderDataset(
        args.index, args.folder, resize=resize_width, crop=args.width)
    print('Root folder: {} (split: {}) ({} images)'.format(
        args.index, args.folder, len(dataset)))
    n_batches = int(np.ceil(len(dataset) / bs))

    loader = data.DataLoader(dataset, batch_size=args.batch_size)

    print('Creating CNN instance.')
    cnn = resnet50(pretrained=True)

    # Remove final classifier layer
    del cnn.fc

    cnn.train(False)

    x = Variable(torch.zeros(1, 3, args.width, args.width), volatile=True)

    # Create placeholders
    if args.layer == 'avgpool':
        feats = np.zeros((len(dataset), 2048), dtype='float32')
        extractor = avgpool
    elif args.layer == 'res4f_relu':
        extractor = res4f_relu
        w = extractor(cnn, x).shape[-1]
        print('Output spatial dimensions: {}x{}'.format(w, w))
        feats = np.zeros((len(dataset), 1024, w, w), dtype='float32')
    elif args.layer == 'res5c_relu':
        extractor = res5c_relu
        w = extractor(cnn, x).shape[-1]
        print('Output spatial dimensions: {}x{}'.format(w, w))
        feats = np.zeros((len(dataset), 2048, w, w), dtype='float32')

    cnn.cuda()

    for bidx, batch in enumerate(loader):
        x = Variable(batch, volatile=True).cuda()
        out = extractor(cnn, x)

        if args.l2norm:
            if out.dim() == 2:
                out = F.normalize(out, dim=-1)
            else:
                n, c, h, w = out.shape
                out = F.normalize(out.view(n, c, -1), dim=1).view(n, c, h, w)

            feats[bidx * bs: (bidx + 1) * bs] = out.data.cpu()

        print('{:3}/{:3} batches completed.'.format(bidx + 1, n_batches), end='\r')

    # Save the file
    output = "{}-{}-{}-r{}-c{}".format(
        args.split, 'resnet50', args.layer, resize_width, args.width)
    if args.l2norm:
        output += '-l2norm'
    np.save(output, feats.astype('float16'))