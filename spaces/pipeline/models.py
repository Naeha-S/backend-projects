"""Lazy-loaded Hugging Face pipelines for CLIP and BLIP."""

import os
from pathlib import Path

_clip_pipe = None
_blip_pipe = None

_CACHE_ROOT = Path(__file__).resolve().parent.parent / ".hf_cache"
_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HF_HOME", str(_CACHE_ROOT))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(_CACHE_ROOT / "hub"))
os.environ.setdefault("TRANSFORMERS_CACHE", str(_CACHE_ROOT / "transformers"))


def _hf_token():
    """Read HF token from any of the standard env vars."""
    return (
        os.getenv("HF_TOKEN")
        or os.getenv("HUGGING_FACE_HUB_TOKEN")
        or os.getenv("HUGGINGFACEHUB_API_TOKEN")
    )


def get_clip():
    global _clip_pipe
    if _clip_pipe is None:
        from transformers import pipeline

        token = _hf_token()
        kwargs = {"cache_dir": str(_CACHE_ROOT)}
        if token:
            kwargs["token"] = token
        _clip_pipe = pipeline(
            "zero-shot-image-classification",
            model="openai/clip-vit-base-patch32",
            **kwargs,
        )
    return _clip_pipe


def get_blip():
    global _blip_pipe
    if _blip_pipe is None:
        from transformers import BlipForConditionalGeneration, BlipProcessor

        token = _hf_token()
        kwargs = {"cache_dir": str(_CACHE_ROOT)}
        if token:
            kwargs["token"] = token

        processor = BlipProcessor.from_pretrained(
            "Salesforce/blip-image-captioning-base", **kwargs
        )
        model = BlipForConditionalGeneration.from_pretrained(
            "Salesforce/blip-image-captioning-base", **kwargs
        )

        def blip_pipe(image):
            inputs = processor(images=image, return_tensors="pt")
            out = model.generate(**inputs, max_new_tokens=50)
            caption = processor.decode(out[0], skip_special_tokens=True)
            return [{"generated_text": caption}]

        _blip_pipe = blip_pipe
    return _blip_pipe
