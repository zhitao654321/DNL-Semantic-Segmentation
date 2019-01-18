#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Author: Donny You(youansheng@gmail.com)
# Priorbox layer for Detection.


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch

from extensions.nms.nms_wrapper import nms
from utils.layers.det.fr_priorbox_layer import FRPriorBoxLayer


class FRROIGenerator(object):
    # unNOTE: I'll make it undifferential
    # unTODO: make sure it's ok
    # It's ok
    """Proposal regions are generated by calling this object.
    The :meth:`__call__` of this object outputs object detection proposals by
    applying estimated bounding box offsets
    to a set of anchors.
    This class takes parameters to control number of bounding boxes to
    pass to NMS and keep after NMS.
    If the paramters are negative, it uses all the bounding boxes supplied
    or keep all the bounding boxes returned by NMS.
    This class is used for Region Proposal Networks introduced in
    Faster R-CNN [#]_.
    .. [#] Shaoqing Ren, Kaiming He, Ross Girshick, Jian Sun. \
    Faster R-CNN: Towards Real-Time Object Detection with \
    Region Proposal Networks. NIPS 2015.
    Args:
        nms_thresh (float): Threshold value used when calling NMS.
        n_train_pre_nms (int): Number of top scored bounding boxes
            to keep before passing to NMS in train mode.
        n_train_post_nms (int): Number of top scored bounding boxes
            to keep after passing to NMS in train mode.
        n_test_pre_nms (int): Number of top scored bounding boxes
            to keep before passing to NMS in test mode.
        n_test_post_nms (int): Number of top scored bounding boxes
            to keep after passing to NMS in test mode.
        force_cpu_nms (bool): If this is :obj:`True`,
            always use NMS in CPU mode. If :obj:`False`,
            the NMS mode is selected based on the type of inputs.
        min_size (int): A paramter to determine the threshold on
            discarding bounding boxes based on their sizes.
    """

    def __init__(self, configer):
        self.configer = configer
        self.fr_priorbox_layer = FRPriorBoxLayer(self.configer)

    def __call__(self, feat_list, loc, score, n_pre_nms, n_post_nms, meta):
        """input should  be ndarray
        Propose RoIs.
        Inputs :obj:`loc, score, anchor` refer to the same anchor when indexed
        by the same index.
        On notations, :math:`R` is the total number of anchors. This is equal
        to product of the height and the width of an image and the number of
        anchor bases per pixel.
        Type of the output is same as the inputs.
        Args:
            loc : Predicted offsets and scaling to anchors.
                Its shape is :math:`(R, 4)`.
            score (array): Predicted foreground probability for anchors.
                Its shape is :math:`(R,)`.
            anchor (array): Coordinates of anchors. Its shape is
                :math:`(R, 4)`.
            img_size (tuple of ints): A tuple :obj:`height, width`,
                which contains image size after scaling.
            scale (float): The scaling factor used to scale an image after
                reading it from a file.
        Returns:
            array:
            An array of coordinates of proposal boxes.
            Its shape is :math:`(S, 4)`. :math:`S` is less than
            :obj:`self.n_test_post_nms` in test time and less than
            :obj:`self.n_train_post_nms` in train time. :math:`S` depends on
            the size of the predicted bounding boxes and the number of
            bounding boxes discarded by NMS.
        """
        # NOTE: when test, remember
        # faster_rcnn.eval()
        # to set self.traing = False
        device = loc.device

        anchors = self.fr_priorbox_layer(feat_list, meta[0]['input_size'])
        default_boxes = anchors.unsqueeze(0).repeat(loc.size(0), 1, 1).to(device)

        # loc = loc[:, :, [1, 0, 3, 2]]
        # Convert anchors into proposal via bbox transformations.
        wh = torch.exp(loc[:, :, 2:]) * default_boxes[:, :, 2:]
        cxcy = loc[:, :, :2] * default_boxes[:, :, 2:] + default_boxes[:, :, :2]
        dst_bbox = torch.cat([cxcy - wh / 2, cxcy + wh / 2], 2)  # [b, 8732,4]
        dst_bbox = dst_bbox.detach()
        score = score.detach()
        # cls_prob = F.softmax(score, dim=-1)
        rpn_fg_scores = score[:, :, 1]

        rois_list = list()
        roi_indices_list = list()
        batch_rois_num = torch.zeros((loc.size(0),))

        for i in range(loc.size(0)):
            tmp_dst_bbox = dst_bbox[i]
            tmp_dst_bbox[:, 0::2] = tmp_dst_bbox[:, 0::2].clamp_(min=0, max=meta[i]['border_size'][0] - 1)
            tmp_dst_bbox[:, 1::2] = tmp_dst_bbox[:, 1::2].clamp_(min=0, max=meta[i]['border_size'][1] - 1)
            tmp_scores = rpn_fg_scores[i]
            # Remove predicted boxes with either height or width < threshold.
            ws = tmp_dst_bbox[:, 2] - tmp_dst_bbox[:, 0] + 1
            hs = tmp_dst_bbox[:, 3] - tmp_dst_bbox[:, 1] + 1
            min_size = self.configer.get('rpn', 'min_size')
            keep = (hs >= meta[i]['img_scale'] * min_size) & (ws >= meta[i]['img_scale'] * min_size)
            rois = tmp_dst_bbox[keep]
            tmp_scores = tmp_scores[keep]
            # Sort all (proposal, score) pairs by score from highest to lowest.
            # Take top pre_nms_topN (e.g. 6000).
            if rois.numel() == 0:
                rois_list.append(rois)
                roi_indices_list.append(rois)
                batch_rois_num[i] = rois.numel()
                continue

            _, order = tmp_scores.sort(0, descending=True)
            if n_pre_nms > 0:
                order = order[:n_pre_nms]

            rois = rois[order]
            tmp_scores = tmp_scores[order]

            # Apply nms (e.g. threshold = 0.7).
            # Take after_nms_topN (e.g. 300).

            # unNOTE: somthing is wrong here!
            # TODO: remove cuda.to_gpu
            keep = nms(torch.cat((rois, tmp_scores.unsqueeze(1)), 1),
                       thresh=self.configer.get('rpn', 'nms_threshold'))
            # keep = DetHelper.nms(rois,
            #                      scores=tmp_scores,
            #                      nms_threshold=self.configer.get('rpn', 'nms_threshold'))
            if n_post_nms > 0:
                keep = keep[:n_post_nms]

            rois = rois[keep]

            batch_index = i * torch.ones((len(rois),))
            rois_list.append(rois)
            roi_indices_list.append(batch_index)
            batch_rois_num[i] = len(rois)

        rois = torch.cat(rois_list, 0)
        roi_indices = torch.cat(roi_indices_list, 0)

        if rois.numel() == 0:
            indices_and_rois = rois
        else:
            indices_and_rois = torch.cat([roi_indices.unsqueeze(1).to(device), rois.to(device)], dim=1).contiguous()

        return indices_and_rois.to(device), batch_rois_num.long().to(device)