import torch


def transform_lidar_to_cam(boxes_lidar):
    """
    Only transform format, not exactly in camera coords
    :param boxes_lidar: (N, 3 or 7) [x, y, z, w, l, h, ry] in LiDAR coords
    :return: boxes_cam: (N, 3 or 7) [x, y, z, h, w, l, ry] in camera coords
    """
    # boxes_cam = boxes_lidar.new_tensor(boxes_lidar.data)
    boxes_cam = boxes_lidar.clone().detach()
    boxes_cam[:, 0] = -boxes_lidar[:, 1]
    boxes_cam[:, 1] = -boxes_lidar[:, 2]
    boxes_cam[:, 2] = boxes_lidar[:, 0]
    if boxes_cam.shape[1] > 3:
        boxes_cam[:, [3, 4, 5]] = boxes_lidar[:, [5, 3, 4]]
    return boxes_cam


def boxes3d_to_bev_torch(boxes3d):
    """
    :param boxes3d: (N, 7) [x, y, z, h, w, l, ry] in camera coords
    :return:
        boxes_bev: (N, 5) [x1, y1, x2, y2, ry]
    """
    boxes_bev = boxes3d.new(torch.Size((boxes3d.shape[0], 5)))

    cu, cv = boxes3d[:, 0], boxes3d[:, 2]
    half_l, half_w = boxes3d[:, 5] / 2, boxes3d[:, 4] / 2
    boxes_bev[:, 0], boxes_bev[:, 1] = cu - half_l, cv - half_w
    boxes_bev[:, 2], boxes_bev[:, 3] = cu + half_l, cv + half_w
    boxes_bev[:, 4] = boxes3d[:, 6]
    return boxes_bev


def boxes3d_to_bev_torch_lidar(boxes3d):
    """
    :param boxes3d: (N, 7) [x, y, z, w, l, h, ry] in LiDAR coords
    :return:
        boxes_bev: (N, 5) [x1, y1, x2, y2, ry]
    """
    boxes_bev = boxes3d.new(torch.Size((boxes3d.shape[0], 5)))

    cu, cv = boxes3d[:, 0], boxes3d[:, 1]
    half_l, half_w = boxes3d[:, 4] / 2, boxes3d[:, 3] / 2
    boxes_bev[:, 0], boxes_bev[:, 1] = cu - half_w, cv - half_l
    boxes_bev[:, 2], boxes_bev[:, 3] = cu + half_w, cv + half_l
    boxes_bev[:, 4] = boxes3d[:, 6]
    return boxes_bev