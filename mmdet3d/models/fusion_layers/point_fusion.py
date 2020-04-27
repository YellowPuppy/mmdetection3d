import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import xavier_init

from mmdet.ops import ConvModule
from ..registry import FUSION_LAYERS


def point_sample(
    img_features,
    points,
    lidar2img_rt,
    pcd_rotate_mat,
    img_scale_factor,
    img_crop_offset,
    pcd_trans_factor,
    pcd_scale_factor,
    pcd_flip,
    img_flip,
    img_pad_shape,
    img_shape,
    aligned=True,
    padding_mode='zeros',
    align_corners=True,
):
    """sample image features using point coordinates

    Arguments:
        img_features (Tensor): 1xCxHxW image features
        points (Tensor): Nx3 point cloud coordinates
        P (Tensor): 4x4 transformation matrix
        scale_factor (Tensor): scale_factor of images
        img_pad_shape (int, int): int tuple indicates the h & w after padding,
            this is necessary to obtain features in feature map
        img_shape (int, int): int tuple indicates the h & w before padding
            after scaling, this is necessary for flipping coordinates
    return:
        (Tensor): NxC image features sampled by point coordinates
    """
    # aug order: flip -> trans -> scale -> rot
    # The transformation follows the augmentation order in data pipeline
    if pcd_flip:
        # if the points are flipped, flip them back first
        points[:, 1] = -points[:, 1]

    points -= pcd_trans_factor
    # the points should be scaled to the original scale in velo coordinate
    points /= pcd_scale_factor
    # the points should be rotated back
    # pcd_rotate_mat @ pcd_rotate_mat.inverse() is not exactly an identity
    # matrix, use angle to create the inverse rot matrix neither.
    points = points @ pcd_rotate_mat.inverse()

    # project points from velo coordinate to camera coordinate
    num_points = points.shape[0]
    pts_4d = torch.cat([points, points.new_ones(size=(num_points, 1))], dim=-1)
    pts_2d = pts_4d @ lidar2img_rt.t()

    # cam_points is Tensor of Nx4 whose last column is 1
    # transform camera coordinate to image coordinate

    pts_2d[:, 2] = torch.clamp(pts_2d[:, 2], min=1e-5)
    pts_2d[:, 0] /= pts_2d[:, 2]
    pts_2d[:, 1] /= pts_2d[:, 2]

    # img transformation: scale -> crop -> flip
    # the image is resized by img_scale_factor
    img_coors = pts_2d[:, 0:2] * img_scale_factor  # Nx2
    img_coors -= img_crop_offset

    # grid sample, the valid grid range should be in [-1,1]
    coor_x, coor_y = torch.split(img_coors, 1, dim=1)  # each is Nx1

    if img_flip:
        # by default we take it as horizontal flip
        # use img_shape before padding for flip
        orig_h, orig_w = img_shape
        coor_x = orig_w - coor_x

    h, w = img_pad_shape
    coor_y = coor_y / h * 2 - 1
    coor_x = coor_x / w * 2 - 1
    grid = torch.cat([coor_x, coor_y],
                     dim=1).unsqueeze(0).unsqueeze(0)  # Nx2 -> 1x1xNx2

    # align_corner=True provides higher performance
    mode = 'bilinear' if aligned else 'nearest'
    point_features = F.grid_sample(
        img_features,
        grid,
        mode=mode,
        padding_mode=padding_mode,
        align_corners=align_corners)  # 1xCx1xN feats

    return point_features.squeeze().t()


@FUSION_LAYERS.register_module
class PointFusion(nn.Module):
    """Fuse image features from fused single scale features
    """

    def __init__(self,
                 img_channels,
                 pts_channels,
                 mid_channels,
                 out_channels,
                 img_levels=3,
                 conv_cfg=None,
                 norm_cfg=None,
                 act_cfg=None,
                 activate_out=True,
                 fuse_out=False,
                 dropout_ratio=0,
                 aligned=True,
                 align_corners=True,
                 padding_mode='zeros',
                 lateral_conv=True):
        super(PointFusion, self).__init__()
        if isinstance(img_levels, int):
            img_levels = [img_levels]
        if isinstance(img_channels, int):
            img_channels = [img_channels] * len(img_levels)
        assert isinstance(img_levels, list)
        assert isinstance(img_channels, list)
        assert len(img_channels) == len(img_levels)

        self.img_levels = img_levels
        self.act_cfg = act_cfg
        self.activate_out = activate_out
        self.fuse_out = fuse_out
        self.dropout_ratio = dropout_ratio
        self.img_channels = img_channels
        self.aligned = aligned
        self.align_corners = align_corners
        self.padding_mode = padding_mode

        self.lateral_convs = None
        if lateral_conv:
            self.lateral_convs = nn.ModuleList()
            for i in range(len(img_channels)):
                l_conv = ConvModule(
                    img_channels[i],
                    mid_channels,
                    3,
                    padding=1,
                    conv_cfg=conv_cfg,
                    norm_cfg=norm_cfg,
                    act_cfg=self.act_cfg,
                    inplace=False)
                self.lateral_convs.append(l_conv)
            self.img_transform = nn.Sequential(
                nn.Linear(mid_channels * len(img_channels), out_channels),
                nn.BatchNorm1d(out_channels, eps=1e-3, momentum=0.01),
            )
        else:
            self.img_transform = nn.Sequential(
                nn.Linear(sum(img_channels), out_channels),
                nn.BatchNorm1d(out_channels, eps=1e-3, momentum=0.01),
            )
        self.pts_transform = nn.Sequential(
            nn.Linear(pts_channels, out_channels),
            nn.BatchNorm1d(out_channels, eps=1e-3, momentum=0.01),
        )

        if self.fuse_out:
            self.fuse_conv = nn.Sequential(
                nn.Linear(mid_channels, out_channels),
                # For pts the BN is initialized differently by default
                # TODO: check whether this is necessary
                nn.BatchNorm1d(out_channels, eps=1e-3, momentum=0.01),
                nn.ReLU(inplace=False))

        self.init_weights()

    # default init_weights for conv(msra) and norm in ConvModule
    def init_weights(self):
        for m in self.modules():
            if isinstance(m, (nn.Conv2d, nn.Linear)):
                xavier_init(m, distribution='uniform')

    def forward(self, img_feats, pts, pts_feats, img_meta):
        """
        img_feats (List[Tensor]): img features
        pts: [List[Tensor]]: a batch of points with shape Nx3
        pts_feats (Tensor): a tensor consist of point features of the
            total batch

        """
        img_pts = self.obtain_mlvl_feats(img_feats, pts, img_meta)
        img_pre_fuse = self.img_transform(img_pts)
        if self.training and self.dropout_ratio > 0:
            img_pre_fuse = F.dropout(img_pre_fuse, self.dropout_ratio)
        pts_pre_fuse = self.pts_transform(pts_feats)

        fuse_out = img_pre_fuse + pts_pre_fuse
        if self.activate_out:
            fuse_out = F.relu(fuse_out)
        if self.fuse_out:
            fuse_out = self.fuse_conv(fuse_out)

        return fuse_out

    def obtain_mlvl_feats(self, img_feats, pts, img_meta):
        if self.lateral_convs is not None:
            img_ins = [
                lateral_conv(img_feats[i])
                for i, lateral_conv in zip(self.img_levels, self.lateral_convs)
            ]
        else:
            img_ins = img_feats
        img_feats_per_point = []
        # Sample multi-level features
        for i in range(len(img_meta)):
            mlvl_img_feats = []
            for level in range(len(self.img_levels)):
                if torch.isnan(img_ins[level][i:i + 1]).any():
                    import pdb
                    pdb.set_trace()
                mlvl_img_feats.append(
                    self.sample_single(img_ins[level][i:i + 1], pts[i][:, :3],
                                       img_meta[i]))
            mlvl_img_feats = torch.cat(mlvl_img_feats, dim=-1)
            img_feats_per_point.append(mlvl_img_feats)

        img_pts = torch.cat(img_feats_per_point, dim=0)
        return img_pts

    def sample_single(self, img_feats, pts, img_meta):
        pcd_scale_factor = (
            img_meta['pcd_scale_factor']
            if 'pcd_scale_factor' in img_meta.keys() else 1)
        pcd_trans_factor = (
            pts.new_tensor(img_meta['pcd_trans'])
            if 'pcd_trans' in img_meta.keys() else 0)
        pcd_rotate_mat = (
            pts.new_tensor(img_meta['pcd_rotation']) if 'pcd_rotation'
            in img_meta.keys() else torch.eye(3).type_as(pts).to(pts.device))
        img_scale_factor = (
            img_meta['scale_factor']
            if 'scale_factor' in img_meta.keys() else 1)
        pcd_flip = img_meta['pcd_flip'] if 'pcd_flip' in img_meta.keys(
        ) else False
        img_flip = img_meta['flip'] if 'flip' in img_meta.keys() else False
        img_crop_offset = (
            pts.new_tensor(img_meta['img_crop_offset'])
            if 'img_crop_offset' in img_meta.keys() else 0)
        img_pts = point_sample(
            img_feats,
            pts,
            pts.new_tensor(img_meta['lidar2img']),
            pcd_rotate_mat,
            img_scale_factor,
            img_crop_offset,
            pcd_trans_factor,
            pcd_scale_factor,
            pcd_flip=pcd_flip,
            img_flip=img_flip,
            img_pad_shape=img_meta['pad_shape'][:2],
            img_shape=img_meta['img_shape'][:2],
            aligned=self.aligned,
            padding_mode=self.padding_mode,
            align_corners=self.align_corners,
        )
        return img_pts