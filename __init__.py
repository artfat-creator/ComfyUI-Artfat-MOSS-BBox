import logging

# The nodes build on ComfyUI's own bounding-box canvas (v0.27+) and MOSS-Image support (v0.42+).
# On an older ComfyUI the pack loads empty with one clear line instead of an import traceback.
try:
    import comfy_extras.nodes_moss_image  # noqa: F401  (only present from v0.42.0)
    from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
except ImportError as e:
    logging.warning("[Artfat MOSS BBox] needs ComfyUI v0.42.0 or newer (MOSS-Image + bounding-box canvas). "
                    "Update ComfyUI. Nodes not loaded: %s", e)
    NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS = {}, {}

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
