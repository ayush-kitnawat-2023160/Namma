#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
import torch
import yaml
from PIL import Image
from torchvision.ops import nms
from transformers import (
    AutoImageProcessor,
    AutoModelForZeroShotObjectDetection,
    AutoProcessor,
    Mask2FormerForUniversalSegmentation,
    Sam2Model,
    Sam2Processor,
)


NEXTGEN_ROOT = Path("/workspace/nextgen")
SEMANTIC_ROOT = Path("/workspace/semantic")
DEFAULT_CONFIG = Path("/opt/namma-semantic/config/default.yaml")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}

# 0 is deliberately left for unlabeled/background so later 3D fusion can
# distinguish "not one of our target classes" from a real structural class.
SEMANTIC_IDS = {
    "background": 0,
    "floor": 1,
    "wall": 2,
    "ceiling": 3,
    "pillar": 4,
    "door": 5,
}

PALETTE = {
    "floor": (65, 180, 75),
    "wall": (230, 120, 50),
    "ceiling": (180, 180, 180),
    "pillar": (180, 80, 200),
    "door": (40, 160, 220),
    "chair": (50, 90, 230),
    "table": (220, 180, 40),
    "sofa": (200, 70, 150),
    "cabinet": (80, 200, 200),
}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_label(value: str) -> str:
    return " ".join(value.strip().lower().replace("_", " ").split())


def load_config() -> dict:
    with DEFAULT_CONFIG.open() as handle:
        return yaml.safe_load(handle) or {}


def flatten_prompt_aliases(mapping: dict[str, list[str]]) -> tuple[list[str], dict[str, str]]:
    prompts: list[str] = []
    alias_to_class: dict[str, str] = {}
    for canonical, aliases in mapping.items():
        for alias in aliases:
            normalized = normalize_label(str(alias))
            if normalized not in alias_to_class:
                prompts.append(normalized)
            alias_to_class[normalized] = canonical
    return prompts, alias_to_class


def canonical_from_text(value: object, alias_to_class: dict[str, str]) -> str | None:
    if not isinstance(value, str):
        return None
    text = normalize_label(value)
    if text in alias_to_class:
        return alias_to_class[text]
    # Grounding DINO may return phrases such as "a wooden chair". Prefer the
    # longest matching alias to avoid overly broad substring matches.
    matches = [alias for alias in alias_to_class if alias in text]
    if not matches:
        return None
    return alias_to_class[max(matches, key=len)]


def move_batch_to_device(batch, device: str):
    if hasattr(batch, "to"):
        return batch.to(device)
    return {
        key: value.to(device) if torch.is_tensor(value) else value
        for key, value in batch.items()
    }


def select_images(all_images: list[Path], limit: int) -> list[Path]:
    if limit <= 0 or limit >= len(all_images):
        return all_images
    indices = np.linspace(0, len(all_images) - 1, limit).round().astype(int)
    return [all_images[int(index)] for index in sorted(set(indices.tolist()))]


def structural_alias_map(stuff_cfg: dict[str, list[str]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for canonical, values in stuff_cfg.items():
        if canonical not in SEMANTIC_IDS:
            continue
        for value in values:
            aliases[normalize_label(str(value))] = canonical
    return aliases


def run_stuff_segmentation(
    images: list[Path],
    output: Path,
    model_id: str,
    stuff_cfg: dict[str, list[str]],
    device: str,
) -> dict[str, int]:
    print(f"[stuff] loading {model_id}")
    processor = AutoImageProcessor.from_pretrained(model_id)
    model = Mask2FormerForUniversalSegmentation.from_pretrained(model_id).eval().to(device)

    aliases = structural_alias_map(stuff_cfg)
    id2label = {
        int(key): normalize_label(str(value))
        for key, value in model.config.id2label.items()
    }
    model_to_target = {
        source_id: aliases[label]
        for source_id, label in id2label.items()
        if label in aliases
    }
    if not model_to_target:
        raise RuntimeError("None of the configured structural labels exist in the stuff model.")
    print(
        "[stuff] mapped labels:",
        {id2label[source_id]: target for source_id, target in model_to_target.items()},
    )

    semantic_dir = output / "semantic"
    semantic_dir.mkdir(parents=True, exist_ok=True)
    pixels = Counter()

    for index, path in enumerate(images, start=1):
        image = Image.open(path).convert("RGB")
        inputs = move_batch_to_device(
            processor(images=image, return_tensors="pt"), device
        )
        with torch.inference_mode():
            raw = model(**inputs)
        semantic = processor.post_process_semantic_segmentation(
            raw,
            target_sizes=[(image.height, image.width)],
        )[0].detach().cpu().numpy()

        target = np.zeros((image.height, image.width), dtype=np.uint8)
        for source_id, canonical in model_to_target.items():
            target[semantic == source_id] = SEMANTIC_IDS[canonical]
        cv2.imwrite(str(semantic_dir / f"{path.stem}.png"), target)

        for canonical, target_id in SEMANTIC_IDS.items():
            if target_id == 0:
                continue
            pixels[canonical] += int(np.count_nonzero(target == target_id))

        if index == 1 or index % 20 == 0 or index == len(images):
            print(f"[stuff] {index}/{len(images)} {path.name}")

    del model, processor
    torch.cuda.empty_cache()
    return dict(pixels)


def postprocess_grounding(
    processor,
    outputs,
    inputs,
    image: Image.Image,
    box_threshold: float,
    text_threshold: float,
    prompts: list[str],
):
    kwargs = {
        "threshold": box_threshold,
        "text_threshold": text_threshold,
        "target_sizes": [(image.height, image.width)],
        "text_labels": [prompts],
    }
    try:
        return processor.post_process_grounded_object_detection(
            outputs,
            inputs.input_ids,
            **kwargs,
        )[0]
    except TypeError:
        return processor.post_process_grounded_object_detection(
            outputs,
            input_ids=inputs.input_ids,
            **kwargs,
        )[0]


def deduplicate_detections(detections: list[dict], iou_threshold: float) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for detection in detections:
        grouped[detection["class"]].append(detection)

    kept: list[dict] = []
    for values in grouped.values():
        boxes = torch.tensor([value["box"] for value in values], dtype=torch.float32)
        scores = torch.tensor([value["score"] for value in values], dtype=torch.float32)
        for idx in nms(boxes, scores, iou_threshold).tolist():
            kept.append(values[idx])
    kept.sort(key=lambda item: item["score"], reverse=True)
    return kept


def normalize_sam_masks(value: torch.Tensor | np.ndarray) -> np.ndarray:
    if torch.is_tensor(value):
        value = value.detach().cpu().numpy()
    masks = np.asarray(value)
    masks = np.squeeze(masks)
    if masks.ndim == 2:
        masks = masks[None, ...]
    if masks.ndim != 3:
        raise RuntimeError(
            f"Unexpected SAM2 mask shape after post-processing: {masks.shape}"
        )
    # SAM2 post-processed masks are logits; zero is the standard decision
    # boundary. This also works if a future processor returns booleans.
    return masks > 0.0


def overlay_frame(
    image_bgr: np.ndarray,
    semantic: np.ndarray,
    detections: list[dict],
    alpha: float,
) -> np.ndarray:
    result = image_bgr.copy()
    tint = result.copy()
    for class_name, target_id in SEMANTIC_IDS.items():
        if target_id == 0:
            continue
        mask = semantic == target_id
        if np.any(mask):
            tint[mask] = PALETTE[class_name]
    result = cv2.addWeighted(tint, alpha, result, 1.0 - alpha, 0)

    for detection in detections:
        mask = detection["_mask"]
        class_name = detection["class"]
        color = PALETTE.get(class_name, (255, 255, 255))
        object_tint = result.copy()
        object_tint[mask] = color
        result = cv2.addWeighted(object_tint, alpha, result, 1.0 - alpha, 0)
        x1, y1, x2, y2 = [int(round(value)) for value in detection["box"]]
        cv2.rectangle(result, (x1, y1), (x2, y2), color, 2)
        label = (
            f"{detection['local_id']}:{class_name} "
            f"{detection['score']:.2f}"
        )
        cv2.putText(
            result,
            label,
            (max(0, x1), max(18, y1 - 5)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            color,
            1,
            cv2.LINE_AA,
        )
    return result


def run_instances(
    images: list[Path],
    output: Path,
    detector_id: str,
    sam2_id: str,
    prompt_cfg: dict[str, list[str]],
    box_threshold: float,
    text_threshold: float,
    nms_iou: float,
    min_mask_area: int,
    overlay_alpha: float,
    device: str,
) -> tuple[list[dict], Counter]:
    prompts, alias_to_class = flatten_prompt_aliases(prompt_cfg)
    print(f"[objects] prompts: {prompts}")
    print(f"[objects] loading detector {detector_id}")
    detector_processor = AutoProcessor.from_pretrained(detector_id)
    detector = (
        AutoModelForZeroShotObjectDetection.from_pretrained(detector_id)
        .eval()
        .to(device)
    )
    print(f"[objects] loading SAM2 {sam2_id}")
    sam_processor = Sam2Processor.from_pretrained(sam2_id)
    sam_model = Sam2Model.from_pretrained(sam2_id).eval().to(device)

    masks_root = output / "masks"
    instances_root = output / "instances"
    instance_maps_root = output / "instance_maps"
    overlays_root = output / "overlays"
    for directory in [masks_root, instances_root, instance_maps_root, overlays_root]:
        directory.mkdir(parents=True, exist_ok=True)

    all_frames: list[dict] = []
    class_counts = Counter()

    for frame_index, path in enumerate(images, start=1):
        image = Image.open(path).convert("RGB")
        det_inputs = detector_processor(
            images=image,
            text=[prompts],
            return_tensors="pt",
        )
        det_inputs = move_batch_to_device(det_inputs, device)
        with torch.inference_mode():
            det_outputs = detector(**det_inputs)

        result = postprocess_grounding(
            detector_processor,
            det_outputs,
            det_inputs,
            image,
            box_threshold,
            text_threshold,
            prompts,
        )
        label_values = result.get("text_labels", result.get("labels", []))

        candidates: list[dict] = []
        for box, score, raw_label in zip(
            result["boxes"],
            result["scores"],
            label_values,
        ):
            raw_value = (
                raw_label.item()
                if hasattr(raw_label, "item") and not isinstance(raw_label, str)
                else raw_label
            )
            canonical = canonical_from_text(raw_value, alias_to_class)
            if canonical is None:
                continue
            candidates.append(
                {
                    "class": canonical,
                    "raw_label": str(raw_value),
                    "score": float(score.detach().cpu().item()),
                    "box": [
                        float(value)
                        for value in box.detach().cpu().tolist()
                    ],
                }
            )

        detections = deduplicate_detections(candidates, nms_iou)
        frame_mask_dir = masks_root / path.stem
        frame_mask_dir.mkdir(parents=True, exist_ok=True)
        final_detections: list[dict] = []

        if detections:
            boxes = [detection["box"] for detection in detections]
            sam_inputs = sam_processor(
                images=image,
                input_boxes=[boxes],
                return_tensors="pt",
            )
            original_sizes = sam_inputs["original_sizes"].clone()
            sam_inputs = move_batch_to_device(sam_inputs, device)
            with torch.inference_mode():
                sam_outputs = sam_model(**sam_inputs, multimask_output=False)
            masks = sam_processor.post_process_masks(
                sam_outputs.pred_masks.detach().cpu(),
                original_sizes,
            )[0]
            masks = normalize_sam_masks(masks)
            if len(masks) != len(detections):
                raise RuntimeError(
                    f"SAM2 returned {len(masks)} masks for "
                    f"{len(detections)} boxes in {path.name}."
                )

            for detection, mask in zip(detections, masks):
                area = int(np.count_nonzero(mask))
                if area < min_mask_area:
                    continue
                local_id = len(final_detections) + 1
                filename = f"{local_id:03d}_{detection['class']}.png"
                cv2.imwrite(
                    str(frame_mask_dir / filename),
                    mask.astype(np.uint8) * 255,
                )
                item = dict(detection)
                item.update(
                    {
                        "local_id": local_id,
                        "mask": str(Path("masks") / path.stem / filename),
                        "area_px": area,
                        "_mask": mask,
                    }
                )
                final_detections.append(item)
                class_counts[detection["class"]] += 1

        # The ID map is intentionally frame-local. Stage 2 will assign persistent
        # 3D instance IDs after fusing masks through the BA coordinate frame.
        instance_map = np.zeros((image.height, image.width), dtype=np.uint16)
        for item in final_detections:
            mask = item["_mask"] & (instance_map == 0)
            instance_map[mask] = np.uint16(item["local_id"])
        cv2.imwrite(str(instance_maps_root / f"{path.stem}.png"), instance_map)

        semantic_path = output / "semantic" / f"{path.stem}.png"
        semantic = cv2.imread(str(semantic_path), cv2.IMREAD_UNCHANGED)
        if semantic is None:
            raise RuntimeError(f"Missing structural mask: {semantic_path}")
        image_bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image_bgr is None:
            raise RuntimeError(f"Cannot read source image: {path}")
        overlay = overlay_frame(
            image_bgr,
            semantic,
            final_detections,
            overlay_alpha,
        )
        cv2.imwrite(
            str(overlays_root / f"{path.stem}.jpg"),
            overlay,
            [cv2.IMWRITE_JPEG_QUALITY, 92],
        )

        serializable = []
        for item in final_detections:
            serializable.append(
                {key: value for key, value in item.items() if key != "_mask"}
            )
        frame_record = {
            "image": path.name,
            "width": image.width,
            "height": image.height,
            "instances": serializable,
        }
        (instances_root / f"{path.stem}.json").write_text(
            json.dumps(frame_record, indent=2)
        )
        all_frames.append(frame_record)

        if (
            frame_index == 1
            or frame_index % 10 == 0
            or frame_index == len(images)
        ):
            print(
                f"[objects] {frame_index}/{len(images)} {path.name}: "
                f"{len(final_detections)} instances"
            )

    del detector, detector_processor, sam_model, sam_processor
    torch.cuda.empty_cache()
    return all_frames, class_counts


def make_contact_sheet(
    overlays_dir: Path,
    output_file: Path,
    max_images: int = 12,
) -> None:
    paths = sorted(overlays_dir.glob("*.jpg"))
    if not paths:
        return
    if len(paths) > max_images:
        indices = np.linspace(0, len(paths) - 1, max_images).round().astype(int)
        paths = [paths[int(index)] for index in indices]

    thumb_w = 320
    thumbs: list[np.ndarray] = []
    for path in paths:
        image = cv2.imread(str(path))
        if image is None:
            continue
        scale = thumb_w / image.shape[1]
        thumb = cv2.resize(
            image,
            (thumb_w, int(round(image.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
        cv2.putText(
            thumb,
            path.stem,
            (8, 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        thumbs.append(thumb)
    if not thumbs:
        return

    cols = min(4, len(thumbs))
    rows = math.ceil(len(thumbs) / cols)
    tile_h = max(thumb.shape[0] for thumb in thumbs)
    sheet = np.zeros((rows * tile_h, cols * thumb_w, 3), dtype=np.uint8)
    for idx, thumb in enumerate(thumbs):
        row, col = divmod(idx, cols)
        y0 = row * tile_h
        x0 = col * thumb_w
        sheet[y0 : y0 + thumb.shape[0], x0 : x0 + thumb.shape[1]] = thumb
    cv2.imwrite(
        str(output_file),
        sheet,
        [cv2.IMWRITE_JPEG_QUALITY, 92],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Namma Stage 1: structural semantics + object instance masks"
    )
    parser.add_argument("scene")
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("NS_SEGMENT_LIMIT", "0")),
        help="Evenly sample N views for a quick preview. 0 means all views.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=env_bool("NS_SEGMENT_OVERWRITE"),
    )
    args = parser.parse_args()

    if not torch.cuda.is_available():
        raise RuntimeError("Stage 1 segmentation requires CUDA.")

    config = load_config()["segmentation"]
    stuff_model = os.environ.get("NS_STUFF_MODEL", config["stuff_model"])
    detector_model = os.environ.get(
        "NS_DETECTOR_MODEL", config["detector_model"]
    )
    sam2_model = os.environ.get("NS_SAM2_MODEL", config["sam2_model"])
    box_threshold = float(
        os.environ.get("NS_BOX_THRESHOLD", config["box_threshold"])
    )
    text_threshold = float(
        os.environ.get("NS_TEXT_THRESHOLD", config["text_threshold"])
    )
    nms_iou = float(os.environ.get("NS_NMS_IOU", config["nms_iou"]))
    min_mask_area = int(
        os.environ.get("NS_MIN_MASK_AREA", config["min_mask_area"])
    )
    overlay_alpha = float(config.get("overlay_alpha", 0.45))

    source_manifest = SEMANTIC_ROOT / args.scene / "00_source/manifest.json"
    if not source_manifest.is_file():
        raise RuntimeError(
            f"Missing {source_manifest}. "
            f"Run './run_semantic.sh source {args.scene}' first."
        )

    source = NEXTGEN_ROOT / args.scene / "01_frames/images"
    if not source.is_dir():
        raise RuntimeError(f"Missing selected RGB directory: {source}")
    all_images = sorted(
        path for path in source.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES
    )
    if not all_images:
        raise RuntimeError(f"No RGB images found in {source}")
    images = select_images(all_images, args.limit)

    output = SEMANTIC_ROOT / args.scene / "01_segmentation"
    if output.exists():
        if not args.overwrite:
            raise RuntimeError(
                f"Segmentation output already exists: {output}. "
                "Use --overwrite to replace it."
            )
        shutil.rmtree(output)
    output.mkdir(parents=True)

    print("=== NAMMA STAGE 1: 2D SEGMENTATION ===")
    print(f"Scene:             {args.scene}")
    print(f"Images:            {len(images)}/{len(all_images)}")
    print(f"Stuff model:       {stuff_model}")
    print(f"Detector:          {detector_model}")
    print(f"SAM2:              {sam2_model}")
    print(f"Box/text thresh:   {box_threshold:.2f}/{text_threshold:.2f}")
    print(f"Output:            {output}")

    device = "cuda"
    stuff_pixels = run_stuff_segmentation(
        images,
        output,
        stuff_model,
        config["stuff_classes"],
        device,
    )
    frame_records, object_counts = run_instances(
        images,
        output,
        detector_model,
        sam2_model,
        config["object_prompts"],
        box_threshold,
        text_threshold,
        nms_iou,
        min_mask_area,
        overlay_alpha,
        device,
    )

    labels = {
        "semantic_ids": SEMANTIC_IDS,
        "object_classes": list(config["object_prompts"].keys()),
        "instance_ids": (
            "frame-local; persistent 3D IDs are assigned during Stage 2 fusion"
        ),
    }
    (output / "labels.json").write_text(json.dumps(labels, indent=2))
    (output / "detections.json").write_text(
        json.dumps({"frames": frame_records}, indent=2)
    )

    complete = len(images) == len(all_images)
    summary = {
        "scene": args.scene,
        "images_processed": len(images),
        "images_available": len(all_images),
        "complete_dataset": complete,
        "models": {
            "stuff": stuff_model,
            "detector": detector_model,
            "sam2": sam2_model,
        },
        "thresholds": {
            "box": box_threshold,
            "text": text_threshold,
            "nms_iou": nms_iou,
            "min_mask_area": min_mask_area,
        },
        "structural_pixels": stuff_pixels,
        "object_instances": dict(object_counts),
        "total_object_instances": int(sum(object_counts.values())),
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    make_contact_sheet(
        output / "overlays",
        output / "contact_sheet.jpg",
    )
    (output / (".complete" if complete else ".preview")).write_text("ok\n")

    print("=== STAGE 1 COMPLETE ===")
    print(json.dumps(summary, indent=2))
    print("Contact sheet:", output / "contact_sheet.jpg")
    if not complete:
        print(
            "[NOTE] This was a preview run. Re-run without --limit "
            "using --overwrite for all views."
        )


if __name__ == "__main__":
    main()
