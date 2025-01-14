from typing import List

import torch
from clearml import Logger
from monai import metrics
from torch.nn import functional as F
from torch.nn.modules.loss import _Loss
from torch.optim import Optimizer
from torch.utils.data import DataLoader

from models.uv_unet import UVUNet
from models.kpts_unet import KeypointUNet, KeypointSegUNet
from utils import convert_list_of_uv_to_coordinates, extract_kpts_from_heatmap
from kornia.augmentation import AugmentationSequential
from kornia.geometry.conversions import normalize_pixel_coordinates
from dataset.jsrt_dataset import JSRTDataset


# torch.autograd.set_detect_anomaly(True)


def landmark_uv_loss(uv: torch.Tensor, landmarks: torch.Tensor, landmark_uv_values: List[torch.Tensor],
                     loss_fn: _Loss) -> torch.Tensor:
    """
    Calculate the difference between the predicted uv values and the ground truth uv values at the given landmarks.
    :param uv: predicted uv maps (B, C, UV, H, W)
    :param landmarks: unnormalized landmarks in coordinates (B, N, 2)
    :param landmark_uv_values: ground truth uv values for each landmark in each class C. List of (B, N_c, 2)
    :param loss_fn: metric to calculate the difference between the predicted and ground truth uv values
    :return: difference between the predicted and ground truth uv values at the given landmarks
    """
    assert loss_fn.reduction == 'none', 'loss_fn must have reduction set to none'
    assert not uv.isnan().any(), 'uv should not contain NaN values'

    B, C, _, H, W = uv.shape
    device = uv.device

    norm_lm = normalize_pixel_coordinates(landmarks, H, W)

    # get the index of the landmarks for each class
    N_c = [len(lm) for lm in landmark_uv_values]
    N_c.insert(0, 0)
    anatomy_idx = torch.tensor(N_c, device=device).cumsum(0)
    loss = torch.zeros(B, C, device=device)
    for c in range(C):
        start_idx, end_idx = anatomy_idx[c], anatomy_idx[c + 1]
        sample_point = norm_lm[:, start_idx:end_idx]  # (B, N_c, 2)
        outside_mask = (sample_point < -1) | (sample_point > 1)
        outside_mask = outside_mask.any(dim=-1)
        if outside_mask.all():
            continue

        uv_values_hat = F.grid_sample(uv[:, c], sample_point.view(B, 1, -1, 2),
                                      align_corners=True, mode='bilinear').squeeze(2).permute(0, 2, 1)  # (B, N_c, 2)
        uv_values = landmark_uv_values[c].unsqueeze(0).expand(B, -1, -1).clone()  # (B, N_c, 2)

        # zero out values outside the valid range
        uv_values[outside_mask] = 0
        uv_values_hat[outside_mask] = 0

        loss[:, c] = loss_fn(uv_values_hat, uv_values).mean([1, 2])

    return loss


# take a lot of VRAM and is unstable for training. Use landmark_uv_loss instead.
def landmark_regression_via_uv(uv: torch.Tensor,
                               landmarks: torch.Tensor,
                               landmark_uv_values: List[torch.Tensor],
                               mask: torch.Tensor, k: int) -> tuple:
    """
    Calculate the loss for landmark regression via uv maps.
    :param uv: predicted uv maps (B, C, 2, H, W)
    :param landmarks: landmarks in coordinates (B, N, 2)
    :param landmark_uv_values: list of uv values for all landmarks in each class C
    :param mask: segmentation mask used to mask uv values to valid segmentation area (B, C, H, W)
    :param k: number of nearest uv values to consider for coordinate interpolation from uv maps
    :return: L2 loss normalized in [0, 1] and in coordinates distance
    """
    B, C, _, H, W = uv.shape
    device = uv.device
    N_c = torch.tensor([len(lm) for lm in landmark_uv_values], device=device)
    assert mask.shape == (B, C, H, W), 'mask must have the same shape as uv'
    assert len(landmark_uv_values) == C, 'landmarks and landmark_uv_values must contain C elements'

    # cloning to maintain original values
    uv_with_nan = uv.clone()

    # mask uv maps to valid segmentation area
    mask = mask.unsqueeze(2).expand_as(uv_with_nan)
    uv_with_nan[mask.logical_not()] = torch.nan

    lm_hat = convert_list_of_uv_to_coordinates(uv_with_nan, landmark_uv_values, 'linear', k)  # list of (B, N_c, 2)
    lm_hat = torch.cat(lm_hat, dim=1)  # (B, N, 2)

    lm_diff = lm_hat - landmarks  # (B, N, 2)
    # normalize to [0, 1]
    lm_diff_norm = lm_diff / torch.tensor([W, H], device=device, dtype=lm_diff.dtype).view(1, 1, 2)

    lm_diff = torch.linalg.vector_norm(lm_diff, ord=2, dim=-1).mean()
    lm_diff_norm = torch.linalg.vector_norm(lm_diff_norm, ord=2, dim=-1).mean()

    return lm_diff_norm, lm_diff.detach()


def balanced_normalized_uv_loss(uv_hat: torch.Tensor, uv: torch.Tensor, loss_fn: _Loss) -> torch.Tensor:
    """
    Calculate the difference between the predicted uv values and the ground truth uv values. The final loss is then
    weighted by the number of valid pixels for each class.
    :param uv_hat: predicted uv maps (B, C, UV, H, W)
    :param uv: ground truth uv maps (B, C, UV, H, W)
    :param loss_fn: metric to calculate the difference between the predicted and ground truth uv values
    :return: weighted mean loss
    """
    assert loss_fn.reduction == 'none', 'loss_fn must have reduction set to none'
    assert not uv.isnan().all(), 'uv must contain some valid values'

    # (B, C, 2, H, W) -> (B, C, 2 * H * W)
    uv_hat_flat = uv_hat.flatten(start_dim=2)
    uv_flat = uv.flatten(start_dim=2)

    # zero out NaN values in ground truth to prevent NaN loss
    nan_gt_mask = uv_flat.isnan()
    uv_flat = torch.where(nan_gt_mask, 0, uv_flat)
    uv_hat_flat = torch.where(nan_gt_mask, 0, uv_hat_flat)

    # normalize each class with its number of valid pixels
    valid_pxl = nan_gt_mask.logical_not().sum(-1)  # (B, C)
    # 1 is added to avoid division by zero
    loss = loss_fn(uv_hat_flat, uv_flat).sum(-1) / (valid_pxl + 1)  # (B, C)

    return loss


def total_variation(uv: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """
    Calculate the total variation of the uv maps.
    :param uv: uv maps (B, C, UV, H, W)
    :param mask: segmentation mask used to mask uv values to valid segmentation area (B, C, H, W)
    :return: total variation of the uv maps
    """
    B, C, _, H, W = uv.shape
    assert mask.shape == (B, C, H, W), 'mask must have the same shape as uv'
    assert mask.dtype == torch.bool, 'mask must be of type bool'
    # calculate total variation
    tv = torch.stack(torch.gradient(uv, dim=(3, 4), edge_order=1), dim=-1)  # (B, C, UV, H, W, 2)
    tv = torch.linalg.vector_norm(tv, ord=2, dim=-1)  # (B, C, UV, H, W)
    tv = tv.mean(2)  # (B, C, H, W)

    # mask uv maps to valid segmentation area
    tv = torch.where(mask, tv, 0)
    tv = tv.pow(2)

    # average by the number of valid pixels (1 is added to avoid division by zero)
    tv = tv.sum(dim=[2, 3]) / (mask.sum(dim=[2, 3]) + 1)

    return tv


# used for monitoring the training process
@torch.no_grad()
def uv_l1_loss(uv_hat: torch.Tensor, uv: torch.Tensor) -> torch.Tensor:
    return balanced_normalized_uv_loss(uv_hat, uv, torch.nn.L1Loss(reduction='none'))


def forward(mode: str, data_loader: DataLoader, epoch: int,  # have to be given each call
            # can be provided via kwargs dict
            lambdas: list,  # [lambda_dsc, lambda_uv, lambda_tv]
            model: UVUNet, optimizer: Optimizer, device: torch.device, lm_uv_values: List[torch.Tensor],
            supervision: str, bce_pos_weight: torch.Tensor, uv_loss_fn, data_aug: AugmentationSequential = None) -> (
        torch.Tensor, torch.Tensor):
    assert any(lambdas), 'At least one weighting for loss must be non-zero'
    # set model mode according to mode
    if mode == 'train':
        model.train()
    elif mode in ['test', 'val']:
        model.eval()
    else:
        raise ValueError(f'Unknown mode: {mode}')

    lambda_bce, lambda_reg_uv, lambda_tv = lambdas
    dsc = metrics.DiceMetric(reduction='mean_batch', include_background=True, ignore_empty=True,
                             num_classes=data_loader.dataset.N_CLASSES)
    uv_l1 = metrics.LossMetric(uv_l1_loss, reduction='mean_batch')
    tv = metrics.LossMetric(total_variation, reduction='mean_batch')
    loss_collector = metrics.CumulativeAverage()

    for img, lm, _, seg, uv_map in data_loader:
        img = img.to(device, non_blocking=True)
        lm = lm.to(device, non_blocking=True)
        seg = seg.to(device, non_blocking=True)
        uv = uv_map.to(device, non_blocking=True)

        if data_aug and model.training:
            img, seg, uv0, uv1, lm = data_aug(img, seg, uv[:, :, 0], uv[:, :, 1], lm)
            uv = torch.stack([uv0, uv1], dim=2)
            uv = torch.where(seg.bool().unsqueeze(2).expand_as(uv), uv, torch.nan)

        with torch.set_grad_enabled(model.training):  # forward
            seg_hat, uv_hat = model(img)
            bce_loss = F.binary_cross_entropy_with_logits(seg_hat, seg, pos_weight=bce_pos_weight) if lambda_bce else 0
            reg_loss = balanced_normalized_uv_loss(uv_hat, uv, uv_loss_fn).mean() if lambda_reg_uv else 0
            lm_loss = landmark_uv_loss(uv_hat, lm, lm_uv_values, uv_loss_fn).mean() if lambda_reg_uv else 0
            tv_loss = total_variation(uv_hat, seg.bool()).mean() if lambda_tv else 0

            if supervision == 'dense':
                uv_loss = lambda_reg_uv * (reg_loss + lm_loss) / 2 + lambda_tv * tv_loss
            elif supervision == 'sparse':
                uv_loss = lambda_reg_uv * lm_loss + lambda_tv * tv_loss
            loss = lambda_bce * bce_loss + uv_loss

        if model.training:  # backward
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()

        # track metrics
        batch_size = len(img)
        loss_collector.append([loss, bce_loss, uv_loss, reg_loss, lm_loss], count=batch_size)
        dsc(seg_hat.sigmoid() > 0.5, seg)
        uv_l1(uv_hat, uv)
        tv(uv_hat, seg.bool())

    # log metrics scalars
    log = Logger.current_logger()
    loss_avg = loss_collector.aggregate()  # [loss, bce, uv, reg, lm]
    log.report_scalar('Loss', mode, iteration=epoch, value=loss_avg[0].item())
    if lambda_bce:
        log.report_scalar('BCE', mode, iteration=epoch, value=loss_avg[1].item())
        log.report_scalar('Dice', mode, iteration=epoch, value=dsc.aggregate().mean().item())
        log.report_histogram('Dice', mode, iteration=epoch,
                             values=dsc.aggregate().cpu().numpy(),
                             xlabels=data_loader.dataset.CLASS_LABEL, xaxis='class', yaxis='dice')
    if any(lambdas[1:]):
        log.report_scalar('UV Loss', mode, iteration=epoch, value=loss_avg[2].item())
        log.report_scalar('UV L1', mode, iteration=epoch, value=uv_l1.aggregate().mean().item())
        log.report_histogram('UV L1', mode, iteration=epoch,
                             values=uv_l1.aggregate().cpu().numpy(),
                             xlabels=data_loader.dataset.CLASS_LABEL, xaxis='class', yaxis='uv l1')
        log.report_scalar('TV Loss', mode, iteration=epoch, value=tv.aggregate().mean().item())
        log.report_histogram('TV Loss', mode, iteration=epoch,
                             values=tv.aggregate().cpu().numpy(),
                             xlabels=data_loader.dataset.CLASS_LABEL, xaxis='class', yaxis='tv')

        if lambda_reg_uv:
            log.report_scalar('Regression UV Loss', mode, iteration=epoch, value=loss_avg[3].item())
            log.report_scalar('Landmark UV Loss', mode, iteration=epoch, value=loss_avg[4].item())


def forward_heatmap(mode: str, data_loader: DataLoader, epoch: int,  # have to be given each call
                    # can be provided via kwargs dict
                    model: KeypointUNet, optimizer: Optimizer, device: torch.device, std_pixel: int, alpha: int,
                    data_aug: AugmentationSequential = None) -> (torch.Tensor, torch.Tensor):
    # set model mode according to mode
    if mode == 'train':
        model.train()
    elif mode in ['test', 'val']:
        model.eval()
    else:
        raise ValueError(f'Unknown mode: {mode}')

    loss_collector = metrics.CumulativeAverage()
    tre_collector = metrics.CumulativeAverage()

    for batch in data_loader:
        img, lm = batch[:2]
        img = img.to(device, non_blocking=True)
        lm = lm.to(device, non_blocking=True)  # (B, N, 2)

        if data_aug and model.training:
            img, lm = data_aug(img, lm)

        # create ground truth heatmap
        B, _, H, W = img.shape
        N = lm.shape[1]
        grid = torch.stack(torch.meshgrid(torch.arange(W, device=device),
                                          torch.arange(H, device=device), indexing='xy'), dim=-1)
        grid = grid.view(1, H * W, 2)
        gaussian = grid.unsqueeze(1) - lm.unsqueeze(2)  # (B, N, H*W, 2)
        gaussian = alpha * torch.exp(-torch.sum(gaussian.pow(2), dim=-1) / (2 * std_pixel ** 2))  # (B, N, H*W)
        heatmap = gaussian.view(B, N, H, W)

        # from matplotlib import pyplot as plt
        # plt.figure()
        # plt.imshow(img[0, 0].cpu().numpy(), cmap='gray')
        # plt.scatter(lm[0, :, 0].cpu().numpy(), lm[0, :, 1].cpu().numpy(), c='r')
        # for i, txt in enumerate(range(lm.shape[1])):
        #     plt.gca().annotate(txt, (lm[0, i, 0], lm[0, i, 1]))
        #
        # plt.figure()
        # idx = 0
        # plt.imshow(heatmap[0, idx].cpu().numpy())
        # plt.scatter(lm[0, idx, 0].cpu().numpy(), lm[0, idx, 1].cpu().numpy(), c='r')
        # plt.show()

        with torch.set_grad_enabled(model.training):  # forward
            heatmap_hat = model(img)
            loss = F.mse_loss(heatmap_hat, heatmap)

        if model.training:  # backward
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()

        # track metrics
        lm_hat = extract_kpts_from_heatmap(heatmap_hat.detach())
        tre = torch.linalg.vector_norm(lm - lm_hat, ord=2, dim=-1) * JSRTDataset.PIXEL_RESOLUTION_MM
        tre_organ = []
        for (start_idx, end_idx) in data_loader.dataset.get_anatomical_structure_index().values():
            tre_organ.append(tre[:, start_idx:end_idx].mean())

        tre_collector.append(tre_organ, count=B)
        loss_collector.append([loss, tre.mean()], count=B)

    # log metrics scalars
    log = Logger.current_logger()
    loss_avg = loss_collector.aggregate()  # [loss, tre]
    log.report_scalar('Loss', mode, iteration=epoch, value=loss_avg[0].item())
    log.report_scalar('TRE [mm]', mode, iteration=epoch, value=loss_avg[1].item())
    log.report_histogram('TRE [mm]', mode, iteration=epoch, values=tre_collector.aggregate(),
                         xlabels=data_loader.dataset.CLASS_LABEL, xaxis='class', yaxis='TRE [mm]')

def forward_heatmap_and_seg(mode: str, data_loader: DataLoader, epoch: int,  # have to be given each call
                    # can be provided via kwargs dict
                    model: KeypointSegUNet, optimizer: Optimizer, device: torch.device, std_pixel: int, alpha: int, lambda_loss: float,
                    bce_pos_weight: torch.Tensor, data_aug: AugmentationSequential = None) -> (torch.Tensor, torch.Tensor):
    # set model mode according to mode
    if mode == 'train':
        model.train()
    elif mode in ['test', 'val']:
        model.eval()
    else:
        raise ValueError(f'Unknown mode: {mode}')

    loss_collector = metrics.CumulativeAverage()
    tre_collector = metrics.CumulativeAverage()
    dsc = metrics.DiceMetric(reduction='mean_batch', include_background=True, ignore_empty=True,
                             num_classes=data_loader.dataset.N_CLASSES)

    for batch in data_loader:
        img, lm, _, seg = batch[:4]
        img = img.to(device, non_blocking=True)
        seg = seg.to(device, non_blocking=True)
        lm = lm.to(device, non_blocking=True)  # (B, N, 2)

        if data_aug and model.training:
            img, seg, lm = data_aug(img, seg, lm)

        # create ground truth heatmap
        B, _, H, W = img.shape
        N = lm.shape[1]
        grid = torch.stack(torch.meshgrid(torch.arange(W, device=device),
                                          torch.arange(H, device=device), indexing='xy'), dim=-1)
        grid = grid.view(1, H * W, 2)
        gaussian = grid.unsqueeze(1) - lm.unsqueeze(2)  # (B, N, H*W, 2)
        gaussian = alpha * torch.exp(-torch.sum(gaussian.pow(2), dim=-1) / (2 * std_pixel ** 2))  # (B, N, H*W)
        heatmap = gaussian.view(B, N, H, W)

        with torch.set_grad_enabled(model.training):  # forward
            seg_hat, heatmap_hat = model(img)
            seg_loss = F.binary_cross_entropy_with_logits(seg_hat, seg, pos_weight=bce_pos_weight)
            heatmap_loss = F.mse_loss(heatmap_hat, heatmap)
            loss = lambda_loss * seg_loss + (1 - lambda_loss) * heatmap_loss

        if model.training:  # backward
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()

        # track metrics
        lm_hat = extract_kpts_from_heatmap(heatmap_hat.detach())
        tre = torch.linalg.vector_norm(lm - lm_hat, ord=2, dim=-1) * JSRTDataset.PIXEL_RESOLUTION_MM
        tre_organ = []
        dsc(seg_hat.sigmoid() > 0.5, seg)
        for (start_idx, end_idx) in data_loader.dataset.get_anatomical_structure_index().values():
            tre_organ.append(tre[:, start_idx:end_idx].mean())

        tre_collector.append(tre_organ, count=B)
        loss_collector.append([loss, tre.mean(), seg_loss, heatmap_loss], count=B)

    # log metrics scalars
    log = Logger.current_logger()
    loss_avg = loss_collector.aggregate()  # [loss, tre, seg_loss, heatmap_loss]
    log.report_scalar('Loss', mode, iteration=epoch, value=loss_avg[0].item())
    log.report_scalar('BCE', mode, iteration=epoch, value=loss_avg[2].item())
    log.report_scalar('Heatmap MSE', mode, iteration=epoch, value=loss_avg[3].item())
    log.report_scalar('TRE [mm]', mode, iteration=epoch, value=loss_avg[1].item())
    log.report_histogram('TRE [mm]', mode, iteration=epoch, values=tre_collector.aggregate(),
                         xlabels=data_loader.dataset.CLASS_LABEL, xaxis='class', yaxis='TRE [mm]')
    log.report_scalar('Dice', mode, iteration=epoch, value=dsc.aggregate().mean().item())
    log.report_histogram('Dice', mode, iteration=epoch,
                         values=dsc.aggregate().cpu().numpy(),
                         xlabels=data_loader.dataset.CLASS_LABEL, xaxis='class', yaxis='dice')

