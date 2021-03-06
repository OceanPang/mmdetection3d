import numpy as np
import torch

from mmdet3d.ops import points_in_boxes_batch
from .base_box3d import BaseInstance3DBoxes
from .utils import limit_period, rotation_3d_in_axis


class DepthInstance3DBoxes(BaseInstance3DBoxes):
    """3D boxes of instances in Depth coordinates.

    Coordinates in Depth:

    .. code-block:: none

                    up z    y front (yaw=0.5*pi)
                       ^   ^
                       |  /
                       | /
                       0 ------> x right (yaw=0)

    The relative coordinate of bottom center in a Depth box is (0.5, 0.5, 0),
    and the yaw is around the z axis, thus the rotation axis=2.
    The yaw is 0 at the positive direction of x axis, and increases from
    the positive direction of x to the positive direction of y.

    Args:
        tensor (torch.Tensor): Float matrix of N x box_dim.
        box_dim (int): Integer indicates the dimension of a box
            Each row is (x, y, z, x_size, y_size, z_size, yaw, ...).
        with_yaw (bool): If True, the value of yaw will be set to 0 as minmax
            boxes.
    """

    @property
    def gravity_center(self):
        """Calculate the gravity center of all the boxes.

        Returns:
            torch.Tensor: A tensor with center of each box.
        """
        bottom_center = self.bottom_center
        gravity_center = torch.zeros_like(bottom_center)
        gravity_center[:, :2] = bottom_center[:, :2]
        gravity_center[:, 2] = bottom_center[:, 2] + self.tensor[:, 5] * 0.5
        return gravity_center

    @property
    def corners(self):
        """Calculate the coordinates of corners of all the boxes.

        Convert the boxes to corners in clockwise order, in form of
        (x0y0z0, x0y0z1, x0y1z1, x0y1z0, x1y0z0, x1y0z1, x1y1z1, x1y1z0)

        .. code-block:: none

                                           up z
                            front y           ^
                                 /            |
                                /             |
                  (x0, y1, z1) + -----------  + (x1, y1, z1)
                              /|            / |
                             / |           /  |
               (x0, y0, z1) + ----------- +   + (x1, y1, z0)
                            |  /      .   |  /
                            | / oriign    | /
               (x0, y0, z0) + ----------- + --------> right x
                                          (x1, y0, z0)

        Returns:
            torch.Tensor: Corners of each box with size (N, 8, 3).
        """
        # TODO: rotation_3d_in_axis function do not support
        #  empty tensor currently.
        assert len(self.tensor) != 0
        dims = self.dims
        corners_norm = torch.from_numpy(
            np.stack(np.unravel_index(np.arange(8), [2] * 3), axis=1)).to(
                device=dims.device, dtype=dims.dtype)

        corners_norm = corners_norm[[0, 1, 3, 2, 4, 5, 7, 6]]
        # use relative origin (0.5, 0.5, 0)
        corners_norm = corners_norm - dims.new_tensor([0.5, 0.5, 0])
        corners = dims.view([-1, 1, 3]) * corners_norm.reshape([1, 8, 3])

        # rotate around z axis
        corners = rotation_3d_in_axis(corners, self.tensor[:, 6], axis=2)
        corners += self.tensor[:, :3].view(-1, 1, 3)
        return corners

    @property
    def bev(self):
        """Calculate the 2D bounding boxes in BEV with rotation.

        Returns:
            torch.Tensor: A n x 5 tensor of 2D BEV box of each box. \
                The box is in XYWHR format.
        """
        return self.tensor[:, [0, 1, 3, 4, 6]]

    @property
    def nearest_bev(self):
        """Calculate the 2D bounding boxes in BEV without rotation.

        Returns:
            torch.Tensor: A tensor of 2D BEV box of each box.
        """
        # Obtain BEV boxes with rotation in XYWHR format
        bev_rotated_boxes = self.bev
        # convert the rotation to a valid range
        rotations = bev_rotated_boxes[:, -1]
        normed_rotations = torch.abs(limit_period(rotations, 0.5, np.pi))

        # find the center of boxes
        conditions = (normed_rotations > np.pi / 4)[..., None]
        bboxes_xywh = torch.where(conditions, bev_rotated_boxes[:,
                                                                [0, 1, 3, 2]],
                                  bev_rotated_boxes[:, :4])

        centers = bboxes_xywh[:, :2]
        dims = bboxes_xywh[:, 2:]
        bev_boxes = torch.cat([centers - dims / 2, centers + dims / 2], dim=-1)
        return bev_boxes

    def rotate(self, angle, points=None):
        """Rotate boxes with points (optional) with the given angle.

        Args:
            angle (float, torch.Tensor): Rotation angle.
            points (torch.Tensor, numpy.ndarray, optional): Points to rotate.
                Defaults to None.

        Returns:
            tuple or None: When ``points`` is None, the function returns \
                None, otherwise it returns the rotated points and the \
                rotation matrix ``rot_mat_T``.
        """
        if not isinstance(angle, torch.Tensor):
            angle = self.tensor.new_tensor(angle)
        rot_sin = torch.sin(angle)
        rot_cos = torch.cos(angle)
        rot_mat_T = self.tensor.new_tensor([[rot_cos, -rot_sin, 0],
                                            [rot_sin, rot_cos, 0], [0, 0,
                                                                    1]]).T
        self.tensor[:, 0:3] = self.tensor[:, 0:3] @ rot_mat_T
        if self.with_yaw:
            self.tensor[:, 6] -= angle
        else:
            corners_rot = self.corners @ rot_mat_T
            new_x_size = corners_rot[..., 0].max(
                dim=1, keepdim=True)[0] - corners_rot[..., 0].min(
                    dim=1, keepdim=True)[0]
            new_y_size = corners_rot[..., 1].max(
                dim=1, keepdim=True)[0] - corners_rot[..., 1].min(
                    dim=1, keepdim=True)[0]
            self.tensor[:, 3:5] = torch.cat((new_x_size, new_y_size), dim=-1)

        if points is not None:
            if isinstance(points, torch.Tensor):
                points[:, :3] = points[:, :3] @ rot_mat_T
            elif isinstance(points, np.ndarray):
                rot_mat_T = rot_mat_T.numpy()
                points[:, :3] = np.dot(points[:, :3], rot_mat_T)
            else:
                raise ValueError
            return points, rot_mat_T

    def flip(self, bev_direction='horizontal', points=None):
        """Flip the boxes in BEV along given BEV direction.

        In Depth coordinates, it flips x (horizontal) or y (vertical) axis.

        Args:
            bev_direction (str): Flip direction (horizontal or vertical).
            points (torch.Tensor, numpy.ndarray, None): Points to flip.
                Defaults to None.

        Returns:
            torch.Tensor, numpy.ndarray or None: Flipped points.
        """
        assert bev_direction in ('horizontal', 'vertical')
        if bev_direction == 'horizontal':
            self.tensor[:, 0::7] = -self.tensor[:, 0::7]
            if self.with_yaw:
                self.tensor[:, 6] = -self.tensor[:, 6] + np.pi
        elif bev_direction == 'vertical':
            self.tensor[:, 1::7] = -self.tensor[:, 1::7]
            if self.with_yaw:
                self.tensor[:, 6] = -self.tensor[:, 6]

        if points is not None:
            assert isinstance(points, (torch.Tensor, np.ndarray))
            if bev_direction == 'horizontal':
                points[:, 0] = -points[:, 0]
            elif bev_direction == 'vertical':
                points[:, 1] = -points[:, 1]
            return points

    def in_range_bev(self, box_range):
        """Check whether the boxes are in the given range.

        Args:
            box_range (list | torch.Tensor): The range of box
                (x_min, y_min, x_max, y_max).

        Note:
            In the original implementation of SECOND, checking whether
            a box in the range checks whether the points are in a convex
            polygon, we try to reduce the burdun for simpler cases.

        Returns:
            torch.Tensor: Indicating whether each box is inside \
                the reference range.
        """
        in_range_flags = ((self.tensor[:, 0] > box_range[0])
                          & (self.tensor[:, 1] > box_range[1])
                          & (self.tensor[:, 0] < box_range[2])
                          & (self.tensor[:, 1] < box_range[3]))
        return in_range_flags

    def convert_to(self, dst, rt_mat=None):
        """Convert self to `dst` mode.

        Args:
            dst (:obj:`BoxMode`): The target Box mode.
            rt_mat (np.ndarray | torch.Tensor): The rotation and translation
                matrix between different coordinates. Defaults to None.
                The conversion from `src` coordinates to `dst` coordinates
                usually comes along the change of sensors, e.g., from camera
                to LiDAR. This requires a transformation matrix.

        Returns:
            :obj:`BaseInstance3DBoxes`: \
                The converted box of the same type in the `dst` mode.
        """
        from .box_3d_mode import Box3DMode
        return Box3DMode.convert(
            box=self, src=Box3DMode.DEPTH, dst=dst, rt_mat=rt_mat)

    def points_in_boxes(self, points):
        """Find points that are in boxes (CUDA).

        Args:
            points (torch.Tensor): Points in shape [1, M, 3] or [M, 3], \
                3 dimensions are [x, y, z] in LiDAR coordinate.

        Returns:
            torch.Tensor: The index of boxes each point lies in with shape \
                of (B, M, T).
        """
        from .box_3d_mode import Box3DMode

        # to lidar
        points_lidar = points.clone()
        points_lidar = points_lidar[..., [1, 0, 2]]
        points_lidar[..., 1] *= -1
        if points.dim() == 2:
            points_lidar = points_lidar.unsqueeze(0)
        else:
            assert points.dim() == 3 and points_lidar.shape[0] == 1

        boxes_lidar = self.convert_to(Box3DMode.LIDAR).tensor
        boxes_lidar = boxes_lidar.to(points.device).unsqueeze(0)
        box_idxs_of_pts = points_in_boxes_batch(points_lidar, boxes_lidar)

        return box_idxs_of_pts.squeeze(0)
