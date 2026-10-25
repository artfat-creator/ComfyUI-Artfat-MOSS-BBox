"""Artfat MOSS BBox: MOSS-Image layout generation and box-only edits (Canvas + Sampler + BBoxes to Mask).

Two nodes on purpose: the LLM prompter sits between them. The canvas tells the prompter what the
boxes must hold (`brief`), the sampler takes the prompter's text and draws the frame. One node
would need the prompter's text before the prompter could read the boxes -- a cycle.

The canvas reuses the core Create Bounding Boxes widget (`io.BoundingBoxes`). The frontend finds
its sources by input/widget NAME (`background`, `bboxes`, `width`, `height`, `last_incoming`)
and by the ui keys `background_images` / `input_bboxes`, so those names must stay as they are.
"""

import json
import math
import re

import torch

import comfy.model_management
import comfy.samplers
import comfy.utils
import nodes
from comfy_api.latest import io
from comfy_extras import nodes_bounding_boxes as cbb
from comfy_extras.nodes_differential_diffusion import DifferentialDiffusion

LATENT_CHANNELS = 128  # flux2 VAE, as EmptyFlux2LatentImage
LATENT_DOWNSCALE = 16


def _scale_to_megapixels(image, megapixels, step=16):
    """The same math as core ImageScaleToTotalPixels (lanczos, multiple of `step`)."""
    samples = image.movedim(-1, 1)
    scale_by = math.sqrt(megapixels * 1024 * 1024 / (samples.shape[3] * samples.shape[2]))
    w = max(step, round(samples.shape[3] * scale_by / step) * step)
    h = max(step, round(samples.shape[2] * scale_by / step) * step)
    return comfy.utils.common_upscale(samples, int(w), int(h), "lanczos", "disabled").movedim(1, -1)


def _regions_mask(regions, width, height, grow, feather):
    """White inside the boxes, grown by `grow` px, then a linear fade over `feather` px.
    Core FeatherMask only fades the canvas borders, never the shape, hence our own falloff."""
    ys = torch.arange(height, dtype=torch.float32).view(-1, 1)
    xs = torch.arange(width, dtype=torch.float32).view(1, -1)
    m = torch.zeros((height, width), dtype=torch.float32)
    for r in regions:
        x0, y0 = r["x"] * width - grow, r["y"] * height - grow
        x1, y1 = (r["x"] + r["w"]) * width + grow, (r["y"] + r["h"]) * height + grow
        if x1 <= x0 or y1 <= y0:
            continue
        dx = (x0 - xs).clamp(min=0) + (xs - x1).clamp(min=0)
        dy = (y0 - ys).clamp(min=0) + (ys - y1).clamp(min=0)
        dist = torch.sqrt(dx * dx + dy * dy)
        box = (dist == 0).float() if feather <= 0 else (1.0 - dist / feather).clamp(0.0, 1.0)
        m = torch.maximum(m, box)
    return m.unsqueeze(0)


def _where(r):
    cx, cy = r["x"] + r["w"] / 2, r["y"] + r["h"] / 2
    v = "top" if cy < 1 / 3 else ("bottom" if cy > 2 / 3 else "middle")
    h = "left" if cx < 1 / 3 else ("right" if cx > 2 / 3 else "centre")
    place = "centre" if (v, h) == ("middle", "centre") else ("%s %s" % (v, h)).replace("middle ", "").replace(" centre", "")
    area = r["w"] * r["h"]
    size = "small" if area < 0.06 else ("large" if area > 0.3 else "medium")
    return "%s, %s" % (place, size)


def _boxes_text(regions, characters, editing):
    lines = []
    for i, r in enumerate(regions, 1):
        if r.get("type") == "text":
            what = 'the words "%s"' % (r.get("text") or "").strip()
            if (r.get("desc") or "").strip():
                what += " on " + r["desc"].strip()
            kind = "text"
        else:
            what = (r.get("desc") or "").strip() or "(no description)"
            kind = "character" if i in characters else "object"
        lines.append("%d. %s (%s): %s" % (i, kind, _where(r), what))
    if not lines:
        return ""
    head = ("[BOXES] The picture is being edited. Only these areas change, everything else stays as it is:"
            if editing else "[BOXES] Layout of the frame, each item at its place:")
    return head + "\n" + "\n".join(lines)


class ArtfatBBoxCanvas(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ArtfatBBoxCanvas",
            display_name="Artfat BBox Canvas",
            category="artfat",
            description="Draw boxes. Picture connected = edit only the boxes; nothing connected = generate a frame laid out by the boxes. "
                        "Wire `brief` into the prompter's brief and the rest into Artfat BBox Sampler.",
            inputs=[
                io.Image.Input("background", display_name="image", optional=True,
                               tooltip="The picture to edit. Leave empty to generate a new frame from the boxes."),
                io.MultiType.Input("bboxes", [io.BoundingBox, io.Array, io.String], optional=True,
                                   tooltip="Boxes to start the canvas from (optional)."),
                io.Int.Input("width", default=1152, min=64, max=16384, step=16,
                             tooltip="Frame width when generating. With a picture connected it follows the picture."),
                io.Int.Input("height", default=2048, min=64, max=16384, step=16,
                             tooltip="Frame height when generating. With a picture connected it follows the picture."),
                io.BoundingBoxes.Input("editor_state", socketless=False,
                                       tooltip="Draw the boxes. Per box: object or text, the words for a text box, and a description of what goes inside."),
                io.BoundingBoxes.Input("last_incoming", optional=True, tooltip="Internal canvas state."),
                io.String.Input("brief", multiline=True, default="",
                                tooltip="Your idea for the whole frame. Goes out on `brief` together with the box list, for the prompter."),
                io.String.Input("character_boxes", default="",
                                tooltip="Numbers of the object boxes that hold the LoRA character, e.g. \"1, 3\". The prompter will not describe their look."),
                io.Float.Input("megapixels", default=2.0, min=0.25, max=8.0, step=0.05,
                               tooltip="Edit size: the picture is scaled to this many megapixels (multiple of 16)."),
                io.Int.Input("grow", default=16, min=0, max=512, tooltip="Edit: the redrawn area reaches this many px past each box."),
                io.Int.Input("feather", default=64, min=0, max=1024,
                             tooltip="Edit: soft edge in px past the grown box. 0 = hard edge, a visible seam."),
                # Sockets only: the core canvas hides width/height whenever `background` is wired, so a
                # resolution node (Artfat Resolution) comes in here. Added last to keep saved widget order.
                io.Int.Input("res_width", display_name="res width", optional=True, force_input=True, min=64, max=16384,
                             tooltip="Optional, from Artfat Resolution. Generate: the frame width. Edit: with res height "
                                     "it sets the size (width x height pixels), the picture keeps its own proportions."),
                io.Int.Input("res_height", display_name="res height", optional=True, force_input=True, min=64, max=16384,
                             tooltip="Optional, from Artfat Resolution. Generate: the frame height. Edit: see res width."),
                # Last again, for the same reason. The switch sits in the canvas toolbar (artfat_bbox.js).
                io.Boolean.Input("bbox_mode", default=True, optional=True, label_on="bbox generation", label_off="simple generation",
                                 tooltip="ON: the boxes, the picture and this brief are used. OFF: a plain new frame -- "
                                         "boxes, picture and brief are left out, the prompter writes from its own brief."),
            ],
            outputs=[
                io.Image.Output(display_name="image"),
                io.Mask.Output(display_name="mask"),
                io.String.Output(display_name="brief"),
                io.Array.Output(display_name="layout"),
                io.Int.Output(display_name="width"),
                io.Int.Output(display_name="height"),
                io.Image.Output(display_name="preview"),
                io.BoundingBox.Output(display_name="bboxes"),
            ],
            is_output_node=True,
            is_experimental=True,
        )

    @classmethod
    def execute(cls, width, height, editor_state=None, last_incoming=None, background=None, bboxes=None,
                brief="", character_boxes="", megapixels=2.0, grow=16, feather=64,
                res_width=None, res_height=None, bbox_mode=True) -> io.NodeOutput:
        # Same source rule as core Create Bounding Boxes: a new upstream value wins once, canvas edits after that.
        # Boxes are in canvas pixels (width x height), so the fractions come from the canvas size.
        incoming = cbb.boxes_from_input(bboxes, width, height)
        applied = last_incoming if isinstance(last_incoming, list) else []
        source = incoming if (incoming and incoming != applied) else (editor_state or [])
        regions = cbb.boxes_to_regions(source, width, height)  # fractions of the canvas
        if not bbox_mode:  # simple generation: nothing from the canvas reaches the prompter or the sampler
            regions, background, brief = [], None, ""

        # res width/height wired: generate = exactly that frame; edit = that many pixels, picture's proportions.
        sized = bool(res_width and res_height)
        editing = background is not None and len(background) > 0
        if editing and sized:
            megapixels = res_width * res_height / (1024 * 1024)
        image = _scale_to_megapixels(background[:1, :, :, :3], megapixels) if editing else None
        if editing:
            W, H = image.shape[2], image.shape[1]
        elif sized:
            W, H = (max(16, round(v / 16) * 16) for v in (res_width, res_height))
        else:
            W, H = width, height

        characters = {int(i) for i in re.findall(r"\d+", character_boxes or "")}
        layout = cbb.build_elements(regions)
        for i, el in enumerate(layout, 1):
            if el.get("type") != "text" and i in characters:
                el["character"] = True
        mask = _regions_mask(regions, W, H, grow, feather) if editing else torch.zeros((1, H, W))
        text = "\n\n".join(t for t in ((brief or "").strip(), _boxes_text(regions, characters, editing)) if t)
        preview = cbb.render_preview(regions, W, H, cbb._bg_from_image(image))

        ui = {"dims": [W, H]}  # artfat_bbox.js copies these into the hidden width/height so the canvas shape follows
        if incoming:
            ui["input_bboxes"] = incoming
        if editing:
            ui["background_images"] = nodes.PreviewImage().save_images(background[:1], "comfy.bboxes.background")["ui"]["images"]
        return io.NodeOutput(image, mask, text, layout, W, H, preview,
                             cbb.fractions_to_bbox_frame(regions, W, H), ui=ui)


def _layout_prompt(prompt, layout, width, height):
    """MOSS layout JSON, the same compile as core MOSSImageLayoutPrompt (abs_centralized, yxyx)."""
    items = []
    for el in layout if isinstance(layout, list) else []:
        label = "text" if el.get("type") == "text" else ("character" if el.get("character") else "object")
        item = {"label": label, "caption": el.get("desc", "")}
        if label == "text":
            item["text"] = el.get("text", "")
        if el.get("color_palette"):
            item["palette"] = el["color_palette"]
        y0, x0, y1, x1 = el["bbox"]  # 0-1000 grid
        item["bbox"] = [math.floor(round(y0 * height / 1000, 9)) - height // 2, math.floor(round(x0 * width / 1000, 9)) - width // 2,
                        math.ceil(round(y1 * height / 1000, 9)) - height // 2, math.ceil(round(x1 * width / 1000, 9)) - width // 2]
        items.append(item)
    if not items:
        return prompt
    return json.dumps({"summary": {"caption": prompt.strip()}, "items": items,
                       "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx"}, ensure_ascii=False, separators=(",", ":"))


def _encode(clip, text):
    return clip.encode_from_tokens_scheduled(clip.tokenize(text))


class ArtfatBBoxSampler(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        samplers, schedulers = comfy.samplers.KSampler.SAMPLERS, comfy.samplers.KSampler.SCHEDULERS
        return io.Schema(
            node_id="ArtfatBBoxSampler",
            display_name="Artfat BBox Sampler",
            category="artfat",
            description="Draws the frame from the prompt + Artfat BBox Canvas boxes. Image connected = redraws only the masked boxes "
                        "and pastes them back (outside the mask untouched); no image = a new frame.",
            inputs=[
                io.Model.Input("model", tooltip="The model with your LoRAs already applied."),
                io.Clip.Input("clip"),
                io.Vae.Input("vae"),
                io.String.Input("prompt", multiline=True, default="",
                                tooltip="The whole frame as it must look in the end (the prompter's `prompt`). Edit: describe the frame AFTER the change."),
                io.Array.Input("layout", optional=True, tooltip="`layout` from Artfat BBox Canvas."),
                io.Image.Input("image", optional=True, tooltip="`image` from Artfat BBox Canvas. Empty = generate."),
                io.Mask.Input("mask", optional=True, tooltip="`mask` from Artfat BBox Canvas. Edit without a mask redraws the whole picture."),
                io.Int.Input("width", default=1152, min=16, max=16384, step=16, tooltip="Generate: frame width (`width` from the canvas)."),
                io.Int.Input("height", default=2048, min=16, max=16384, step=16, tooltip="Generate: frame height (`height` from the canvas)."),
                io.Int.Input("seed", default=0, min=0, max=0xffffffffffffffff, control_after_generate=True),
                io.Int.Input("steps", default=8, min=1, max=200),
                io.Float.Input("cfg", default=1.0, min=0.0, max=30.0, step=0.1, round=0.01),
                io.Combo.Input("sampler_name", options=samplers, default="res_6s" if "res_6s" in samplers else "euler"),
                io.Combo.Input("scheduler", options=schedulers, default="bong_tangent" if "bong_tangent" in schedulers else "simple"),
                io.Int.Input("start_step", default=0, min=0, max=200,
                             tooltip="Edit only: skip the first steps to keep more of the old box content. 0 = full redraw (the box takes the new thing)."),
                io.Conditioning.Input("positive", optional=True,
                                      tooltip="Optional: your own positive (e.g. after MOSS depth control). Replaces the prompt + layout encode; build it from `layout_prompt`."),
                io.Conditioning.Input("negative", optional=True),
            ],
            outputs=[
                io.Image.Output(display_name="IMAGE"),
                io.Latent.Output(display_name="LATENT"),
                io.String.Output(display_name="layout_prompt"),
            ],
        )

    @classmethod
    def execute(cls, model, clip, vae, prompt, width, height, seed, steps, cfg, sampler_name, scheduler, start_step,
                layout=None, image=None, mask=None, positive=None, negative=None) -> io.NodeOutput:
        editing = image is not None
        if editing:
            image = image[:, :, :, :3]
            height, width = image.shape[1], image.shape[2]
            if mask is not None and float(mask.max()) <= 0.0:
                # No boxes drawn yet: nothing to redraw, the picture goes through as it is (fast).
                ArtfatEditSource.remember(image)
                return io.NodeOutput(image, {"samples": vae.encode(image)}, prompt or "")
        text = _layout_prompt(prompt or "", layout, width, height)
        pos = positive if positive is not None else _encode(clip, text)
        neg = negative if negative is not None else _encode(clip, "")

        if editing:
            if mask is None:
                mask = torch.ones((1, height, width))
            mask = mask.reshape((-1, 1, mask.shape[-2], mask.shape[-1]))
            if mask.shape[-2:] != (height, width):
                mask = torch.nn.functional.interpolate(mask, size=(height, width), mode="bilinear")
            latent = {"samples": vae.encode(image), "noise_mask": mask}
            m = model.clone()  # = DifferentialDiffusion node: the soft mask edge becomes a soft edit
            m.set_model_denoise_mask_function(lambda *a, **k: DifferentialDiffusion.forward(*a, **k, strength=1.0))
            model, first = m, min(start_step, steps - 1)
        else:
            latent = {"samples": torch.zeros([1, LATENT_CHANNELS, height // LATENT_DOWNSCALE, width // LATENT_DOWNSCALE],
                                             device=comfy.model_management.intermediate_device())}
            first = 0

        out = nodes.common_ksampler(model, seed, steps, cfg, sampler_name, scheduler, pos, neg, latent,
                                    start_step=first, last_step=steps, force_full_denoise=True)[0]
        decoded = vae.decode(out["samples"])
        if decoded.ndim == 5:
            decoded = decoded.reshape(-1, decoded.shape[-3], decoded.shape[-2], decoded.shape[-1])
        if editing:
            if decoded.shape[1:3] != image.shape[1:3]:
                decoded = comfy.utils.common_upscale(decoded.movedim(-1, 1), width, height, "bilinear", "disabled").movedim(1, -1)
            alpha = mask.movedim(1, -1).to(decoded.device)  # [B,H,W,1]; pixels outside the mask stay the original ones
            decoded = image.to(decoded.device) * (1.0 - alpha) + decoded * alpha
        out.pop("noise_mask", None)
        ArtfatEditSource.remember(decoded)
        return io.NodeOutput(decoded, out, text)


class ArtfatBBoxesToMask:
    """Rectangles from Create Bounding Boxes as a mask (white inside the boxes), for a box-only redraw."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "bboxes": ("BOUNDING_BOX", {"tooltip": "The `bboxes` output of Create Bounding Boxes (pixel boxes)."}),
                "width": ("INT", {"default": 1024, "min": 16, "max": 16384, "step": 1,
                                  "tooltip": "Mask width: the same canvas width the boxes were drawn on."}),
                "height": ("INT", {"default": 1024, "min": 16, "max": 16384, "step": 1,
                                   "tooltip": "Mask height: the same canvas height the boxes were drawn on."}),
                "feather": ("INT", {"default": 64, "min": 0, "max": 1024, "step": 1,
                                    "tooltip": "Soft edge in pixels, fading outward from each box. 0 = hard edge (a visible seam). "
                                               "Wider hides the seam better but lets the redraw spill further around the box."}),
            },
        }

    RETURN_TYPES = ("MASK",)
    RETURN_NAMES = ("mask",)
    FUNCTION = "build"
    CATEGORY = "artfat"

    def build(self, bboxes, width, height, feather=64):
        # Create Bounding Boxes outputs one list of boxes per frame; a bare list or a single box also works.
        frames = bboxes if isinstance(bboxes, list) else [bboxes]
        if frames and isinstance(frames[0], dict):
            frames = [frames]
        ys = torch.arange(height, dtype=torch.float32).view(-1, 1)
        xs = torch.arange(width, dtype=torch.float32).view(1, -1)
        masks = []
        for frame in frames or [[]]:
            m = torch.zeros((height, width), dtype=torch.float32)
            for b in frame or []:
                x0, y0 = int(b.get("x", 0)), int(b.get("y", 0))
                x1, y1 = x0 + int(b.get("width", 0)), y0 + int(b.get("height", 0))
                if x1 <= x0 or y1 <= y0:
                    continue
                # Distance from the box (0 inside), turned into a linear fade over `feather` pixels.
                dx = (x0 - xs).clamp(min=0) + (xs - (x1 - 1)).clamp(min=0)
                dy = (y0 - ys).clamp(min=0) + (ys - (y1 - 1)).clamp(min=0)
                dist = torch.sqrt(dx * dx + dy * dy)
                box = (dist == 0).float() if feather <= 0 else (1.0 - dist / feather).clamp(0.0, 1.0)
                m = torch.maximum(m, box)
            masks.append(m)
        return (torch.stack(masks),)


class ArtfatEditSource(io.ComfyNode):
    """The picture for the canvas: a new frame, your own photo, or the last frame the BBox Sampler drew.
    A wire from the sampler back into the canvas would be a loop, so the sampler stores its
    result here and this node hands it to the canvas on the next run."""

    _last = None   # last frame from any Artfat BBox Sampler (CPU); lives until ComfyUI restarts
    _count = 0
    SOURCES = ["new image", "my photo", "edit last result"]

    @classmethod
    def remember(cls, image):
        cls._last = image.detach().cpu().clone()
        cls._count += 1

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ArtfatEditSource",
            display_name="Artfat Edit Source",
            category="artfat",
            description="Wire into the canvas `image`. new image: the canvas generates a new frame (Load Image is not read). "
                        "my photo: your Load Image goes to the canvas. edit last result: the frame the BBox Sampler drew last "
                        "comes back into the canvas - draw boxes on it and run to edit it.",
            inputs=[
                io.Image.Input("image", optional=True, lazy=True,
                               tooltip="Your own photo (Load Image). Only read when source = my photo."),
                io.Combo.Input("source", options=cls.SOURCES, default="new image",
                               tooltip="new image = generate a new frame. my photo = edit the Load Image picture. "
                                       "edit last result = edit the frame the BBox Sampler drew last; every run then edits the newest result."),
            ],
            outputs=[io.Image.Output(display_name="image")],
            is_output_node=True,
        )

    @classmethod
    def check_lazy_status(cls, source="new image", image=None):
        if source == "my photo" and image is None:
            return ["image"]
        return []

    @classmethod
    def fingerprint_inputs(cls, source="new image", image=None):
        return cls._count if source == "edit last result" else source

    @classmethod
    def execute(cls, source="new image", image=None) -> io.NodeOutput:
        if source == "edit last result":
            out = cls._last
            if out is None:
                raise ValueError("Artfat Edit Source: nothing generated yet. Set source to `new image` and run once.")
        elif source == "my photo":
            out = image
            if out is None:
                raise ValueError("Artfat Edit Source: source is `my photo` but no picture comes in. Wire a Load Image into `image`.")
        else:
            return io.NodeOutput(None)
        preview = nodes.PreviewImage().save_images(out[:1], "artfat.edit_source")["ui"]
        return io.NodeOutput(out, ui=preview)


class ArtfatFrameHold(io.ComfyNode):
    """Keeps the last frame so you can draw boxes on it and edit it without generating it again.
    The input is lazy: while `hold` is on and a frame is stored, ComfyUI never evaluates the
    generation behind it -- as long as nothing else in the graph asks for that generation."""

    _frames = {}  # node id -> last frame (CPU); lives until ComfyUI restarts

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ArtfatFrameHold",
            display_name="Artfat Frame Hold",
            category="artfat",
            description="hold OFF: passes the generated frame through and remembers it. hold ON: returns the remembered frame "
                        "and the generation is skipped. Route EVERY use of the generated frame (save, upscale, edit) through this node, "
                        "otherwise those other links still trigger a new generation.",
            inputs=[
                io.Image.Input("image", optional=True, lazy=True, tooltip="The generated frame (VAE Decode)."),
                io.Boolean.Input("hold", default=False, label_on="hold frame", label_off="pass through",
                                 tooltip="ON = keep the last frame and skip the generation (draw boxes on it, edit it). "
                                         "OFF = generate as usual."),
            ],
            outputs=[io.Image.Output(display_name="image")],
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    def check_lazy_status(cls, hold, image=None):
        if hold and cls.hidden.unique_id in cls._frames:
            return []
        return ["image"]

    @classmethod
    def execute(cls, hold, image=None) -> io.NodeOutput:
        key = cls.hidden.unique_id
        if hold and key in cls._frames:
            return io.NodeOutput(cls._frames[key])
        if image is None:
            raise ValueError("Artfat Frame Hold: no frame yet. Turn `hold` off and run once to generate one.")
        cls._frames[key] = image.detach().cpu().clone()
        return io.NodeOutput(image)


NODE_CLASS_MAPPINGS = {"ArtfatBBoxCanvas": ArtfatBBoxCanvas, "ArtfatBBoxSampler": ArtfatBBoxSampler,
                       "ArtfatBBoxesToMask": ArtfatBBoxesToMask, "ArtfatFrameHold": ArtfatFrameHold,
                       "ArtfatEditSource": ArtfatEditSource}
NODE_DISPLAY_NAME_MAPPINGS = {"ArtfatBBoxCanvas": "Artfat BBox Canvas", "ArtfatBBoxSampler": "Artfat BBox Sampler",
                              "ArtfatBBoxesToMask": "Artfat BBoxes to Mask", "ArtfatFrameHold": "Artfat Frame Hold",
                              "ArtfatEditSource": "Artfat Edit Source"}
