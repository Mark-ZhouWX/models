import mindspore as ms
from mindspore import nn, ops

from typing import Any, Dict, List, Tuple

from .image_encoder import ImageEncoderViT
from .mask_decoder import MaskDecoder
from .prompt_encoder import PromptEncoder


class Sam(nn.Cell):
    mask_threshold: float = 0.0
    image_format: str = "RGB"

    def __init__(
        self,
        image_encoder: ImageEncoderViT,
        prompt_encoder: PromptEncoder,
        mask_decoder: MaskDecoder,
        pixel_mean: List[float] = [123.675, 116.28, 103.53],
        pixel_std: List[float] = [58.395, 57.12, 57.375],
    ) -> None:
        """
        SAM predicts object masks from an image and input prompts.

        Arguments:
          image_encoder (ImageEncoderViT): The backbone used to encode the
            image into image embeddings that allow for efficient mask prediction.
          prompt_encoder (PromptEncoder): Encodes various types of input prompts.
          mask_decoder (MaskDecoder): Predicts masks from the image embeddings
            and encoded prompts.
          pixel_mean (list(float)): Mean values for normalizing pixels in the input image.
          pixel_std (list(float)): Std values for normalizing pixels in the input image.
        """
        super().__init__()
        self.image_encoder = image_encoder
        self.prompt_encoder = prompt_encoder
        self.mask_decoder = mask_decoder
        self.pixel_mean = ms.Tensor(pixel_mean).view(-1, 1, 1)
        self.pixel_std = ms.Tensor(pixel_std).view(-1, 1, 1)

    def construct(
        self,
        image: ms.Tensor,
        boxes: ms.Tensor,
    ) -> Tuple[ms.Tensor]:
        """
        Predicts masks end-to-end from provided images and prompts. Currently, only boxe prompt is supported

        Arguments:

          image: The image as a ms tensor in Bx3xHxW format, already transformed for input to the model (1024x1024).
              'original_size': (tuple(int, int)) The original size of
                the image before transformation, as (H, W).

          boxes: (ms.Tensor) Batched box inputs, with shape BxNx4. N denotes the number of boxes within in one image.
            Already transformed to the input frame of the model.

        Returns:
          (list(ms.Tensor)): A list over input images, where each element is
            a tuple with the following elements.
              'masks': (ms.Tensor) Batched binary mask predictions,
                with shape BxNxHxW, where B is the number of input prompts,
                C is determined by multimask_output, and (H, W) is the
                original size of the image, and N is the number of boxes within in one image.
              'iou_predictions': (ms.Tensor) The model's predictions
                of mask quality, in shape BxC.
              'low_res_logits': (ms.Tensor) Low resolution logits with
                shape BxCxHxW, where H=W=256. Can be passed as mask input
                to subsequent iterations of prediction.
        """
        bs, _, h, w = image.shape
        image_embeddings = self.image_encoder(image)  # (b, c, h, w)

        pred_masks = []
        pred_ious = []
        for i in range(bs):
            box, curr_embedding = boxes[i], image_embeddings[i]
            # box (n, 4)  curr_embedding (c, h, w)
            sparse_embeddings, dense_embeddings = self.prompt_encoder(
                points=None,
                boxes=box,
                masks=None,
            )
            low_res_masks, iou_predictions = self.mask_decoder(
                image_embeddings=curr_embedding.unsqueeze(0),  # (1, c, h, w) will finally expand to (n, c, h, w)
                image_pe=self.prompt_encoder.get_dense_pe(),
                sparse_prompt_embeddings=sparse_embeddings,
                dense_prompt_embeddings=dense_embeddings,
                multimask_output=False,
            )
            # low_res_masks (n, 4, h, w) if multimask_output else (n, 1, h, w)
            # iou_predictions (n, 4) if multimask_output else (n, 1)
            masks = ops.interpolate(low_res_masks, (h, w), mode='bilinear', align_corners=False)

            pred_masks.append(masks)
            pred_ious.append(iou_predictions)

        # stack along batch dimension
        pred_masks = ops.stack(pred_masks).squeeze(2)  # -> (b, n, 1, h, w)  -> (b, n, h, w)
        pred_ious = ops.stack(pred_ious).squeeze(2)  # -> (b, n, 1) -> (b, n,)

        return pred_masks, pred_ious

    def postprocess_masks(
        self,
        masks: ms.Tensor,
        input_size: Tuple[int, ...],
        original_size: Tuple[int, ...],
    ) -> ms.Tensor:
        """
        Remove padding and upscale masks to the original image size.

        Arguments:
          masks (ms.Tensor): Batched masks from the mask_decoder,
            in BxCxHxW format.
          input_size (tuple(int, int)): The size of the image input to the
            model, in (H, W) format. Used to remove padding.
          original_size (tuple(int, int)): The original size of the image
            before resizing for input to the model, in (H, W) format.

        Returns:
          (ms.Tensor): Batched masks in BxCxHxW format, where (H, W)
            is given by original_size.
        """
        masks = ops.interpolate(
            masks,
            (self.image_encoder.img_size, self.image_encoder.img_size),
            mode="bilinear",
            align_corners=False,
        )
        masks = masks[..., : input_size[0], : input_size[1]]
        masks = ops.interpolate(masks, original_size, mode="bilinear", align_corners=False)
        return masks

    def preprocess(self, x: ms.Tensor) -> ms.Tensor:
        """Normalize pixel values and pad to a square input."""
        # Normalize colors
        x = (x - self.pixel_mean) / self.pixel_std

        # Pad
        h, w = x.shape[-2:]
        padh = self.image_encoder.img_size - h
        padw = self.image_encoder.img_size - w
        x = ops.pad(x, (0, padw, 0, padh))
        return x