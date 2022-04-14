from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import torch
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from torch import Tensor, nn
from torchvision.ops import box_convert

from pl_bolts.models.detection.yolo.loss import LossFunction
from pl_bolts.models.detection.yolo.target_matching import (
    HighestIoUMatching,
    IoUThresholdMatching,
    ShapeMatching,
    SimOTAMatching,
    SizeRatioMatching,
)
from pl_bolts.models.detection.yolo.utils import global_xy
from pl_bolts.utils import _TORCHVISION_AVAILABLE


def _get_padding(kernel_size: int, stride: int) -> Tuple[int, nn.Module]:
    """Returns the amount of padding needed by convolutional and max pooling layers.

    Determines the amount of padding needed to make the output size of the layer the input size divided by the stride.
    The first value that the function returns is the amount of padding to be added to all sides of the input matrix
    (``padding`` argument of the operation). If an uneven amount of padding is needed in different sides of the input,
    the second variable that is returned is an ``nn.ZeroPad2d`` operation that adds an additional column and row of
    padding. If the input size is not divisible by the stride, the output size will be rounded upwards.

    Args:
        kernel_size: Size of the kernel.
        stride: Stride of the operation.

    Returns:
        padding, pad_op: The amount of padding to be added to all sides of the input and an ``nn.Identity`` or
        ``nn.ZeroPad2d`` operation to add one more column and row of padding if necessary.
    """
    # The output size is generally (input_size + padding - max(kernel_size, stride)) / stride + 1 and we want to
    # make it equal to input_size / stride.
    padding, remainder = divmod(max(kernel_size, stride) - stride, 2)

    # If the kernel size is an even number, we need one cell of extra padding, on top of the padding added by MaxPool2d
    # on both sides.
    pad_op: nn.Module = nn.Identity() if remainder == 0 else nn.ZeroPad2d((0, 1, 0, 1))

    return padding, pad_op


class DetectionLayer(nn.Module):
    """A YOLO detection layer.

    A YOLO model has usually 1 - 3 detection layers at different resolutions. The loss is summed from all of them.

    Args:
        num_classes: Number of different classes that this layer predicts.
        prior_shapes: A list of prior box dimensions for this layer, used for scaling the predicted dimensions. The list
            should contain (width, height) tuples in the network input resolution.
        matching_func: The matching algorithm to be used for assigning targets to anchors.
        loss_func: ``LossFunction`` object for calculating the losses.
        xy_scale: Eliminate "grid sensitivity" by scaling the box coordinates by this factor. Using a value > 1.0 helps
            to produce coordinate values close to one.
        input_is_normalized: The input is normalized by logistic activation in the previous layer. In this case the
            detection layer will not take the sigmoid of the coordinate and probability predictions, and the width and
            height are scaled up so that the maximum value is four times the anchor dimension.
    """

    def __init__(
        self,
        num_classes: int,
        prior_shapes: List[Tuple[int, int]],
        matching_func: Callable,
        loss_func: LossFunction,
        xy_scale: float = 1.0,
        input_is_normalized: bool = False,
    ) -> None:
        super().__init__()

        if not _TORCHVISION_AVAILABLE:  # pragma: no cover
            raise ModuleNotFoundError("YOLO model uses `torchvision`, which is not installed yet.")

        self.num_classes = num_classes
        self.prior_shapes = prior_shapes
        self.matching_func = matching_func
        self.loss_func = loss_func
        self.xy_scale = xy_scale
        self.input_is_normalized = input_is_normalized

    def forward(self, x: Tensor, image_size: Tensor, targets: Optional[List[Dict[str, Tensor]]] = None) -> Tensor:
        """Runs a forward pass through this YOLO detection layer.

        Maps cell-local coordinates to global coordinates in the image space, scales the bounding boxes with the
        anchors, converts the center coordinates to corner coordinates, and maps probabilities to the `]0, 1[` range
        using sigmoid.

        If targets are given, computes also losses from the predictions and the targets. This layer is responsible only
        for the targets that best match one of the anchors assigned to this layer. Training losses will be saved to the
        ``losses`` attribute. ``hits`` attribute will be set to the number of targets that this layer was responsible
        for. ``losses`` is a tensor of three elements: the overlap, confidence, and classification loss.

        Args:
            x: The output from the previous layer. Tensor of size
                ``[batch_size, anchors_per_cell * (num_classes + 5), height, width]``.
            image_size: Image width and height in a vector (defines the scale of the predicted and target coordinates).
            targets: If set, computes losses from detection layers against these targets. A list of target dictionaries,
                one for each image.

        Returns:
            Layer output tensor, sized ``[batch_size, num_anchors * height * width, num_classes + 5]``.
        """
        batch_size, num_features, height, width = x.shape
        num_attrs = self.num_classes + 5
        anchors_per_cell = num_features // num_attrs
        if anchors_per_cell != len(self.prior_shapes):
            raise MisconfigurationException(
                "The model predicts {} bounding boxes per spatial location, but {} prior box dimensions are defined "
                "for this layer.".format(anchors_per_cell, len(self.prior_shapes))
            )

        # Reshape the output to have the bounding box attributes of each grid cell on its own row.
        x = x.permute(0, 2, 3, 1)  # [batch_size, height, width, anchors_per_cell * num_attrs]
        x = x.view(batch_size, height, width, anchors_per_cell, num_attrs)

        # Take the sigmoid of the bounding box coordinates, confidence score, and class probabilities, unless the input
        # is normalized by the previous layer activation. Confidence and class losses use the unnormalized values if
        # possible.
        norm_x = x if self.input_is_normalized else torch.sigmoid(x)
        xy = norm_x[..., :2]
        wh = x[..., 2:4]
        confidence = x[..., 4]
        classprob = x[..., 5:]
        norm_confidence = norm_x[..., 4]
        norm_classprob = norm_x[..., 5:]

        # Eliminate grid sensitivity. The previous layer should output extremely high values for the sigmoid to produce
        # x/y coordinates close to one. YOLOv4 solves this by scaling the x/y coordinates.
        xy = xy * self.xy_scale - 0.5 * (self.xy_scale - 1)

        image_xy = global_xy(xy, image_size)
        if self.input_is_normalized:
            image_wh = 4 * torch.square(wh) * torch.tensor(self.prior_shapes, dtype=wh.dtype, device=wh.device)
        else:
            image_wh = torch.exp(wh) * torch.tensor(self.prior_shapes, dtype=wh.dtype, device=wh.device)
        box = torch.cat((image_xy, image_wh), -1)
        box = box_convert(box, in_fmt="cxcywh", out_fmt="xyxy")
        output = torch.cat((box, norm_confidence.unsqueeze(-1), norm_classprob), -1)
        output = output.reshape(batch_size, height * width * anchors_per_cell, num_attrs)

        if targets is not None:
            # We want to use binary_cross_entropy_with_logits, so we'll use the unnormalized confidence and classprob,
            # if possible.
            preds = [{"boxes": b, "confidences": c, "classprobs": p} for b, c, p in zip(box, confidence, classprob)]
            self._calculate_losses(preds, targets, image_size)

        return output

    def _calculate_losses(
        self,
        preds: List[Dict[str, Tensor]],
        targets: List[Dict[str, Tensor]],
        image_size: Tensor,
    ) -> None:
        """Matches the predictions to targets and calculates the losses. Creates the attributes ``losses`` and
        ``hits``. ``losses`` is a tensor of three elements: the overlap, confidence, and classification loss.
        ``hits`` is the number of targets that this layer was responsible for.

        Args:
            preds: List of predictions for each image.
            targets: List of training targets for each image.
            image_size: Width and height in a vector that defines the scale of the target coordinates.
        """
        batch_size = len(preds)
        if batch_size != len(targets):
            raise ValueError("Different batch size for predictions and targets.")

        matches = []
        for image_preds, image_targets in zip(preds, targets):
            if image_targets["boxes"].shape[0] > 0:
                matched_preds, matched_targets = self.matching_func(image_preds, image_targets, image_size)
            else:
                device = image_preds["confidences"].device
                matched_preds = {
                    "boxes": torch.empty((0, 4), device=device),
                    "confidences": torch.empty(0, device=device),
                    "bg_confidences": image_preds["confidences"].flatten(),
                    "classprobs": torch.empty((0, self.num_classes), device=device),
                }
                matched_targets = {
                    "boxes": torch.empty((0, 4), device=device),
                    "labels": torch.empty(0, dtype=torch.int64, device=device),
                }
            matches.append((matched_preds, matched_targets))

        matched_preds = {
            "boxes": torch.cat(tuple(m[0]["boxes"] for m in matches)),
            "confidences": torch.cat(tuple(m[0]["confidences"] for m in matches)),
            "bg_confidences": torch.cat(tuple(m[0]["bg_confidences"] for m in matches)),
            "classprobs": torch.cat(tuple(m[0]["classprobs"] for m in matches)),
        }
        matched_targets = {
            "boxes": torch.cat(tuple(m[1]["boxes"] for m in matches)),
            "labels": torch.cat(tuple(m[1]["labels"] for m in matches)),
        }
        self.loss_func(matched_preds, matched_targets, self.input_is_normalized, image_size)
        overlap_loss, confidence_loss, class_loss = self.loss_func.sums()
        self.losses = torch.stack((overlap_loss, confidence_loss, class_loss)) / batch_size
        self.hits = len(matched_targets["boxes"])


class Conv(nn.Module):
    """A convolutional layer with optional layer normalization and activation.

    If ``padding`` is ``None``, the module tries to add padding so much that the output size will be the input size
    divided by the stride. If the input size is not divisible by the stride, the output size will be rounded upwards.

    Args:
        in_channels: Number of input channels that the layer expects.
        out_channels: Number of output channels that the convolution produces.
        kernel_size: Size of the convolving kernel.
        stride: Stride of the convolution.
        padding: Padding added to all four sides of the input.
        bias: If ``True``, adds a learnable bias to the output.
        activation: Which layer activation to use. Can be "relu", "leaky", "mish", "silu" (or "swish"), "logistic",
            "linear", or "none".
        norm: Which layer normalization to use. Can be "batchnorm", "groupnorm", or "none".
    """

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size: int = 1,
        stride: int = 1,
        padding: Optional[int] = None,
        bias: bool = False,
        activation: Optional[str] = "silu",
        norm: Optional[str] = "batchnorm",
    ):
        super().__init__()

        if padding is None:
            padding, self.pad = _get_padding(kernel_size, stride)
        else:
            self.pad = nn.Identity()

        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size, stride, padding, bias=bias)
        self.norm = create_normalization_module(norm, out_channels)
        self.act = create_activation_module(activation)

    def forward(self, x: Tensor) -> Tensor:
        x = self.pad(x)
        x = self.conv(x)
        x = self.norm(x)
        return self.act(x)


class MaxPool(nn.Module):
    """A max pooling layer with padding.

    The module tries to add padding so much that the output size will be the input size divided by the stride. If the
    input size is not divisible by the stride, the output size will be rounded upwards.
    """

    def __init__(self, kernel_size: int, stride: int):
        super().__init__()
        padding, self.pad = _get_padding(kernel_size, stride)
        self.maxpool = nn.MaxPool2d(kernel_size, stride, padding)

    def forward(self, x: Tensor) -> Tensor:
        x = self.pad(x)
        return self.maxpool(x)


class RouteLayer(nn.Module):
    """Route layer concatenates the output (or part of it) from given layers.

    Args:
        source_layers: Indices of the layers whose output will be concatenated.
        num_chunks: Layer outputs will be split into this number of chunks.
        chunk_idx: Only the chunks with this index will be concatenated.
    """

    def __init__(self, source_layers: List[int], num_chunks: int, chunk_idx: int) -> None:
        super().__init__()
        self.source_layers = source_layers
        self.num_chunks = num_chunks
        self.chunk_idx = chunk_idx

    def forward(self, x: Tensor, outputs: List[Tensor]) -> Tensor:
        chunks = [torch.chunk(outputs[layer], self.num_chunks, dim=1)[self.chunk_idx] for layer in self.source_layers]
        return torch.cat(chunks, dim=1)


class ShortcutLayer(nn.Module):
    """Shortcut layer adds a residual connection from the source layer.

    Args:
        source_layer: Index of the layer whose output will be added to the output of the previous layer.
    """

    def __init__(self, source_layer: int) -> None:
        super().__init__()
        self.source_layer = source_layer

    def forward(self, x: Tensor, outputs: List[Tensor]) -> Tensor:
        return outputs[-1] + outputs[self.source_layer]


class Mish(nn.Module):
    """Mish activation."""

    def forward(self, x: Tensor) -> Tensor:
        return x * torch.tanh(nn.functional.softplus(x))


def create_activation_module(name: Optional[str]) -> nn.Module:
    """Creates a layer activation module given its type as a string.

    Args:
        name: Which layer activation to use. Can be "relu", "leaky", "mish", "silu" (or "swish"), "logistic", "linear",
            or "none".
    """
    if name == "relu":
        return nn.ReLU(inplace=True)
    if name == "leaky":
        return nn.LeakyReLU(0.1, inplace=True)
    if name == "mish":
        return Mish()
    if name == "silu" or name == "swish":
        return nn.SiLU(inplace=True)
    if name == "logistic":
        return nn.Sigmoid()
    if name == "linear" or name == "none" or name is None:
        return nn.Identity()
    raise ValueError(f"Activation type `{name}´ is unknown.")


def create_normalization_module(name: Optional[str], num_channels: int) -> nn.Module:
    """Creates a layer normalization module given its type as a string.

    Group normalization uses always 8 channels. The most common network widths are divisible by this number.

    Args:
        name: Which layer normalization to use. Can be "batchnorm", "groupnorm", or "none".
        num_channels: The number of input channels that the module expects.
    """
    if name == "batchnorm":
        return nn.BatchNorm2d(num_channels, eps=0.001)
    if name == "groupnorm":
        return nn.GroupNorm(8, num_channels, eps=0.001)
    if name == "none" or name is None:
        return nn.Identity()
    raise ValueError(f"Normalization layer type `{name}´ is unknown.")


def create_detection_layer(
    prior_shapes: Sequence[Tuple[int, int]],
    prior_shape_idxs: Sequence[int],
    matching_algorithm: Optional[str] = None,
    matching_threshold: Optional[float] = None,
    ignore_bg_threshold: float = 0.7,
    overlap_func: Union[str, Callable] = "ciou",
    predict_overlap: float = 1.0,
    overlap_loss_multiplier: float = 5.0,
    confidence_loss_multiplier: float = 1.0,
    class_loss_multiplier: float = 1.0,
    **kwargs: Any,
) -> DetectionLayer:
    """Creates a detection layer module and the required loss function and target matching objects.

    Args:
        prior_shapes: A list of all the prior box dimensions, used for scaling the predicted dimensions and possibly for
            matching the targets to the anchors. The list should contain (width, height) tuples in the network input
            resolution.
        prior_shape_idxs: List of indices to ``prior_shapes`` that is used to select the (usually 3) prior shapes that
            this layer uses.
        matching_algorithm: Which algorithm to use for matching targets to anchors. "simota" (the SimOTA matching rule
            from YOLOX), "size" (match those prior shapes, whose width and height relative to the target is below given
            ratio), "iou" (match all prior shapes that give a high enough IoU), or "maxiou" (match the prior shape that
            gives the highest IoU, default).
        matching_threshold: Threshold for "size" and "iou" matching algorithms.
        ignore_bg_threshold: If a predictor is not responsible for predicting any target, but the corresponding anchor
            has IoU with some target greater than this threshold, the predictor will not be taken into account when
            calculating the confidence loss.
        overlap_func: A function for calculating the pairwise overlaps between two sets of boxes. Either a string or a
            function that returns a tensor with as many elements as there are input boxes. Valid values for a string are
            "iou", "giou", "diou", and "ciou" (default).
        predict_overlap: Balance between binary confidence targets and predicting the overlap. 0.0 means that target
            confidence is one if there's an object, and 1.0 means that the target confidence is the output of
            ``overlap_func``.
        overlap_loss_multiplier: Overlap loss will be scaled by this value.
        class_loss_multiplier: Classification loss will be scaled by this value.
        confidence_loss_multiplier: Confidence loss will be scaled by this value.
        xy_scale: Eliminate "grid sensitivity" by scaling the box coordinates by this factor. Using a value > 1.0 helps
            to produce coordinate values close to one.
    """
    matching_func: Union[ShapeMatching, SimOTAMatching]
    if matching_algorithm == "simota":
        loss_func = LossFunction(
            overlap_func, None, overlap_loss_multiplier, confidence_loss_multiplier, class_loss_multiplier
        )
        matching_func = SimOTAMatching(loss_func)
    elif matching_algorithm == "size":
        if matching_threshold is None:
            raise ValueError("matching_threshold is required with size ratio matching.")
        matching_func = SizeRatioMatching(prior_shapes, prior_shape_idxs, matching_threshold, ignore_bg_threshold)
    elif matching_algorithm == "iou":
        if matching_threshold is None:
            raise ValueError("matching_threshold is required with IoU threshold matching.")
        matching_func = IoUThresholdMatching(prior_shapes, prior_shape_idxs, matching_threshold, ignore_bg_threshold)
    elif matching_algorithm == "maxiou" or matching_algorithm is None:
        matching_func = HighestIoUMatching(prior_shapes, prior_shape_idxs, ignore_bg_threshold)
    else:
        raise ValueError(f"Matching algorithm `{matching_algorithm}´ is unknown.")

    loss_func = LossFunction(
        overlap_func, predict_overlap, overlap_loss_multiplier, confidence_loss_multiplier, class_loss_multiplier
    )

    layer_shapes = [prior_shapes[i] for i in prior_shape_idxs]
    return DetectionLayer(prior_shapes=layer_shapes, matching_func=matching_func, loss_func=loss_func, **kwargs)
