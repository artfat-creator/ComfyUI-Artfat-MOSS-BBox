import { app } from "../../scripts/app.js";

// The core widget only resizes and hides `last_incoming` for its own CreateBoundingBoxes node,
// so the Artfat canvas does the same here.
app.registerExtension({
    name: "Artfat.BBoxCanvas",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ArtfatBBoxCanvas") return;
        // A generated frame sized by `res width/height`: copy its size into the hidden width/height,
        // which is what the canvas reads for its shape. An edit already takes the picture's size.
        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (output) {
            onExecuted?.apply(this, arguments);
            const dims = output?.dims;
            if (!Array.isArray(dims) || dims.length !== 2 || output?.background_images) return;
            for (const [name, value] of [["width", dims[0]], ["height", dims[1]]]) {
                const widget = this.widgets?.find((w) => w.name === name);
                if (widget && widget.value !== value) {
                    widget.value = value;
                    widget.callback?.(value);
                }
            }
        };
    },
    nodeCreated(node) {
        if (node.constructor.comfyClass !== "ArtfatBBoxCanvas") return;
        const [w, h] = node.size;
        node.setSize([Math.max(w, 460), Math.max(h, 760)]);
        for (const widget of node.widgets ?? []) {
            if (widget.name === "last_incoming") widget.hidden = true;
        }
    },
});
