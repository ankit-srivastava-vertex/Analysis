// ─── Drawing Tools Engine ──────────────────────────────────────────────────────
// TradingView-style canvas-overlay drawing tools, one canvas per chart pane.
//
// Tools (14): cursor, trendline, ray, horizontal, vertical, parallel channel,
// rectangle, price-range, date-range, date+price range, text, comment,
// fib retracement, fib extension.
//
// Architecture:
//   - drawings[paneId] : Array of { id, type, points: [{time, price}], ... }.
//     Persisted to localStorage.chartDrawings (mirrored to server state.json
//     via the TRACKED_KEYS sync layer in index.html). IDs are backfilled on
//     load for any pre-existing drawings so alerts can reference them.
//   - Hover detection happens at the container level (mousemove on the chart
//     parent, not the canvas) so the canvas only intercepts pointer events
//     when within ~10px of a drawing or its handles; the chart remains
//     pannable everywhere else.
//   - Drag uses pixel-space offsets during interaction and only re-projects
//     to time/price on mouseup, so positions don't drift during pan/zoom.
//
// Public surface (consumed by index.html):
//   window.initDrawingTools()       — wire toolbar + keyboard listeners.
//   window.attachDrawingCanvas(pane) — create the overlay for a new pane.
//   window.activateDrawingTool(tool) — programmatically pick a tool.
//   window.drawingsState.drawings   — live reference to the drawings map
//                                     (used by alert engine for drawing-cross
//                                     evaluation).

(function() {
'use strict';

const COLORS = {
    line: '#2962ff',
    channel: '#7b1fa2',
    rectangle: 'rgba(33, 150, 243, 0.15)',
    rectangleBorder: '#2196f3',
    priceRange: 'rgba(76, 175, 80, 0.15)',
    priceRangeBorder: '#4caf50',
    dateRange: 'rgba(156, 39, 176, 0.15)',
    dateRangeBorder: '#9c27b0',
    datePriceRange: 'rgba(255, 152, 0, 0.12)',
    datePriceRangeBorder: '#ff9800',
    text: '#e0e0e0',
    fib: '#f57c00',
    fibFill: 'rgba(245, 124, 0, 0.06)',
    handle: '#ffffff',
    hover: '#ffeb3b',
};

const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
const FIB_EXT_LEVELS = [0, 0.618, 1, 1.272, 1.618, 2, 2.618];

// ─── State ────────────────────────────────────────────────────────────────────
let activeTool = 'cursor';
let drawingState = null; // { phase, points[], paneId }
let drawings = {}; // paneId -> [{type, points, ...}]
let hoveredDrawing = null; // { paneId, index }
let selectedDrawing = null; // { paneId, index } - persists after click for delete
let dragState = null; // { paneId, index, handleIdx, startX, startY, offsetX, offsetY, originalPoints, moved }

function genDrawingId() {
    return 'd_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8);
}

// ─── Init ─────────────────────────────────────────────────────────────────────
window.initDrawingTools = function() {
    setupToolbarButtons();
    loadDrawings();
};

window.activateDrawingTool = function(tool) {
    if (tool === 'delete-all') { deleteAllDrawings(); return; }
    setActiveTool(tool);
};

function setupToolbarButtons() {
    const sidebar = document.getElementById('drawingSidebar');
    if (!sidebar) return;

    sidebar.querySelectorAll('.draw-tool-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tool = btn.dataset.tool;
            if (tool === 'delete-all') {
                deleteAllDrawings();
                return;
            }
            setActiveTool(tool);
        });
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
        if (e.key === 'Escape') {
            cancelDrawing();
            setActiveTool('cursor');
        }
        if (e.key === 'Delete' || e.key === 'Backspace') {
            if (e.target.tagName !== 'INPUT') deleteHoveredDrawing();
        }
    });

    // Document-level mouseup to catch drags that end outside canvas
    document.addEventListener('mouseup', () => {
        if (dragState) {
            const pane = (window.panes || []).find(p => p.id === dragState.paneId);
            if (pane) {
                finishDrag(pane);
            } else {
                dragState = null;
            }
        }
    });

    // Document-level mousemove for drag continuation outside canvas
    document.addEventListener('mousemove', (e) => {
        if (!dragState) return;
        const pane = (window.panes || []).find(p => p.id === dragState.paneId);
        if (!pane) return;

        const rect = pane.drawingCanvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;

        dragState.offsetX = x - dragState.startX;
        dragState.offsetY = y - dragState.startY;
        dragState.moved = true;
        redrawAll(pane);
    });
}

function setActiveTool(tool) {
    activeTool = tool;
    drawingState = null;
    dragState = null;
    if (tool !== 'cursor') {
        selectedDrawing = null;
        hoveredDrawing = null;
    }

    // Update button states
    document.querySelectorAll('.draw-tool-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tool === tool);
    });

    // Update canvas interactivity
    document.querySelectorAll('.drawing-canvas').forEach(canvas => {
        canvas.classList.remove('active', 'interacting');
        if (tool !== 'cursor') {
            canvas.classList.add('active');
        }
    });
}

// ─── Pane Integration ─────────────────────────────────────────────────────────
window.attachDrawingCanvas = function(pane) {
    const container = pane.el.querySelector('.chart-container');
    const canvas = document.createElement('canvas');
    canvas.className = 'drawing-canvas';
    container.appendChild(canvas);
    pane.drawingCanvas = canvas;

    if (!drawings[pane.id]) drawings[pane.id] = [];

    // Resize canvas
    function resizeCanvas() {
        const rect = container.getBoundingClientRect();
        canvas.width = rect.width * window.devicePixelRatio;
        canvas.height = rect.height * window.devicePixelRatio;
        canvas.style.width = rect.width + 'px';
        canvas.style.height = rect.height + 'px';
        redrawAll(pane);
    }

    const ro = new ResizeObserver(resizeCanvas);
    ro.observe(container);
    setTimeout(resizeCanvas, 100);

    // Mouse events on canvas (for drawing mode and active interactions)
    canvas.addEventListener('mousedown', (e) => onMouseDown(e, pane));
    canvas.addEventListener('mousemove', (e) => onMouseMove(e, pane));
    canvas.addEventListener('mouseup', (e) => onMouseUp(e, pane));
    canvas.addEventListener('dblclick', (e) => onDblClick(e, pane));

    // Container-level mousemove for hover detection in cursor mode
    // (fires even when canvas has pointer-events:none because events hit chart elements and bubble up)
    container.addEventListener('mousemove', (e) => onContainerMouseMove(e, pane));

    // Redraw on chart scroll/zoom
    pane.chart.timeScale().subscribeVisibleTimeRangeChange(() => {
        if (dragState && dragState.paneId === pane.id) return;
        redrawAll(pane);
    });
    pane.chart.subscribeCrosshairMove(() => {
        if (drawingState && drawingState.paneId === pane.id) return;
        if (dragState && dragState.paneId === pane.id) return;
        redrawAll(pane);
    });
};

// ─── Coordinate Helpers ───────────────────────────────────────────────────────
function getChartCoords(e, pane) {
    const rect = pane.drawingCanvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    const timeScale = pane.chart.timeScale();
    const priceScale = pane.series;

    const time = timeScale.coordinateToTime(x);
    const price = priceScale.coordinateToPrice(y);

    return { x, y, time, price };
}

function toPixel(point, pane) {
    const timeScale = pane.chart.timeScale();
    const x = timeScale.timeToCoordinate(point.time);
    const y = pane.series.priceToCoordinate(point.price);
    return { x, y };
}

// Container-level hover detection: enables/disables canvas interaction based on proximity to drawings
function onContainerMouseMove(e, pane) {
    if (activeTool !== 'cursor') return;
    if (dragState) return; // Don't change state during drag

    const rect = pane.drawingCanvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const coords = { x, y };

    // Check if near a drawing handle or body
    const hitHandle = findHandleAt(coords, pane);
    const foundIdx = hitHandle !== null ? hitHandle.drawingIndex : findDrawingAt(coords, pane);

    if (foundIdx !== null) {
        // Near a drawing - enable canvas interaction
        pane.drawingCanvas.classList.add('interacting');
        if (hitHandle !== null) {
            pane.drawingCanvas.style.cursor = 'grab';
        } else {
            pane.drawingCanvas.style.cursor = 'move';
        }
        const newHover = { paneId: pane.id, index: foundIdx };
        if (!hoveredDrawing || hoveredDrawing.paneId !== newHover.paneId || hoveredDrawing.index !== newHover.index) {
            hoveredDrawing = newHover;
            redrawAll(pane);
        }
    } else {
        // Not near any drawing - disable canvas interaction so chart works
        pane.drawingCanvas.classList.remove('interacting');
        pane.drawingCanvas.style.cursor = '';
        if (hoveredDrawing && hoveredDrawing.paneId === pane.id) {
            hoveredDrawing = null;
            redrawAll(pane);
        }
    }
}

// Find if cursor is near a handle (anchor point) of a hovered/selected drawing
function findHandleAt(coords, pane) {
    const paneDrawings = drawings[pane.id] || [];
    const handleRadius = 10;

    // Only check handles on drawings that are currently showing handles (hovered or selected)
    const candidates = [];
    if (hoveredDrawing && hoveredDrawing.paneId === pane.id) candidates.push(hoveredDrawing.index);
    if (selectedDrawing && selectedDrawing.paneId === pane.id && !candidates.includes(selectedDrawing.index)) {
        candidates.push(selectedDrawing.index);
    }

    for (const i of candidates) {
        if (i >= paneDrawings.length) continue;
        const d = paneDrawings[i];
        const pts = d.points.map(p => toPixel(p, pane));
        for (let j = 0; j < pts.length; j++) {
            if (pts[j].x === null || pts[j].y === null) continue;
            const dist = Math.hypot(coords.x - pts[j].x, coords.y - pts[j].y);
            if (dist <= handleRadius) {
                return { drawingIndex: i, pointIndex: j };
            }
        }
    }
    return null;
}

// ─── Mouse Handlers ───────────────────────────────────────────────────────────
function onMouseDown(e, pane) {
    if (activeTool === 'cursor') {
        const rect = pane.drawingCanvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const coords = { x, y };

        const hitHandle = findHandleAt(coords, pane);

        if (hitHandle !== null) {
            // Start handle drag (adjust endpoint) — pixel-space
            dragState = {
                paneId: pane.id,
                index: hitHandle.drawingIndex,
                handleIdx: hitHandle.pointIndex,
                startX: x,
                startY: y,
                offsetX: 0,
                offsetY: 0,
                originalPoints: drawings[pane.id][hitHandle.drawingIndex].points.map(p => ({ ...p })),
                moved: false,
            };
            selectedDrawing = { paneId: pane.id, index: hitHandle.drawingIndex };
            pane.drawingCanvas.style.cursor = 'grabbing';
            e.preventDefault();
            e.stopPropagation();
            return;
        }

        const foundIdx = findDrawingAt(coords, pane);
        if (foundIdx !== null) {
            // Start whole-drawing drag — pixel-space
            dragState = {
                paneId: pane.id,
                index: foundIdx,
                handleIdx: -1,
                startX: x,
                startY: y,
                offsetX: 0,
                offsetY: 0,
                originalPoints: drawings[pane.id][foundIdx].points.map(p => ({ ...p })),
                moved: false,
            };
            hoveredDrawing = { paneId: pane.id, index: foundIdx };
            selectedDrawing = { paneId: pane.id, index: foundIdx };
            pane.drawingCanvas.style.cursor = 'grabbing';
            e.preventDefault();
            e.stopPropagation();
            return;
        }

        // Clicked away from any drawing — deselect
        selectedDrawing = null;
        redrawAll(pane);
        return;
    }

    const coords = getChartCoords(e, pane);
    if (!coords.time || coords.price === null) return;

    if (!drawingState) {
        // Start new drawing
        drawingState = {
            paneId: pane.id,
            type: activeTool,
            points: [{ time: coords.time, price: coords.price }],
            phase: 'first-click',
        };
    } else if (drawingState.paneId === pane.id) {
        // Add point
        drawingState.points.push({ time: coords.time, price: coords.price });
        
        const neededPoints = getPointsNeeded(drawingState.type);
        if (drawingState.points.length >= neededPoints) {
            commitDrawing(pane);
        }
    }
}

function onMouseMove(e, pane) {
    if (activeTool === 'cursor') {
        // Handle active drag in pixel space
        if (dragState && dragState.paneId === pane.id) {
            const rect = pane.drawingCanvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;

            dragState.offsetX = x - dragState.startX;
            dragState.offsetY = y - dragState.startY;
            dragState.moved = true;

            e.preventDefault();
            e.stopPropagation();
            redrawAll(pane);
            return;
        }

        // If canvas is interacting (near a drawing), update cursor
        const rect = pane.drawingCanvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const hitHandle = findHandleAt({ x, y }, pane);
        if (hitHandle !== null) {
            pane.drawingCanvas.style.cursor = 'grab';
        } else {
            pane.drawingCanvas.style.cursor = 'move';
        }
        return;
    }

    // Drawing mode: preview
    if (!drawingState || drawingState.paneId !== pane.id) return;

    const coords = getChartCoords(e, pane);
    if (!coords.time || coords.price === null) return;

    // Preview current drawing
    redrawAll(pane, { time: coords.time, price: coords.price });
}

function onMouseUp(e, pane) {
    if (activeTool === 'cursor') {
        if (dragState && dragState.paneId === pane.id) {
            finishDrag(pane);
            e.preventDefault();
            e.stopPropagation();
        }
        return;
    }

    // For single-click tools like horizontal/vertical line
    if (drawingState && drawingState.paneId === pane.id) {
        const type = drawingState.type;
        if ((type === 'horizontal' || type === 'vertical') && drawingState.points.length >= 1) {
            commitDrawing(pane);
        }
        if (type === 'text' && drawingState.points.length >= 1) {
            commitTextDrawing(pane);
        }
    }
}

function onDblClick(e, pane) {
    if (activeTool === 'cursor') {
        const rect = pane.drawingCanvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const foundIdx = findDrawingAt({ x, y }, pane);
        if (foundIdx !== null) {
            drawings[pane.id].splice(foundIdx, 1);
            hoveredDrawing = null;
            selectedDrawing = null;
            dragState = null;
            redrawAll(pane);
            saveDrawings();
            e.preventDefault();
            e.stopPropagation();
        }
    }
}

// Convert pixel-space drag offset to time/price and commit final positions
function finishDrag(pane) {
    if (!dragState || !dragState.moved) {
        // No movement — just a click (selection). Keep selectedDrawing set.
        pane.drawingCanvas.style.cursor = 'move';
        dragState = null;
        redrawAll(pane);
        return;
    }

    const drawing = drawings[pane.id][dragState.index];
    if (!drawing) { dragState = null; return; }

    const timeScale = pane.chart.timeScale();
    const series = pane.series;

    if (dragState.handleIdx >= 0) {
        // Single handle moved — compute new position from original pixel + offset
        const origPt = dragState.originalPoints[dragState.handleIdx];
        const origPixel = toPixel(origPt, pane);
        if (origPixel.x !== null && origPixel.y !== null) {
            const newX = origPixel.x + dragState.offsetX;
            const newY = origPixel.y + dragState.offsetY;
            const newTime = timeScale.coordinateToTime(newX);
            const newPrice = series.coordinateToPrice(newY);
            if (newTime && newPrice !== null) {
                drawing.points[dragState.handleIdx] = { time: newTime, price: newPrice };
            }
        }
    } else {
        // Whole drawing moved — apply offset to each point
        for (let i = 0; i < drawing.points.length; i++) {
            const origPt = dragState.originalPoints[i];
            const origPixel = toPixel(origPt, pane);
            if (origPixel.x !== null && origPixel.y !== null) {
                const newX = origPixel.x + dragState.offsetX;
                const newY = origPixel.y + dragState.offsetY;
                const newTime = timeScale.coordinateToTime(newX);
                const newPrice = series.coordinateToPrice(newY);
                if (newTime && newPrice !== null) {
                    drawing.points[i] = { time: newTime, price: newPrice };
                } else {
                    // Fallback: keep original position if conversion fails
                    drawing.points[i] = { ...origPt };
                }
            }
        }
    }

    pane.drawingCanvas.style.cursor = 'move';
    dragState = null;
    saveDrawings();
    redrawAll(pane);
}

function getPointsNeeded(type) {
    switch (type) {
        case 'horizontal':
        case 'vertical':
        case 'text':
            return 1;
        case 'trendline':
        case 'ray':
        case 'rectangle':
        case 'pricerange':
        case 'daterange':
        case 'datepricerange':
        case 'fibretracement':
        case 'fibextension':
            return 2;
        case 'channel':
            return 3;
        default:
            return 2;
    }
}

function cancelDrawing() {
    if (drawingState) {
        const pane = window.panes && window.panes.find(p => p.id === drawingState.paneId);
        drawingState = null;
        if (pane) redrawAll(pane);
    }
}

function commitDrawing(pane) {
    if (!drawingState) return;
    const drawing = {
        id: genDrawingId(),
        type: drawingState.type,
        points: [...drawingState.points],
        color: COLORS.line,
    };
    if (!drawings[pane.id]) drawings[pane.id] = [];
    drawings[pane.id].push(drawing);
    drawingState = null;

    // Auto-switch to cursor after drawing
    setActiveTool('cursor');
    redrawAll(pane);
    saveDrawings();
}

function commitTextDrawing(pane) {
    if (!drawingState) return;
    const text = prompt('Enter text:');
    if (!text || !text.trim()) {
        drawingState = null;
        setActiveTool('cursor');
        return;
    }
    const drawing = {
        id: genDrawingId(),
        type: 'text',
        points: [...drawingState.points],
        color: COLORS.text,
        text: text.trim(),
    };
    if (!drawings[pane.id]) drawings[pane.id] = [];
    drawings[pane.id].push(drawing);
    drawingState = null;
    setActiveTool('cursor');
    redrawAll(pane);
    saveDrawings();
}

// ─── Hit Detection ────────────────────────────────────────────────────────────
function findDrawingAt(coords, pane) {
    const paneDrawings = drawings[pane.id] || [];
    const threshold = 8;

    for (let i = paneDrawings.length - 1; i >= 0; i--) {
        const d = paneDrawings[i];
        if (isPointNearDrawing(coords, d, pane, threshold)) {
            return i;
        }
    }
    return null;
}

function isPointNearDrawing(coords, drawing, pane, threshold) {
    const pts = drawing.points.map(p => toPixel(p, pane));
    if (pts.some(p => p.x === null || p.y === null)) return false;

    switch (drawing.type) {
        case 'horizontal': {
            const y = pts[0].y;
            return Math.abs(coords.y - y) < threshold;
        }
        case 'vertical': {
            const x = pts[0].x;
            return Math.abs(coords.x - x) < threshold;
        }
        case 'trendline':
        case 'ray': {
            return distToSegment(coords, pts[0], pts[1]) < threshold;
        }
        case 'text': {
            const tx = pts[0].x;
            const ty = pts[0].y;
            const textW = (drawing.text || '').length * 7;
            return coords.x >= tx - 4 && coords.x <= tx + textW + 4 &&
                   coords.y >= ty - 16 && coords.y <= ty + 4;
        }
        case 'rectangle':
        case 'pricerange':
        case 'daterange':
        case 'datepricerange': {
            const minX = Math.min(pts[0].x, pts[1].x);
            const maxX = Math.max(pts[0].x, pts[1].x);
            const minY = Math.min(pts[0].y, pts[1].y);
            const maxY = Math.max(pts[0].y, pts[1].y);
            return coords.x >= minX - threshold && coords.x <= maxX + threshold &&
                   coords.y >= minY - threshold && coords.y <= maxY + threshold;
        }
        case 'channel': {
            if (pts.length < 3) return false;
            const d1 = distToSegment(coords, pts[0], pts[1]);
            // Parallel line offset
            const dy = pts[2].y - pts[0].y;
            const p3 = { x: pts[0].x, y: pts[0].y + dy };
            const p4 = { x: pts[1].x, y: pts[1].y + dy };
            const d2 = distToSegment(coords, p3, p4);
            return d1 < threshold || d2 < threshold;
        }
        case 'fibretracement':
        case 'fibextension': {
            const minY = Math.min(pts[0].y, pts[1].y);
            const maxY = Math.max(pts[0].y, pts[1].y);
            return coords.y >= minY - 20 && coords.y <= maxY + 20;
        }
        default:
            return false;
    }
}

function distToSegment(p, a, b) {
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const lenSq = dx * dx + dy * dy;
    if (lenSq === 0) return Math.hypot(p.x - a.x, p.y - a.y);
    let t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / lenSq;
    t = Math.max(0, Math.min(1, t));
    const px = a.x + t * dx;
    const py = a.y + t * dy;
    return Math.hypot(p.x - px, p.y - py);
}

// ─── Rendering ────────────────────────────────────────────────────────────────
function redrawAll(pane, cursorPoint) {
    const canvas = pane.drawingCanvas;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.save();
    ctx.scale(dpr, dpr);

    // Draw committed drawings
    const paneDrawings = drawings[pane.id] || [];
    paneDrawings.forEach((d, idx) => {
        const isHovered = (activeTool === 'cursor' && hoveredDrawing && hoveredDrawing.paneId === pane.id && hoveredDrawing.index === idx);
        const isSelected = (activeTool === 'cursor' && selectedDrawing && selectedDrawing.paneId === pane.id && selectedDrawing.index === idx);
        const isDragging = (dragState && dragState.paneId === pane.id && dragState.index === idx && dragState.moved);

        if (isDragging) {
            // Render at pixel-offset position (avoids time conversion issues)
            renderDrawingWithOffset(ctx, d, pane, dragState, isHovered || isSelected);
        } else {
            renderDrawing(ctx, d, pane, isHovered || isSelected);
        }
    });

    // Draw in-progress
    if (drawingState && drawingState.paneId === pane.id && cursorPoint) {
        const preview = {
            type: drawingState.type,
            points: [...drawingState.points, cursorPoint],
        };
        renderDrawing(ctx, preview, pane, false, true);
    }

    ctx.restore();
}

// Render a drawing using pixel offsets during drag (no time/price conversion for position)
function renderDrawingWithOffset(ctx, drawing, pane, drag, isHighlighted) {
    // Compute original pixel positions and apply the drag offset
    const origPts = drag.originalPoints.map(p => toPixel(p, pane));
    if (origPts.some(p => p.x === null || p.y === null)) return;

    let pts;
    if (drag.handleIdx >= 0) {
        // Only the dragged handle moves
        pts = origPts.map((p, i) => {
            if (i === drag.handleIdx) {
                return { x: p.x + drag.offsetX, y: p.y + drag.offsetY };
            }
            return { ...p };
        });
    } else {
        // All points move together
        pts = origPts.map(p => ({
            x: p.x + drag.offsetX,
            y: p.y + drag.offsetY,
        }));
    }

    ctx.save();
    ctx.lineWidth = isHighlighted ? 2.5 : 1.5;
    ctx.setLineDash([]);
    const color = isHighlighted ? COLORS.hover : COLORS.line;

    // Use the same render logic but with pre-computed pixel positions
    switch (drawing.type) {
        case 'horizontal':
            drawHorizontalLine(ctx, pts[0], pane, color, drawing.points[0].price);
            break;
        case 'vertical':
            drawVerticalLine(ctx, pts[0], pane, color);
            break;
        case 'trendline':
            if (pts.length >= 2) drawTrendLine(ctx, pts[0], pts[1], color);
            break;
        case 'ray':
            if (pts.length >= 2) drawRay(ctx, pts[0], pts[1], pane, color);
            break;
        case 'channel':
            drawChannel(ctx, pts, pane, isHighlighted);
            break;
        case 'rectangle':
            if (pts.length >= 2) drawRectangle(ctx, pts[0], pts[1], isHighlighted);
            break;
        case 'pricerange':
            if (pts.length >= 2) drawPriceRangePixel(ctx, pts, pane, isHighlighted);
            break;
        case 'daterange':
            if (pts.length >= 2) drawDateRangePixel(ctx, pts, pane, isHighlighted);
            break;
        case 'datepricerange':
            if (pts.length >= 2) drawDatePriceRangePixel(ctx, pts, pane, isHighlighted);
            break;
        case 'text':
            drawTextAnnotationPixel(ctx, drawing, pts[0], isHighlighted);
            break;
        case 'fibretracement':
            if (pts.length >= 2) drawFibRetracementPixel(ctx, pts, pane, isHighlighted);
            break;
        case 'fibextension':
            if (pts.length >= 2) drawFibExtensionPixel(ctx, pts, pane, isHighlighted);
            break;
    }

    // Draw handles
    if (isHighlighted) {
        pts.forEach(p => {
            ctx.beginPath();
            ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
            ctx.fillStyle = COLORS.handle;
            ctx.strokeStyle = COLORS.line;
            ctx.lineWidth = 1.5;
            ctx.fill();
            ctx.stroke();
        });
    }

    ctx.restore();
}

// Simplified pixel-based renderers for drag preview (price/date ranges don't need labels during drag)
function drawPriceRangePixel(ctx, pts, pane, isHovered) {
    const canvas = pane.drawingCanvas;
    const w = canvas.width / window.devicePixelRatio;
    const minY = Math.min(pts[0].y, pts[1].y);
    const maxY = Math.max(pts[0].y, pts[1].y);
    const h = maxY - minY;
    ctx.fillStyle = isHovered ? 'rgba(76, 175, 80, 0.25)' : COLORS.priceRange;
    ctx.fillRect(0, minY, w, h);
    ctx.strokeStyle = isHovered ? COLORS.hover : COLORS.priceRangeBorder;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(0, minY); ctx.lineTo(w, minY);
    ctx.moveTo(0, maxY); ctx.lineTo(w, maxY);
    ctx.stroke();
}

function drawDateRangePixel(ctx, pts, pane, isHovered) {
    const canvas = pane.drawingCanvas;
    const h = canvas.height / window.devicePixelRatio;
    const minX = Math.min(pts[0].x, pts[1].x);
    const maxX = Math.max(pts[0].x, pts[1].x);
    const w = maxX - minX;
    ctx.fillStyle = isHovered ? 'rgba(156, 39, 176, 0.25)' : COLORS.dateRange;
    ctx.fillRect(minX, 0, w, h);
    ctx.strokeStyle = isHovered ? COLORS.hover : COLORS.dateRangeBorder;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(minX, 0); ctx.lineTo(minX, h);
    ctx.moveTo(maxX, 0); ctx.lineTo(maxX, h);
    ctx.stroke();
}

function drawDatePriceRangePixel(ctx, pts, pane, isHovered) {
    const minX = Math.min(pts[0].x, pts[1].x);
    const maxX = Math.max(pts[0].x, pts[1].x);
    const minY = Math.min(pts[0].y, pts[1].y);
    const maxY = Math.max(pts[0].y, pts[1].y);
    const w = maxX - minX;
    const h = maxY - minY;
    ctx.fillStyle = isHovered ? 'rgba(255, 152, 0, 0.2)' : COLORS.datePriceRange;
    ctx.fillRect(minX, minY, w, h);
    ctx.strokeStyle = isHovered ? COLORS.hover : COLORS.datePriceRangeBorder;
    ctx.lineWidth = 1.5;
    ctx.strokeRect(minX, minY, w, h);
}

function drawTextAnnotationPixel(ctx, drawing, pt, isHovered) {
    const text = drawing.text || '';
    const color = isHovered ? COLORS.hover : COLORS.text;
    ctx.font = '12px -apple-system, sans-serif';
    ctx.fillStyle = color;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'bottom';
    ctx.fillText(text, pt.x, pt.y);
    ctx.textBaseline = 'alphabetic';
}

function drawFibRetracementPixel(ctx, pts, pane, isHovered) {
    const canvas = pane.drawingCanvas;
    const w = canvas.width / window.devicePixelRatio;
    const color = isHovered ? COLORS.hover : COLORS.fib;
    const yRange = pts[0].y - pts[1].y;

    FIB_LEVELS.forEach((level) => {
        const y = pts[1].y + yRange * level;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.strokeStyle = color;
        ctx.lineWidth = level === 0 || level === 1 ? 1.5 : 1;
        ctx.setLineDash(level === 0.5 ? [4, 4] : []);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = color;
        ctx.font = '10px -apple-system, sans-serif';
        ctx.textAlign = 'right';
        ctx.fillText(`${(level * 100).toFixed(1)}%`, w - 5, y - 3);
    });
}

function drawFibExtensionPixel(ctx, pts, pane, isHovered) {
    const canvas = pane.drawingCanvas;
    const w = canvas.width / window.devicePixelRatio;
    const color = isHovered ? COLORS.hover : '#9c27b0';
    const yRange = pts[0].y - pts[1].y;

    FIB_EXT_LEVELS.forEach((level) => {
        const y = pts[1].y + yRange * level;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.strokeStyle = color;
        ctx.lineWidth = level === 0 || level === 1 ? 1.5 : 1;
        ctx.setLineDash(level > 1 ? [6, 3] : []);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = color;
        ctx.font = '10px -apple-system, sans-serif';
        ctx.textAlign = 'right';
        ctx.fillText(`${(level * 100).toFixed(1)}%`, w - 5, y - 3);
    });
}

function renderDrawing(ctx, drawing, pane, isHovered, isPreview) {
    const pts = drawing.points.map(p => toPixel(p, pane));
    if (pts.some(p => p.x === null || p.y === null)) return;

    ctx.save();
    ctx.lineWidth = isHovered ? 2.5 : 1.5;
    ctx.setLineDash(isPreview ? [5, 5] : []);

    const color = isHovered ? COLORS.hover : COLORS.line;

    switch (drawing.type) {
        case 'horizontal':
            drawHorizontalLine(ctx, pts[0], pane, color, drawing.points[0].price);
            break;
        case 'vertical':
            drawVerticalLine(ctx, pts[0], pane, color);
            break;
        case 'trendline':
            if (pts.length >= 2) drawTrendLine(ctx, pts[0], pts[1], color);
            break;
        case 'ray':
            if (pts.length >= 2) drawRay(ctx, pts[0], pts[1], pane, color);
            break;
        case 'channel':
            drawChannel(ctx, pts, pane, isHovered);
            break;
        case 'rectangle':
            if (pts.length >= 2) drawRectangle(ctx, pts[0], pts[1], isHovered);
            break;
        case 'pricerange':
            if (pts.length >= 2) drawPriceRange(ctx, drawing.points, pane, isHovered);
            break;
        case 'daterange':
            if (pts.length >= 2) drawDateRange(ctx, drawing.points, pane, isHovered);
            break;
        case 'datepricerange':
            if (pts.length >= 2) drawDatePriceRange(ctx, drawing.points, pane, isHovered);
            break;
        case 'text':
            drawTextAnnotation(ctx, drawing, pane, isHovered);
            break;
        case 'fibretracement':
            if (pts.length >= 2) drawFibRetracement(ctx, drawing.points, pane, isHovered);
            break;
        case 'fibextension':
            if (pts.length >= 2) drawFibExtension(ctx, drawing.points, pane, isHovered);
            break;
    }

    // Draw handles on hover
    if (isHovered && !isPreview) {
        pts.forEach(p => {
            ctx.beginPath();
            ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
            ctx.fillStyle = COLORS.handle;
            ctx.strokeStyle = COLORS.line;
            ctx.lineWidth = 1.5;
            ctx.fill();
            ctx.stroke();
        });
    }

    ctx.restore();
}

function drawHorizontalLine(ctx, pt, pane, color, price) {
    const canvas = pane.drawingCanvas;
    const w = canvas.width / window.devicePixelRatio;
    ctx.beginPath();
    ctx.moveTo(0, pt.y);
    ctx.lineTo(w, pt.y);
    ctx.strokeStyle = color;
    ctx.stroke();

    // Price label
    ctx.fillStyle = color;
    ctx.font = '11px -apple-system, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText(price.toFixed(2), 5, pt.y - 4);
}

function drawVerticalLine(ctx, pt, pane, color) {
    const canvas = pane.drawingCanvas;
    const h = canvas.height / window.devicePixelRatio;
    ctx.beginPath();
    ctx.moveTo(pt.x, 0);
    ctx.lineTo(pt.x, h);
    ctx.strokeStyle = color;
    ctx.stroke();
}

function drawTrendLine(ctx, p1, p2, color) {
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.strokeStyle = color;
    ctx.stroke();
}

function drawRay(ctx, p1, p2, pane, color) {
    const canvas = pane.drawingCanvas;
    const w = canvas.width / window.devicePixelRatio;
    const h = canvas.height / window.devicePixelRatio;

    // Extend the line from p1 through p2 to edges
    const dx = p2.x - p1.x;
    const dy = p2.y - p1.y;

    let endX, endY;
    if (Math.abs(dx) < 0.001) {
        endX = p2.x;
        endY = dy > 0 ? h : 0;
    } else {
        const slope = dy / dx;
        if (dx > 0) {
            endX = w;
            endY = p1.y + slope * (w - p1.x);
        } else {
            endX = 0;
            endY = p1.y + slope * (0 - p1.x);
        }
    }

    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(endX, endY);
    ctx.strokeStyle = color;
    ctx.stroke();

    // Draw dot at origin
    ctx.beginPath();
    ctx.arc(p1.x, p1.y, 3, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
}

function drawChannel(ctx, pts, pane, isHovered) {
    if (pts.length < 2) return;
    const color = isHovered ? COLORS.hover : COLORS.channel;

    // Main line
    ctx.beginPath();
    ctx.moveTo(pts[0].x, pts[0].y);
    ctx.lineTo(pts[1].x, pts[1].y);
    ctx.strokeStyle = color;
    ctx.stroke();

    if (pts.length >= 3) {
        // Parallel line: same direction as pts[0]->pts[1] but offset by pts[2] height
        const dy = pts[2].y - pts[0].y;
        ctx.beginPath();
        ctx.moveTo(pts[0].x, pts[0].y + dy);
        ctx.lineTo(pts[1].x, pts[1].y + dy);
        ctx.strokeStyle = color;
        ctx.stroke();

        // Middle line (dashed)
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(pts[0].x, pts[0].y + dy / 2);
        ctx.lineTo(pts[1].x, pts[1].y + dy / 2);
        ctx.strokeStyle = color;
        ctx.globalAlpha = 0.5;
        ctx.stroke();
        ctx.globalAlpha = 1;
        ctx.setLineDash([]);

        // Fill between lines
        ctx.fillStyle = isHovered ? 'rgba(123, 31, 162, 0.12)' : 'rgba(123, 31, 162, 0.06)';
        ctx.beginPath();
        ctx.moveTo(pts[0].x, pts[0].y);
        ctx.lineTo(pts[1].x, pts[1].y);
        ctx.lineTo(pts[1].x, pts[1].y + dy);
        ctx.lineTo(pts[0].x, pts[0].y + dy);
        ctx.closePath();
        ctx.fill();
    }
}

function drawRectangle(ctx, p1, p2, isHovered) {
    const x = Math.min(p1.x, p2.x);
    const y = Math.min(p1.y, p2.y);
    const w = Math.abs(p2.x - p1.x);
    const h = Math.abs(p2.y - p1.y);

    ctx.fillStyle = isHovered ? 'rgba(33, 150, 243, 0.2)' : COLORS.rectangle;
    ctx.fillRect(x, y, w, h);

    ctx.strokeStyle = isHovered ? COLORS.hover : COLORS.rectangleBorder;
    ctx.strokeRect(x, y, w, h);
}

function drawPriceRange(ctx, points, pane, isHovered) {
    const p1 = toPixel(points[0], pane);
    const p2 = toPixel(points[1], pane);
    if (p1.x === null || p2.x === null) return;

    const canvas = pane.drawingCanvas;
    const w = canvas.width / window.devicePixelRatio;
    const minY = Math.min(p1.y, p2.y);
    const maxY = Math.max(p1.y, p2.y);
    const h = maxY - minY;

    // Fill the full-width band
    ctx.fillStyle = isHovered ? 'rgba(76, 175, 80, 0.25)' : COLORS.priceRange;
    ctx.fillRect(0, minY, w, h);

    // Top and bottom lines
    ctx.strokeStyle = isHovered ? COLORS.hover : COLORS.priceRangeBorder;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(0, minY); ctx.lineTo(w, minY);
    ctx.moveTo(0, maxY); ctx.lineTo(w, maxY);
    ctx.stroke();

    // Price labels
    const price1 = points[0].price;
    const price2 = points[1].price;
    const diff = price1 - price2;
    const pctDiff = ((diff / price2) * 100).toFixed(2);
    const midY = (minY + maxY) / 2;

    ctx.font = '11px -apple-system, sans-serif';
    ctx.fillStyle = COLORS.priceRangeBorder;
    ctx.textAlign = 'left';
    ctx.fillText(`₹${Math.abs(diff).toFixed(2)} (${Math.abs(pctDiff)}%)`, 8, midY + 4);

    // High/Low price labels on right
    ctx.textAlign = 'right';
    const highPrice = Math.max(price1, price2);
    const lowPrice = Math.min(price1, price2);
    ctx.fillText(`₹${highPrice.toFixed(2)}`, w - 8, minY - 4);
    ctx.fillText(`₹${lowPrice.toFixed(2)}`, w - 8, maxY + 14);
    ctx.textAlign = 'left';
}

function drawDateRange(ctx, points, pane, isHovered) {
    const p1 = toPixel(points[0], pane);
    const p2 = toPixel(points[1], pane);
    if (p1.x === null || p2.x === null) return;

    const canvas = pane.drawingCanvas;
    const h = canvas.height / window.devicePixelRatio;
    const minX = Math.min(p1.x, p2.x);
    const maxX = Math.max(p1.x, p2.x);
    const w = maxX - minX;

    // Fill the full-height band
    ctx.fillStyle = isHovered ? 'rgba(156, 39, 176, 0.25)' : COLORS.dateRange;
    ctx.fillRect(minX, 0, w, h);

    // Left and right lines
    ctx.strokeStyle = isHovered ? COLORS.hover : COLORS.dateRangeBorder;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(minX, 0); ctx.lineTo(minX, h);
    ctx.moveTo(maxX, 0); ctx.lineTo(maxX, h);
    ctx.stroke();

    // Date labels and bar count
    const t1 = points[0].time;
    const t2 = points[1].time;
    const days = Math.abs(Math.round((t2 - t1) / 86400));
    const midX = (minX + maxX) / 2;

    ctx.font = '11px -apple-system, sans-serif';
    ctx.fillStyle = COLORS.dateRangeBorder;
    ctx.textAlign = 'center';
    ctx.fillText(`${days} bar${days !== 1 ? 's' : ''}`, midX, 16);

    // Date labels at bottom
    const d1 = new Date(Math.min(t1, t2) * 1000);
    const d2 = new Date(Math.max(t1, t2) * 1000);
    const fmt = d => `${d.getDate()}/${d.getMonth()+1}`;
    ctx.fillText(`${fmt(d1)} → ${fmt(d2)}`, midX, h - 8);
    ctx.textAlign = 'left';
}

function drawDatePriceRange(ctx, points, pane, isHovered) {
    const p1 = toPixel(points[0], pane);
    const p2 = toPixel(points[1], pane);
    if (p1.x === null || p2.x === null) return;

    const minX = Math.min(p1.x, p2.x);
    const maxX = Math.max(p1.x, p2.x);
    const minY = Math.min(p1.y, p2.y);
    const maxY = Math.max(p1.y, p2.y);
    const w = maxX - minX;
    const h = maxY - minY;

    // Fill rectangle
    ctx.fillStyle = isHovered ? 'rgba(255, 152, 0, 0.2)' : COLORS.datePriceRange;
    ctx.fillRect(minX, minY, w, h);

    // Border
    ctx.strokeStyle = isHovered ? COLORS.hover : COLORS.datePriceRangeBorder;
    ctx.lineWidth = 1.5;
    ctx.strokeRect(minX, minY, w, h);

    // Dashed center lines
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(minX, (minY + maxY) / 2);
    ctx.lineTo(maxX, (minY + maxY) / 2);
    ctx.moveTo((minX + maxX) / 2, minY);
    ctx.lineTo((minX + maxX) / 2, maxY);
    ctx.strokeStyle = isHovered ? COLORS.hover : COLORS.datePriceRangeBorder;
    ctx.globalAlpha = 0.5;
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.setLineDash([]);

    // Price info
    const price1 = points[0].price;
    const price2 = points[1].price;
    const priceDiff = price1 - price2;
    const pctDiff = ((priceDiff / price2) * 100).toFixed(2);

    // Date info
    const t1 = points[0].time;
    const t2 = points[1].time;
    const days = Math.abs(Math.round((t2 - t1) / 86400));
    const d1 = new Date(Math.min(t1, t2) * 1000);
    const d2 = new Date(Math.max(t1, t2) * 1000);
    const fmt = d => `${d.getDate()}/${d.getMonth()+1}`;

    const midX = (minX + maxX) / 2;
    const midY = (minY + maxY) / 2;
    const color = isHovered ? COLORS.hover : COLORS.datePriceRangeBorder;

    ctx.font = 'bold 11px -apple-system, sans-serif';
    ctx.fillStyle = color;
    ctx.textAlign = 'center';

    // Price label (center)
    const sign = priceDiff >= 0 ? '+' : '';
    ctx.fillText(`${sign}₹${priceDiff.toFixed(2)} (${sign}${pctDiff}%)`, midX, midY - 4);

    // Date label (below price)
    ctx.font = '10px -apple-system, sans-serif';
    ctx.fillText(`${fmt(d1)} → ${fmt(d2)}  •  ${days} bar${days !== 1 ? 's' : ''}`, midX, midY + 12);

    // High/Low price on right edge
    ctx.font = '10px -apple-system, sans-serif';
    ctx.textAlign = 'right';
    const highPrice = Math.max(price1, price2);
    const lowPrice = Math.min(price1, price2);
    ctx.fillText(`₹${highPrice.toFixed(2)}`, maxX - 4, minY + 12);
    ctx.fillText(`₹${lowPrice.toFixed(2)}`, maxX - 4, maxY - 4);
    ctx.textAlign = 'left';
}

function drawTextAnnotation(ctx, drawing, pane, isHovered) {
    const pt = toPixel(drawing.points[0], pane);
    if (pt.x === null || pt.y === null) return;

    const text = drawing.text || '';
    const color = isHovered ? COLORS.hover : COLORS.text;

    ctx.font = '12px -apple-system, sans-serif';
    ctx.fillStyle = color;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'bottom';
    ctx.fillText(text, pt.x, pt.y);
    ctx.textBaseline = 'alphabetic';

    // Underline on hover
    if (isHovered) {
        const metrics = ctx.measureText(text);
        ctx.beginPath();
        ctx.moveTo(pt.x, pt.y + 2);
        ctx.lineTo(pt.x + metrics.width, pt.y + 2);
        ctx.strokeStyle = COLORS.hover;
        ctx.lineWidth = 1;
        ctx.stroke();
    }
}

function drawFibRetracement(ctx, points, pane, isHovered) {
    const p1 = toPixel(points[0], pane);
    const p2 = toPixel(points[1], pane);
    if (p1.x === null || p2.x === null) return;

    const canvas = pane.drawingCanvas;
    const w = canvas.width / window.devicePixelRatio;
    const priceRange = points[0].price - points[1].price;
    const color = isHovered ? COLORS.hover : COLORS.fib;

    FIB_LEVELS.forEach((level, idx) => {
        const price = points[1].price + priceRange * level;
        const pixel = pane.series.priceToCoordinate(price);
        if (pixel === null) return;

        ctx.beginPath();
        ctx.moveTo(0, pixel);
        ctx.lineTo(w, pixel);
        ctx.strokeStyle = color;
        ctx.lineWidth = level === 0 || level === 1 ? 1.5 : 1;
        ctx.setLineDash(level === 0.5 ? [4, 4] : []);
        ctx.stroke();
        ctx.setLineDash([]);

        // Label
        ctx.fillStyle = color;
        ctx.font = '10px -apple-system, sans-serif';
        ctx.textAlign = 'right';
        ctx.fillText(`${(level * 100).toFixed(1)}% (${price.toFixed(2)})`, w - 5, pixel - 3);

        // Fill between levels
        if (idx > 0) {
            const prevPrice = points[1].price + priceRange * FIB_LEVELS[idx - 1];
            const prevPixel = pane.series.priceToCoordinate(prevPrice);
            if (prevPixel !== null) {
                ctx.fillStyle = COLORS.fibFill;
                ctx.fillRect(0, Math.min(pixel, prevPixel), w, Math.abs(pixel - prevPixel));
            }
        }
    });
}

function drawFibExtension(ctx, points, pane, isHovered) {
    const p1 = toPixel(points[0], pane);
    const p2 = toPixel(points[1], pane);
    if (p1.x === null || p2.x === null) return;

    const canvas = pane.drawingCanvas;
    const w = canvas.width / window.devicePixelRatio;
    const priceRange = points[0].price - points[1].price;
    const color = isHovered ? COLORS.hover : '#9c27b0';

    FIB_EXT_LEVELS.forEach((level, idx) => {
        const price = points[1].price + priceRange * level;
        const pixel = pane.series.priceToCoordinate(price);
        if (pixel === null) return;

        ctx.beginPath();
        ctx.moveTo(0, pixel);
        ctx.lineTo(w, pixel);
        ctx.strokeStyle = color;
        ctx.lineWidth = level === 0 || level === 1 ? 1.5 : 1;
        ctx.setLineDash(level > 1 ? [6, 3] : []);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = color;
        ctx.font = '10px -apple-system, sans-serif';
        ctx.textAlign = 'right';
        ctx.fillText(`${(level * 100).toFixed(1)}% (${price.toFixed(2)})`, w - 5, pixel - 3);
    });
}

// ─── Delete ───────────────────────────────────────────────────────────────────
function deleteHoveredDrawing() {
    // Delete selected drawing (persists after click) or hovered drawing
    const target = selectedDrawing || hoveredDrawing;
    if (!target) return;
    const { paneId, index } = target;
    if (!drawings[paneId] || index >= drawings[paneId].length) return;

    drawings[paneId].splice(index, 1);
    hoveredDrawing = null;
    selectedDrawing = null;
    dragState = null;

    const pane = (window.panes || []).find(p => p.id === paneId);
    if (pane) {
        pane.drawingCanvas.classList.remove('interacting');
        redrawAll(pane);
    }
    saveDrawings();
}

function deleteAllDrawings() {
    if (!confirm('Delete all drawings?')) return;
    for (const key in drawings) {
        drawings[key] = [];
    }
    hoveredDrawing = null;
    selectedDrawing = null;
    (window.panes || []).forEach(p => redrawAll(p));
    saveDrawings();
}

// ─── Persistence ──────────────────────────────────────────────────────────────
function saveDrawings() {
    try {
        localStorage.setItem('chartDrawings', JSON.stringify(drawings));
    } catch (e) {}
}

function loadDrawings() {
    try {
        const saved = localStorage.getItem('chartDrawings');
        if (saved) drawings = JSON.parse(saved);
    } catch (e) {
        drawings = {};
    }
    // Backfill IDs onto pre-existing drawings so they can be referenced by alerts.
    let dirty = false;
    for (const list of Object.values(drawings)) {
        for (const d of (list || [])) {
            if (!d.id) { d.id = genDrawingId(); dirty = true; }
        }
    }
    if (dirty) saveDrawings();
}

// Expose state for pane access
window.drawingsState = { drawings, getActiveTool: () => activeTool };

})();
