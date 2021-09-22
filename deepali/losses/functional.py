r"""Loss functions, evaluation metrics, and related utilities."""

import itertools
from typing import Optional, Sequence, Union

import torch
from torch import Tensor

from ..core.enum import SpatialDerivativeKeys
from ..core.grid import Grid
from ..core.image import avg_pool, dot_channels, spatial_derivatives
from ..core.flow import denormalize_flow
from ..core.pointset import transform_grid
from ..core.pointset import transform_points
from ..core.tensor import as_one_hot_tensor, move_dim
from ..core.types import Array, ScalarOrTuple


__all__ = (
    "label_smoothing",
    "dice_score",
    "dice_loss",
    "kld_loss",
    "lcc_loss",
    "mse_loss",
    "ssd_loss",
    "grad_loss",
    "bending_loss",
    "bending_energy_loss",
    "be_loss",
    "curvature_loss",
    "diffusion_loss",
    "divergence_loss",
    "elasticity_loss",
    "total_variation_loss",
    "tv_loss",
    "inverse_consistency_loss",
    "masked_loss",
    "reduce_loss",
)


def label_smoothing(
    labels: Tensor,
    num_classes: Optional[int] = None,
    ignore_index: Optional[int] = None,
    alpha: float = 0.1,
) -> Tensor:
    r"""Apply label smoothing to target labels.

    Implements label smoothing as proposed by Muller et al., (2019) in https://arxiv.org/abs/1906.02629v2.

    Args:
        labels: Scalar target labels or one-hot encoded class probabilities.
        num_classes: Number of target labels. If ``None``, use maximum value in ``target`` plus 1
            when a scalar label map is given.
        ignore_index: Ignore index to be kept during the expansion. The locations of the index
            value in the labels image is stored in the corresponding locations across all channels
            so that this location can be ignored across all channels later, e.g. in Dice computation.
            This argument must be ``None`` if ``labels`` has ``C == num_channels``.
        alpha: Label smoothing factor in [0, 1]. If zero, no label smoothing is done.

    Returns:
        Multi-channel tensor of smoothed target class probabilities.

    """
    if not isinstance(labels, Tensor):
        raise TypeError("label_smoothing() 'labels' must be Tensor")
    if labels.ndim < 4:
        raise ValueError("label_smoothing() 'labels' must be tensor of shape (N, C, ..., X)")
    if labels.shape[1] == 1:
        target = as_one_hot_tensor(
            labels, num_classes, ignore_index=ignore_index, dtype=torch.float32
        )
    else:
        target = labels.float()
    if alpha > 0:
        target = (1 - alpha) * target + alpha * (1 - target) / (target.size(1) - 1)
    return target


def dice_score(
    input: Tensor,
    target: Tensor,
    weight: Optional[Tensor] = None,
    epsilon: float = 1e-15,
    reduction: str = "mean",
) -> Tensor:
    r"""Soft Dice similarity of multi-channel classification result.

    Args:
        input: Normalized logits of binary predictions as tensor of shape ``(N, C, ..., X)``.
        target: Target label probabilities as tensor of shape ``(N, C, ..., X)``.
        weight: Voxelwise label weight tensor of shape ``(N, C, ..., X)``.
        epsilon: Small constant used to avoid division by zero.
        reduction: Either ``none``, ``mean``, or ``sum``.

    Returns:
        Dice similarity coefficient (DSC). If ``reduction="none"``, the returned tensor has shape
        ``(N, C)`` with DSC for each batch. Otherwise, the DSC scores are reduced into a scalar.

    """
    if not isinstance(input, Tensor):
        raise TypeError("dice_score() 'input' must be torch.Tensor")
    if not isinstance(target, Tensor):
        raise TypeError("dice_score() 'target' must be torch.Tensor")
    if input.dim() < 3:
        raise ValueError("dice_score() 'input' must be tensor of shape (N, C, ..., X)")
    if input.shape != target.shape:
        raise ValueError("dice_score() 'input' and 'target' must have identical shape")
    y_pred = input.float()
    y = target.float()
    intersection = dot_channels(y_pred, y, weight=weight)
    denominator = dot_channels(y_pred, y_pred, weight=weight) + dot_channels(y, y, weight=weight)
    loss = intersection.mul_(2).add_(epsilon).div(denominator.add_(epsilon))
    loss = reduce_loss(loss, reduction)
    return loss


def dice_loss(
    input: Tensor,
    target: Tensor,
    weight: Optional[Tensor] = None,
    epsilon: float = 1e-15,
    reduction: str = "mean",
) -> Tensor:
    r"""One minus soft Dice similarity of multi-channel classification result.

    Args:
        input: Normalized logits of binary predictions as tensor of shape ``(N, C, ..., X)``.
        target: Target label probabilities as tensor of shape ``(N, C, ..., X)``.
        weight: Voxelwise label weight tensor of shape ``(N, C, ..., X)``.
        epsilon: Small constant used to avoid division by zero.
        reduction: Either ``none``, ``mean``, or ``sum``.

    Returns:
        One minus Dice similarity coefficient (DSC). If ``reduction="none"``, the returned tensor has shape
        ``(N, C)`` with DSC for each batch. Otherwise, the DSC scores are reduced into a scalar.

    """
    dsc = dice_score(input, target, weight=weight, epsilon=epsilon, reduction="none")
    loss = reduce_loss(1 - dsc, reduction)
    return loss


def kld_loss(mean: Tensor, logvar: Tensor, reduction: str = "mean") -> Tensor:
    r"""Kullback-Leibler divergence in case of zero-mean and isotropic unit variance normal prior distribution.

    Kingma and Welling, Auto-Encoding Variational Bayes, ICLR 2014, https://arxiv.org/abs/1312.6114 (Appendix B).

    """
    loss = mean.square().add_(logvar.exp()).sub_(1).sub_(logvar)
    loss = reduce_loss(loss, reduction)
    loss = loss.mul_(0.5)
    return loss


def lcc_loss(
    input: Tensor,
    target: Tensor,
    mask: Optional[Tensor] = None,
    kernel_size: ScalarOrTuple[int] = 7,
    epsilon: float = 1e-15,
    reduction: str = "mean",
) -> Tensor:
    r"""Local normalized cross correlation.

    Args:
        input: Source image sampled on ``target`` grid.
        target: Target image with same shape as ``input``.
        mask: Multiplicative mask tensor with same shape as ``input``.
        kernel_size: Local rectangular window size in number of grid points.
        epsilon: Small constant added to denominator to avoid division by zero.
        reduction: Whether to compute "mean" or "sum" over all grid points. If "none",
            output tensor shape is equal to the shape of the input tensors given an odd
            kernel size.

    Returns:
        Negative local normalized cross correlation plus one.

    """

    def pool(data: Tensor) -> Tensor:
        return avg_pool(
            data, kernel_size=kernel_size, stride=1, padding=None, count_include_pad=False
        )

    if not torch.is_tensor(input):
        raise TypeError("lcc_loss() 'input' must be tensor")
    if not torch.is_tensor(target):
        raise TypeError("lcc_loss() 'target' must be tensor")
    if input.shape != target.shape:
        raise ValueError("lcc_loss() 'input' must have same shape as 'target'")
    input = input.float()
    target = target.float()
    x = input - pool(input)
    y = target - pool(target)
    a = pool(x.mul(y))
    b = pool(x.square())
    c = pool(y.square())
    lcc = a.square().div_(b.mul(c).add_(epsilon))  # A^2 / BC cf. Avants et al., 2007, eq 5
    loss = lcc.mul_(-1).add_(1)  # minimize 1 - lcc, where loss range is [0, 1]
    loss = masked_loss(loss, mask, "lcc_loss")
    loss = reduce_loss(loss, reduction, mask)
    return loss


def mse_loss(
    input: Tensor,
    target: Tensor,
    mask: Optional[Tensor] = None,
    norm: Optional[Union[float, Tensor]] = None,
    reduction: str = "mean",
) -> Tensor:
    r"""Average normalized squared differences.

    This loss is equivalent to `ssd_loss`, except that the default `reduction` is "mean".

    Args:
        input: Source image sampled on ``target`` grid.
        target: Target image with same shape as ``input``.
        mask: Multiplicative mask with same shape as ``input``.
        norm: Positive factor by which to divide loss value.
        reduction: Whether to compute "mean" or "sum" over all grid points.
            If "none", output tensor shape is equal to the shape of the input tensors.

    Returns:
        Average normalized squared differences.

    """
    return ssd_loss(input, target, mask=mask, norm=norm, reduction=reduction)


def ssd_loss(
    input: Tensor,
    target: Tensor,
    mask: Optional[Tensor] = None,
    norm: Optional[Union[float, Tensor]] = None,
    reduction: str = "sum",
) -> Tensor:
    r"""Sum of normalized squared differences.

    The SSD loss is equivalent to MSE, except that an optional overlap mask is supported and
    that the loss value is optionally multiplied by a normalization constant. Moreover, by default
    the sum instead of the mean of per-element loss values is returned (cf. ``reduction``).
    The value returned by ``max_difference(source, target).square()`` can be used as normalization
    factor, which is equvalent to first normalizing the images to [0, 1].

    Args:
        input: Source image sampled on ``target`` grid.
        target: Target image with same shape as ``input``.
        mask: Multiplicative mask with same shape as ``input``.
        norm: Positive factor by which to divide loss value.
        reduction: Whether to compute "mean" or "sum" over all grid points.
            If "none", output tensor shape is equal to the shape of the input tensors.

    Returns:
        Sum of normalized squared differences.

    """
    if not isinstance(input, Tensor):
        raise TypeError("ssd_loss() 'input' must be tensor")
    if not isinstance(target, Tensor):
        raise TypeError("ssd_loss() 'target' must be tensor")
    if input.shape != target.shape:
        raise ValueError("ssd_loss() 'input' must have same shape as 'target'")
    loss = input.sub(target).square()
    loss = masked_loss(loss, mask, "ssd_loss")
    loss = reduce_loss(loss, reduction, mask)
    if norm is not None:
        norm = torch.as_tensor(norm, dtype=loss.dtype, device=loss.device).squeeze()
        if not norm.ndim == 0:
            raise ValueError("ssd_loss() 'norm' must be scalar")
        if norm > 0:
            loss = loss.div_(norm)
    return loss


def grad_loss(
    u: Tensor,
    p: Union[int, float] = 2,
    q: Optional[Union[int, float]] = 1,
    spacing: Optional[Array] = None,
    sigma: Optional[float] = None,
    mode: str = "central",
    which: Optional[Union[str, Sequence[str]]] = None,
    reduction: str = "mean",
) -> Tensor:
    r"""Loss term based on p-norm of spatial gradient of vector fields.

    The ``p`` and ``q`` parameters can be used to specify which norm to compute, i.e., ``sum(abs(du)**p)**q``,
    where ``du`` are the 1st order spatial derivative of the input vector fields ``u`` computed using a finite
    difference scheme and optionally normalized using a specified grid ``spacing``.

    This regularization loss is the basis, for example, for total variation and diffusion penalties.

    Args:
        u: Batch of vector fields as tensor of shape ``(N, D, ..., X)``. When a tensor with less than
            four dimensions is given, it is assumed to be a linear transformation and zero is returned.
        p: The order of the gradient norm. When ``p = 0``, the partial derivatives are summed up.
        q: Power parameter of gradient norm. If ``None``, then ``q = 1 / p``. If ``q == 0``, the
            absolute value of the sum of partial derivatives is computed at each grid point.
        spacing: Sampling grid spacing.
        sigma: Standard deviation of Gaussian in grid units.
        mode: Method used to approximate spatial derivatives. See ``spatial_derivatives()``.
        which: String codes of spatial deriviatives to compute. See ``SpatialDerivativeKeys``.
        reduction: Specifies the reduction to apply to the output: 'none' | 'mean' | 'sum'.

    Returns:
        Spatial gradient loss of vector fields.

    """
    if u.ndim < 4:
        # No loss for homogeneous coordinate transformations
        if reduction == "none":
            raise NotImplementedError(
                "grad_loss() not implemented for linear transformation and 'reduction'='none'"
            )
        return torch.tensor(0, dtype=u.dtype, device=u.device)
    D = u.shape[1]
    if u.ndim - 2 != D:
        raise ValueError(f"grad_loss() 'u' must be tensor of shape (N, {u.ndim - 2}, ..., X)")
    if q is None:
        q = 1.0 / p
    derivs = spatial_derivatives(u, mode=mode, which=which, order=1, sigma=sigma, spacing=spacing)
    loss = torch.cat([deriv.unsqueeze(-1) for deriv in derivs.values()], dim=-1)
    if p == 1:
        loss = loss.abs()
    elif p != 0:
        if p % 2 == 0:
            loss = loss.pow(p)
        else:
            loss = loss.abs().pow_(p)
    loss = loss.sum(dim=-1)
    if q == 0:
        loss.abs_()
    elif q != 1:
        loss.pow_(q)
    loss = reduce_loss(loss, reduction)
    return loss


def bending_loss(
    u: Tensor,
    spacing: Optional[Array] = None,
    sigma: Optional[float] = None,
    mode: str = "sobel",
    reduction: str = "mean",
) -> Tensor:
    r"""Bending energy of vector fields.

    Args:
        u: Batch of vector fields as tensor of shape ``(N, D, ..., X)``. When a tensor with less than
            four dimensions is given, it is assumed to be a linear transformation and zero is returned.
        spacing: Sampling grid spacing.
        sigma: Standard deviation of Gaussian in grid units (cf. ``spatial_derivatives()``).
        mode: Method used to approximate spatial derivatives (cf. ``spatial_derivatives()``).
        reduction: Specifies the reduction to apply to the output: 'none' | 'mean' | 'sum'.

    Returns:
        Bending energy.

    """
    if u.ndim < 4:
        # No loss for homogeneous coordinate transformations
        if reduction == "none":
            raise NotImplementedError(
                "bending_energy() not implemented for linear transformation and 'reduction'='none'"
            )
        return torch.tensor(0, dtype=u.dtype, device=u.device)
    D = u.shape[1]
    if u.ndim - 2 != D:
        raise ValueError(f"bending_energy() 'u' must be tensor of shape (N, {u.ndim - 2}, ..., X)")
    which = SpatialDerivativeKeys.unique(SpatialDerivativeKeys.all(ndim=D, order=2))
    derivs = spatial_derivatives(u, mode=mode, which=which, sigma=sigma, spacing=spacing)
    derivs = torch.cat([deriv.unsqueeze(-1) for deriv in derivs.values()], dim=-1)
    derivs *= torch.tensor(
        [2 if SpatialDerivativeKeys.is_mixed(key) else 1 for key in which], device=u.device
    )
    loss = derivs.pow(2).sum(-1)
    loss = reduce_loss(loss, reduction)
    return loss


be_loss = bending_loss
bending_energy_loss = bending_loss


def curvature_loss(
    u: Tensor,
    spacing: Optional[Array] = None,
    sigma: Optional[float] = None,
    mode: str = "sobel",
    reduction: str = "mean",
) -> Tensor:
    r"""Loss term based on unmixed 2nd order spatial derivatives of vector fields.

    Fischer & Modersitzki (2003). Curvature based image registration. Journal Mathematical Imaging and Vision, 18(1), 81–85.

    Args:
        u: Batch of vector fields as tensor of shape ``(N, D, ..., X)``. When a tensor with less than
            four dimensions is given, it is assumed to be a linear transformation and zero is returned.
        spacing: Sampling grid spacing.
        sigma: Standard deviation of Gaussian in grid units (cf. ``spatial_derivatives()``).
        mode: Method used to approximate spatial derivatives (cf. ``spatial_derivatives()``).
        reduction: Specifies the reduction to apply to the output: 'none' | 'mean' | 'sum'.

    Returns:
        Curvature loss of vector fields.

    """
    if u.ndim < 4:
        # No loss for homogeneous coordinate transformations
        if reduction == "none":
            raise NotImplementedError(
                "curvature_loss() not implemented for linear transformation and 'reduction'='none'"
            )
        return torch.tensor(0, dtype=u.dtype, device=u.device)
    D = u.shape[1]
    if u.ndim - 2 != D:
        raise ValueError(f"curvature_loss() 'u' must be tensor of shape (N, {u.ndim - 2}, ..., X)")
    which = SpatialDerivativeKeys.unmixed(ndim=D, order=2)
    derivs = spatial_derivatives(u, mode=mode, which=which, sigma=sigma, spacing=spacing)
    derivs = torch.cat([deriv.unsqueeze(-1) for deriv in derivs.values()], dim=-1)
    loss = 0.5 * derivs.sum(-1).pow(2)
    loss = reduce_loss(loss, reduction)
    return loss


def diffusion_loss(
    u: Tensor,
    spacing: Optional[Tensor] = None,
    sigma: Optional[float] = None,
    mode: str = "central",
    reduction: str = "mean",
) -> Tensor:
    r"""Diffusion regularization loss."""
    loss = grad_loss(u, p=2, q=1, spacing=spacing, sigma=sigma, mode=mode, reduction=reduction)
    return loss.mul_(0.5)


def divergence_loss(
    u: Tensor,
    q: Optional[Union[int, float]] = 1,
    spacing: Optional[Array] = None,
    sigma: Optional[float] = None,
    mode: str = "central",
    reduction: str = "mean",
) -> Tensor:
    r"""Loss term encouraging divergence-free vector fields."""
    if u.ndim < 4:
        # No loss for homogeneous coordinate transformations
        if reduction == "none":
            raise NotImplementedError(
                "div_loss() not implemented for linear transformation and 'reduction'='none'"
            )
        return torch.tensor(0, dtype=u.dtype, device=u.device)
    D = u.shape[1]
    if u.ndim - 2 != D:
        raise ValueError(f"div_loss() 'u' must be tensor of shape (N, {u.ndim - 2}, ..., X)")
    which = SpatialDerivativeKeys.unmixed(ndim=D, order=1)
    loss = grad_loss(u, p=0, spacing=spacing, sigma=sigma, mode=mode, which=which, reduction="none")
    loss = loss.abs_() if q == 1 else loss.pow_(q)
    loss = reduce_loss(loss, reduction)
    return loss


def elasticity_loss(
    u: Tensor,
    spacing: Optional[Array] = None,
    sigma: Optional[float] = None,
    mode: str = "sobel",
    reduction: str = "mean",
) -> Tensor:
    r"""Loss term based on Navier-Cauchy PDE of linear elasticity.

    This linear elasticity loss includes only the term based on 1st order derivatives. The term of the
    Laplace operator, i.e., sum of unmixed 2nd order derivatives, is equivalent to the ``curvature_loss()``.
    This loss can be combined with the curvature regularization term to form a linear elasticity loss.

    Args:
        u: Batch of vector fields as tensor of shape ``(N, D, ..., X)``. When a tensor with less than
            four dimensions is given, it is assumed to be a linear transformation and zero is returned.
        spacing: Sampling grid spacing.
        sigma: Standard deviation of Gaussian in grid units (cf. ``spatial_derivatives()``).
        mode: Method used to approximate spatial derivatives (cf. ``spatial_derivatives()``).
        reduction: Specifies the reduction to apply to the output: 'none' | 'mean' | 'sum'.

    Returns:
        Linear elasticity loss of vector field.

    """
    if u.ndim < 4:
        # No loss for homogeneous coordinate transformations
        if reduction == "none":
            raise NotImplementedError(
                "elasticity_loss() not implemented for linear transformation and 'reduction'='none'"
            )
        return torch.tensor(0, dtype=u.dtype, device=u.device)
    N = u.shape[0]
    D = u.shape[1]
    if u.ndim - 2 != D:
        raise ValueError(f"elasticity_loss() 'u' must be tensor of shape (N, {u.ndim - 2}, ..., X)")
    derivs = spatial_derivatives(u, mode=mode, order=1, sigma=sigma, spacing=spacing)
    derivs = torch.cat([deriv.unsqueeze(-1) for deriv in derivs.values()], dim=-1)
    loss = torch.zeros((N,) + u.shape[2:], dtype=derivs.dtype, device=derivs.device)
    for a, b in itertools.product(range(D), repeat=2):
        loss += (0.5 * (derivs[:, a, ..., b] + derivs[:, b, ..., a])).square()
    if reduction == "none":
        loss = loss.unsqueeze(1)
    else:
        loss = reduce_loss(loss, reduction)
    return loss


def total_variation_loss(
    u: Tensor,
    spacing: Optional[Tensor] = None,
    sigma: Optional[float] = None,
    mode: str = "central",
    reduction: str = "mean",
) -> Tensor:
    r"""Total variation regularization loss."""
    return grad_loss(u, p=1, q=1, spacing=spacing, sigma=sigma, mode=mode, reduction=reduction)


tv_loss = total_variation_loss


def inverse_consistency_loss(
    forward: Tensor,
    inverse: Tensor,
    grid: Optional[Grid] = None,
    margin: Union[int, float] = 0,
    mask: Optional[Tensor] = None,
    units: str = "cube",
    reduction: str = "mean",
) -> Tensor:
    r"""Evaluate inverse consistency error.

    This function expects forward and inverse coordinate maps to be with respect to the unit cube
    of side length 2 as defined by the domain and codomain ``grid`` (see also ``Grid.axes()``).

    Args:
        forward: Tensor representation of spatial transformation.
        inverse: Tensor representation of inverse transformation.
        grid: Coordinate domain and codomain of forward transformation.
        margin: Number of ``grid`` points to ignore when computing mean error. If type of the
            argument is ``int``, this number of points are dropped at each boundary in each dimension.
            If a ``float`` value is given, it must be in [0, 1) and denote the percentage of sampling
            points to drop at each border. Inverse consistency of points near the domain boundary is
            affected by extrapolation and excluding these may be preferrable. See also ``mask``.
        mask: Foreground mask as tensor of shape ``(N, 1, ..., X)`` with size matching ``forward``.
            Inverse consistency errors at target grid points with a zero mask value are ignored.
        units: Compute mean inverse consistency error in specified units: ``cube`` with respect to
            normalized grid cube coordinates, ``voxel`` in voxel units, or in ``world`` units (mm).
        reduction: Specifies the reduction to apply to the output: 'none' | 'mean' | 'sum'.

    Returns:
        Inverse consistency error.

    """
    if not isinstance(forward, Tensor):
        raise TypeError("inverse_consistency_loss() 'forward' must be tensor")
    if not isinstance(inverse, Tensor):
        raise TypeError("inverse_consistency_loss() 'inverse' must be tensor")
    if not isinstance(margin, (int, float)):
        raise TypeError("inverse_consistency_loss() 'margin' must be int or float")
    if grid is None:
        if forward.ndim < 4:
            if inverse.ndim < 4:
                raise ValueError(
                    "inverse_consistency_loss() 'grid' required when both transforms are affine"
                )
            grid = Grid(shape=inverse.shape[2:])
        else:
            grid = Grid(shape=forward.shape[2:])
    # Compute inverse consistency error for each grid point
    x = grid.coords(dtype=forward.dtype, device=forward.device).unsqueeze(0)
    y = transform_grid(forward, x, align_corners=grid.align_corners())
    y = transform_points(inverse, y, align_corners=grid.align_corners())
    error = y - x
    # Set error outside foreground mask to zero
    if mask is not None:
        if not isinstance(mask, Tensor):
            raise TypeError("inverse_consistency_loss() 'mask' must be tensor")
        if mask.ndim != grid.ndim + 2:
            raise ValueError(
                f"inverse_consistency_loss() 'mask' must be {grid.ndim + 2}-dimensional"
            )
        if mask.shape[1] != 1:
            raise ValueError("inverse_consistency_loss() 'mask' must have shape (N, 1, ..., X)")
        if mask.shape[0] != 1 and mask.shape[0] != error.shape[0]:
            raise ValueError(
                f"inverse_consistency_loss() 'mask' batch size must be 1 or {error.shape[0]}"
            )
        error[move_dim(mask == 0, 1, -1).expand_as(error)] = 0
    # Discard error at grid boundary
    if margin > 0:
        if isinstance(margin, float):
            if margin < 0 or margin >= 1:
                raise ValueError(
                    f"inverse_consistency_loss() 'margin' must be in [0, 1), got {margin}"
                )
            m = [int(margin * n) for n in grid.size()]
        else:
            m = [max(0, int(margin))] * grid.ndim
        subgrid = tuple(reversed([slice(i, n - i) for i, n in zip(m, grid.size())]))
        error = error[(slice(0, error.shape[0]),) + subgrid + (slice(0, grid.ndim),)]
    # Scale differences by respective error units
    if units in ("voxel", "world"):
        error = denormalize_flow(error, size=grid.size(), channels_last=True)
        if units == "world":
            error *= grid.spacing().to(error)
    # Calculate error norm
    error: Tensor = error.norm(p=2, dim=-1)
    # Reduce error if requested
    if reduction != "none":
        count = error.numel()
        error = error.sum()
        if reduction == "mean" and mask is not None:
            count = (mask != 0).sum()
        error /= count
    return error


def masked_loss(loss: Tensor, mask: Optional[Tensor] = None, name: Optional[str] = None) -> Tensor:
    r"""Multiply loss with an optionally specified spatial mask."""
    if mask is None:
        return loss
    if not name:
        name = "masked_loss"
    if not isinstance(mask, Tensor):
        raise TypeError(f"{name}() 'mask' must be tensor")
    if mask.shape[0] != 1 and mask.shape[0] != loss.shape[0]:
        raise ValueError(f"{name}() 'mask' must have same batch size as 'target' or batch size 1")
    if mask.shape[1] != 1 and mask.shape[1] != loss.shape[0]:
        raise ValueError(f"{name}() 'mask' must have same number of channels as 'target' or only 1")
    if mask.shape[2:] != loss.shape[2:]:
        raise ValueError(f"{name}() 'mask' must have same spatial shape as 'target'")
    return loss.mul_(mask)


def reduce_loss(loss: Tensor, reduction: str = "mean", mask: Optional[Tensor] = None) -> Tensor:
    r"""Reduce loss computed at each grid point."""
    if reduction not in ("mean", "sum", "none"):
        raise ValueError("reduce_loss() 'reduction' must be 'mean', 'sum' or 'none'")
    if reduction == "none":
        return loss
    if mask is None:
        return loss.mean() if reduction == "mean" else loss.sum()
    value = loss.sum()
    if reduction == "mean":
        numel = mask.expand_as(loss).sum()
        value = value.div_(numel)
    return value
