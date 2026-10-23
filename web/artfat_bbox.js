import { app } from "../../scripts/app.js";

// The core widget only resizes and hides `last_incoming` for its own CreateBoundingBoxes node,
// so the Artfat canvas does the same here.
app.registerExtension({
    name: "Artfat.BBoxCanvas",
    nodeCreated(node) {
        if (node.constructor.comfyClass !== "ArtfatBBoxCanvas") return;
        const [w, h] = node.size;
        node.setSize([Math.max(w, 460), Math.max(h, 760)]);
        for (const widget of node.widgets ?? []) {
            if (widget.name === "last_incoming") widget.hidden = true;
        }
    },
});
