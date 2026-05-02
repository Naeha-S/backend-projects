"""Lazy-loaded Hugging Face pipelines for CLIP and BLIP."""

_clip_pipe = None
_blip_pipe = None


def get_clip():
    global _clip_pipe
    if _clip_pipe is None:
        from transformers import pipeline

        _clip_pipe = pipeline(
            "zero-shot-image-classification",
            model="openai/clip-vit-base-patch32",
        )
    return _clip_pipe


def get_blip():
    global _blip_pipe
    if _blip_pipe is None:
        from transformers import pipeline

        _blip_pipe = pipeline(
            "image-to-text",
            model="Salesforce/blip-image-captioning-base",
        )
    return _blip_pipe
