from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple, Union

import torch
from torch import Tensor
from torch.nn.functional import binary_cross_entropy, binary_cross_entropy_with_logits

from pl_bolts.utils import _TORCHVISION_AVAILABLE
from pl_bolts.utils.warnings import warn_missing_pkg

if not _TORCHVISION_AVAILABLE:
    warn_missing_pkg("torchvision")
else:
    from torchvision.ops import box_iou

    try:
        from torchvision.ops import generalized_box_iou
    except ImportError:
        generalized_box_iou = None
    try:
        from torchvision.ops import generalized_box_iou_loss
    except ImportError:
        generalized_box_iou_loss = None
    try:
        from torchvision.ops import distance_box_iou
    except ImportError:
        distance_box_iou = None
    try:
        from torchvision.ops import distance_box_iou_loss
    except ImportError:
        distance_box_iou_loss = None
    try:
        from torchvision.ops import complete_box_iou
    except ImportError:
        complete_box_iou = None
    try:
        from torchvision.ops import complete_box_iou_loss
    except ImportError:
        complete_box_iou_loss = None


def _size_compensation(targets: Tensor, image_size: Tensor) -> Tuple[Tensor, Tensor]:
    """Calcuates the size compensation factor for the overlap loss.

    The overlap losses for each target should be multiplied by the returned weight. The returned value is
    `2 - (unit_width * unit_height)`, which is large for small boxes (the maximum value is 2) and small for large boxes
    (the minimum value is 1).

    Args:
        targets: An ``[N, 4]`` matrix of target `(x1, y1, x2, y2)` coordinates.
        image_size: Image size, which is used to scale the target boxes to unit coordinates.

    Returns:
        The size compensation factor.
    """
    unit_wh = targets[:, 2:] / image_size
    return 2 - (unit_wh[:, 0] * unit_wh[:, 1])


def _pairwise_confidence_loss(
    preds: Tensor, overlap: Tensor, bce_func: Callable, predict_overlap: Optional[float]
) -> Tensor:
    """Calculates the confidence loss for every pair of a foreground anchor and a target.

    If ``predict_overlap`` is ``True``, ``overlap`` will be used as the target confidence. Otherwise the target
    confidence is 1. The method returns a matrix of losses for target/prediction pairs.

    Args:
        preds: An ``[N]`` vector of predicted confidences.
        overlap: An ``[M, N]`` matrix of overlaps between all target and predicted bounding boxes.
        bce_func: A function for calculating binary cross entropy.
        predict_overlap: Balance between binary confidence targets and predicting the overlap. 0.0 means that target
            confidence is one if there's an object, and 1.0 means that the target confidence is the overlap.

    Returns:
        An ``[M, N]`` matrix of confidence losses between all targets and predictions.
    """
    if predict_overlap is not None:
        # When predicting overlap, target confidence is different for each pair of a prediction and a target. The
        # tensors have to be broadcasted to [M, N].
        preds = preds.unsqueeze(0).expand(overlap.shape)
        targets = torch.ones_like(preds) - predict_overlap
        # Distance-IoU may return negative "overlaps", so we have to make sure that the targets are not negative.
        targets += predict_overlap * overlap.detach().clamp(min=0)
        return bce_func(preds, targets, reduction="none")
    else:
        # When not predicting overlap, target confidence is the same for every target, but we should still return a
        # matrix.
        targets = torch.ones_like(preds)
        return bce_func(preds, targets, reduction="none").unsqueeze(0).expand(overlap.shape)


def _foreground_confidence_loss(
    preds: Tensor, overlap: Tensor, bce_func: Callable, predict_overlap: Optional[float]
) -> Tensor:
    """Calculates the sum of the confidence losses for foreground anchors and their matched targets.

    If ``predict_overlap`` is ``True``, ``overlap`` will be used as the target confidence. Otherwise the target
    confidence is 1. The method returns a vector of losses for each foreground anchor.

    Args:
        preds: A vector of predicted confidences.
        overlap: A vector of overlaps between matched target and predicted bounding boxes.
        bce_func: A function for calculating binary cross entropy.
        predict_overlap: Balance between binary confidence targets and predicting the overlap. 0.0 means that target
            confidence is one if there's an object, and 1.0 means that the target confidence is the overlap.

    Returns:
        The sum of the confidence losses for foreground anchors.
    """
    targets = torch.ones_like(preds)
    if predict_overlap is not None:
        targets -= predict_overlap
        # Distance-IoU may return negative "overlaps", so we have to make sure that the targets are not negative.
        targets += predict_overlap * overlap.detach().clamp(min=0)
    return bce_func(preds, targets, reduction="sum")


def _background_confidence_loss(preds: Tensor, bce_func: Callable) -> Tensor:
    """Calculates the sum of the confidence losses for background anchors.

    Args:
        preds: A vector of predicted confidences for background anchors.
        bce_func: A function for calculating binary cross entropy.

    Returns:
        The sum of the background confidence losses.
    """
    targets = torch.zeros_like(preds)
    return bce_func(preds, targets, reduction="sum")


def _target_labels_to_probs(targets: Tensor, num_classes: int, dtype: torch.dtype) -> Tensor:
    """If ``targets`` is a vector of class labels, converts it to a matrix of one-hot class probabilities.

    Args:
        targets: An ``[M, C]`` matrix of target class probabilities or an ``[M]`` vector of class labels.
        num_classes: The number of classes (C dimension) for the new targets. If ``targets`` is already two-dimensional,
            checks that the length of the second dimension matches this number.
        dtype: Floating-point data type to be used for the one-hot targets.

    Returns:
        An ``[M, C]`` matrix of target class probabilities.
    """
    if targets.ndim == 1:
        # The data may contain a different number of classes than what the model predicts. In case a label is
        # greater than the number of predicted classes, it will be mapped to the last class.
        last_class = torch.tensor(num_classes - 1, device=targets.device)
        targets = torch.min(targets, last_class)
        targets = torch.nn.functional.one_hot(targets, num_classes)
    elif targets.shape[-1] != num_classes:
        raise ValueError(
            f"The number of classes in the data ({targets.shape[-1]}) doesn't match the number of classes "
            f"predicted by the model ({num_classes})."
        )
    return targets.to(dtype=dtype)


@dataclass
class Losses:
    overlap: Tensor
    confidence: Tensor
    classification: Tensor


class LossFunction:
    """A class for calculating the YOLO losses from predictions and targets.

    Args:
        overlap_func: A function for calculating the pairwise overlaps between two sets of boxes. Either a string or a
            function that returns a matrix of pairwise overlaps. Valid string values are "iou", "giou", "diou", and
            "ciou" (default).
        predict_overlap: Balance between binary confidence targets and predicting the overlap. 0.0 means that target
            confidence is one if there's an object, and 1.0 means that the target confidence is the output of
            ``overlap_func``.
        overlap_loss_multiplier: Overlap loss will be scaled by this value.
        confidence_loss_multiplier: Confidence loss will be scaled by this value.
        class_loss_multiplier: Classification loss will be scaled by this value.
    """

    def __init__(
        self,
        overlap_func: Union[str, Callable] = "ciou",
        predict_overlap: Optional[float] = None,
        overlap_multiplier: float = 5.0,
        confidence_multiplier: float = 1.0,
        class_multiplier: float = 1.0,
    ):
        overlap_loss_func = None
        if overlap_func == "iou":
            overlap_func = box_iou
        elif overlap_func == "giou":
            overlap_func = generalized_box_iou
            overlap_loss_func = generalized_box_iou_loss
        elif overlap_func == "diou":
            overlap_func = distance_box_iou
            overlap_loss_func = distance_box_iou_loss
        elif overlap_func == "ciou":
            overlap_func = complete_box_iou
            overlap_loss_func = complete_box_iou_loss

        if not callable(overlap_func):
            raise ValueError(
                f"Unsupported overlap function '{overlap_func}'. Try upgrading Torcvision or using another IoU "
                "algorithm."
            )
        self._pairwise_overlap = overlap_func

        if callable(overlap_loss_func):
            self._elementwise_overlap_loss = overlap_loss_func
        else:
            self._elementwise_overlap_loss = lambda boxes1, boxes2: 1.0 - overlap_func(boxes1, boxes2).diagonal()

        self.predict_overlap = predict_overlap
        self.overlap_multiplier = overlap_multiplier
        self.confidence_multiplier = confidence_multiplier
        self.class_multiplier = class_multiplier

    def pairwise(
        self,
        preds: Dict[str, Tensor],
        targets: Dict[str, Tensor],
        input_is_normalized: bool,
    ) -> Tuple[Losses, Tensor]:
        """Calculates matrices containing the losses for all prediction/target pairs.

        This method is called for obtaining costs for SimOTA matching.

        Args:
            preds: A dictionary of predictions, containing "boxes", "confidences", and "classprobs".
            targets: A dictionary of training targets, containing "boxes" and "labels".
            input_is_normalized: If ``False``, input is logits, if ``True``, input is normalized to `0..1`.

        Returns:
            Loss matrices and an overlap matrix.
        """
        if input_is_normalized:
            bce_func = binary_cross_entropy
        else:
            bce_func = binary_cross_entropy_with_logits

        overlap = self._pairwise_overlap(targets["boxes"], preds["boxes"])
        overlap_loss = 1.0 - overlap

        confidence_loss = _pairwise_confidence_loss(preds["confidences"], overlap, bce_func, self.predict_overlap)

        pred_probs = preds["classprobs"].unsqueeze(0)  # [1, preds, classes]
        target_probs = _target_labels_to_probs(targets["labels"], pred_probs.shape[-1], pred_probs.dtype)
        target_probs = target_probs.unsqueeze(1)  # [targets, 1, classes]
        pred_probs, target_probs = torch.broadcast_tensors(pred_probs, target_probs)
        class_loss = bce_func(pred_probs, target_probs, reduction="none").sum(-1)

        losses = Losses(
            overlap_loss * self.overlap_multiplier,
            confidence_loss * self.confidence_multiplier,
            class_loss * self.class_multiplier,
        )

        return losses, overlap

    def elementwise_sums(
        self,
        preds: Dict[str, Tensor],
        targets: Dict[str, Tensor],
        input_is_normalized: bool,
        image_size: Tensor,
    ) -> Losses:
        """Calculates the sums of the losses for optimization, over prediction/target pairs, assuming the
        predictions and targets have been matched (there are as many predictions and targets).

        Args:
            preds: A dictionary of predictions, containing "boxes", "confidences", and "classprobs".
            targets: A dictionary of training targets, containing "boxes" and "labels".
            input_is_normalized: If ``False``, input is logits, if ``True``, input is normalized to `0..1`.
            image_size: Width and height in a vector that defines the scale of the target coordinates.

        Returns:
            The final losses.
        """
        if input_is_normalized:
            bce_func = binary_cross_entropy
        else:
            bce_func = binary_cross_entropy_with_logits

        overlap_loss = self._elementwise_overlap_loss(targets["boxes"], preds["boxes"])
        overlap = 1.0 - overlap_loss
        overlap_loss = (overlap_loss * _size_compensation(targets["boxes"], image_size)).sum()

        confidence_loss = _foreground_confidence_loss(preds["confidences"], overlap, bce_func, self.predict_overlap)
        confidence_loss += _background_confidence_loss(preds["bg_confidences"], bce_func)

        pred_probs = preds["classprobs"]
        target_probs = _target_labels_to_probs(targets["labels"], pred_probs.shape[-1], pred_probs.dtype)
        class_loss = bce_func(pred_probs, target_probs, reduction="sum")

        losses = Losses(
            overlap_loss * self.overlap_multiplier,
            confidence_loss * self.confidence_multiplier,
            class_loss * self.class_multiplier,
        )

        return losses