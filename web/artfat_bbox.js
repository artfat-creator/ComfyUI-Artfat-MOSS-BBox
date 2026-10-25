import { app } from "../../scripts/app.js";

// The canvas shows the picture its `image` input carries, but only once that node has run.
// Edit Source in photo mode just passes the Load Image picture on, so its preview can be set
// right away: the photo shows under the boxes as soon as it is picked, before any run.
// (edit last result needs a run: that frame exists only after the sampler drew it.)
let _store = null;
function nodeOutputStore() {
    if (_store) return _store;
    const host = document.querySelector("#vue-app")?.__vue_app__
        ? document.querySelector("#vue-app")
        : [...document.querySelectorAll("body *")].find((e) => e.__vue_app__);
    _store = host?.__vue_app__?.config?.globalProperties?.$pinia?._s?.get("nodeOutput") || null;
    return _store;
}

function linkOf(graph, id) {
    if (id == null || !graph) return null;
    return graph.getLink?.(id) ?? graph.links?.get?.(id) ?? graph.links?.[id] ?? null;
}

function photoFor(node) {
    const slot = node.inputs?.findIndex((i) => i.name === "image");
    const link = slot >= 0 ? linkOf(node.graph, node.inputs[slot].link) : null;
    let src = link ? node.graph.getNodeById(link.origin_id) : null;
    while (src && src.type === "Reroute") {
        const l = linkOf(src.graph, src.inputs?.[0]?.link);
        src = l ? src.graph.getNodeById(l.origin_id) : null;
    }
    if (!src || src.mode !== 0) return null; // bypassed / muted Load Image = generate
    const value = src.widgets?.find((w) => w.name === "image")?.value;
    if (typeof value !== "string" || !value) return null;
    const cut = value.lastIndexOf("/");
    return { filename: value.slice(cut + 1), subfolder: cut >= 0 ? value.slice(0, cut) : "", type: "input" };
}

// Workflows saved before 1.2.2 store the old on/off switch (`edit_last_result`) in this slot.
// ON was "edit last result"; OFF read the Load Image, which was a photo unless it was bypassed.
function migrateSource(node) {
    const onConfigure = node.onConfigure;
    node.onConfigure = function () {
        const r = onConfigure?.apply(this, arguments);
        const widget = this.widgets?.find((w) => w.name === "source");
        if (widget && typeof widget.value === "boolean") {
            widget.value = widget.value ? "edit last result" : (photoFor(this) ? "my photo" : "new image");
        }
        return r;
    };
}

function watchPhoto(node) {
    let last = "";
    const timer = setInterval(() => {
        if (!node.graph) return;
        if (node.widgets?.find((w) => w.name === "source")?.value !== "my photo") {
            // Only `my photo` reads the Load Image; drop the photo preview we set otherwise,
            // so the photo does not pose as the new frame or the last result.
            if (last && last !== "-") nodeOutputStore()?.removeNodeOutputsForNode?.(node);
            last = "";
            return;
        }
        const photo = photoFor(node);
        const key = photo ? `${photo.subfolder}/${photo.filename}` : "-";
        if (key === last) return;
        const store = nodeOutputStore();
        if (!store) return;
        last = key;
        if (photo) store.setNodeOutputs(node, { images: [photo] });
        else store.removeNodeOutputsForNode?.(node);
    }, 600);
    const onRemoved = node.onRemoved;
    node.onRemoved = function () {
        clearInterval(timer);
        return onRemoved?.apply(this, arguments);
    };
}

// The core canvas sizes itself to the picture's aspect, so the node grew and shrank with every
// picture. Keep a fixed 3:4 frame instead (the node can still be resized, the frame follows its
// width) and fit the canvas inside it. The canvas keeps its own aspect, and the core widget maps
// the pointer from its on-screen box, so the boxes stay where they are drawn.
const FRAME = 4 / 3; // height / width
function fitCanvas(node) {
    const canvas = document.querySelector(`[data-node-id="${node.id}"] div[style*="aspect-ratio"]`);
    const frame = canvas?.parentElement;
    if (!frame) return;
    const [w, h] = canvas.style.aspectRatio.split("/").map(Number);
    if (!w || !h) return;
    const width = frame.clientWidth;
    const height = Math.round(width * FRAME);
    const bar = frame.firstElementChild === canvas ? 0 : frame.firstElementChild.offsetHeight;
    const fitted = `${Math.round(Math.min(width, height * (w / h)))}px`;
    if (canvas.style.width !== fitted) canvas.style.width = fitted;
    canvas.style.alignSelf = "center";
    canvas.style.margin = "auto 0";
    const total = `${bar + height}px`;
    if (frame.style.height !== total) frame.style.height = total;
}

// bbox generation / simple generation: a button in the canvas toolbar, next to Grid, drives the
// hidden `bbox_mode` widget. The toolbar belongs to the core canvas widget (Nodes 2.0) and Vue may
// re-render it, so the button is re-attached whenever it goes missing.
function modeSwitch(node) {
    const widget = node.widgets?.find((w) => w.name === "bbox_mode");
    if (!widget) return;
    widget.hidden = true;
    let btn = null;
    let hint = null;
    const paint = () => {
        const on = widget.value !== false;
        btn.setAttribute("aria-pressed", on ? "true" : "false");
        btn.querySelector("span").textContent = on ? "bbox generation" : "simple generation";
        // Stands out by brightness and weight too, not by hue alone.
        btn.style.background = on ? "#d32f2f" : "#4a4a4a";
        btn.style.color = "#ffffff";
        btn.style.fontWeight = "700";
        btn.title = on ? "ON: boxes, picture and brief are used. Click for simple generation."
                       : "OFF: a plain new frame, the canvas is left out. Click for bbox generation.";
        const pane = btn.closest(".flex-col")?.lastElementChild;
        if (pane && pane !== btn.parentElement) pane.style.opacity = on ? "" : "0.35";
    };
    const timer = setInterval(() => {
        if (!node.graph) return;
        fitCanvas(node);
        const grid = document.querySelector(`[data-node-id="${node.id}"] .h-9 > button[aria-pressed]`);
        if (!grid) return;
        if (!btn || !btn.isConnected) {
            btn = grid.cloneNode(false);
            btn.dataset.artfatMode = "1";
            btn.appendChild(document.createElement("span"));
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                widget.value = widget.value === false;
                widget.callback?.(widget.value);
                node.setDirtyCanvas?.(true, true);
                paint();
            });
            hint = document.createElement("span");
            hint.textContent = "← click to switch the generation mode";
            hint.style.cssText = "font-size:11px;opacity:.6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";
            grid.after(btn, hint);
        }
        paint();
    }, 500);
    const onRemoved = node.onRemoved;
    node.onRemoved = function () {
        clearInterval(timer);
        return onRemoved?.apply(this, arguments);
    };
}

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
        if (node.constructor.comfyClass === "ArtfatEditSource") {
            migrateSource(node);
            watchPhoto(node);
        }
        if (node.constructor.comfyClass !== "ArtfatBBoxCanvas") return;
        const [w, h] = node.size;
        node.setSize([Math.max(w, 460), Math.max(h, 760)]);
        for (const widget of node.widgets ?? []) {
            if (widget.name === "last_incoming") widget.hidden = true;
        }
        modeSwitch(node);
    },
});
