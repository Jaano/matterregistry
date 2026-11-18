(function () {
  "use strict";

  var graph = document.getElementById("mr-network-graph");
  if (!graph) return;
  var svg = graph.querySelector(".mr-links-svg");
  var rooms = Array.from(graph.querySelectorAll(".mr-room"));
  var links = Array.from(document.querySelectorAll(".mr-link-list li[data-source][data-target]"));
  var nodes = new Map(Array.from(graph.querySelectorAll(".mr-node[data-node-id]")).map(function (node) {
    return [node.dataset.nodeId, node];
  }));
  var ns = "http://www.w3.org/2000/svg";
  var activeNode = null;
  var lastWidth = 0;
  var lastViewportWidth = 0;
  var hasCenteredViewport = false;
  var geometry = window.MRNetworkGeometry;
  if (!geometry) return;

  function point(x, y) { return { x: x, y: y }; }
  function distance(a, b) { return Math.hypot(b.x - a.x, b.y - a.y); }

  function linkWidth(link) {
    var level = link.dataset.lqi;
    if (level === "0") return 1;
    if (level === "1" || level === "2") return 2;
    if (level === "3") return 3;
    return link.dataset.network === "wifi" ? 2 : 1;
  }

  function nodeSlots(count) {
    var presets = {
      1: [[0.5, 0.56]],
      2: [[0.36, 0.55], [0.64, 0.55]],
      3: [[0.31, 0.43], [0.69, 0.43], [0.5, 0.67]],
      4: [[0.31, 0.43], [0.69, 0.43], [0.31, 0.68], [0.69, 0.68]],
      5: [[0.27, 0.35], [0.73, 0.35], [0.5, 0.55], [0.27, 0.75], [0.73, 0.75]]
    };
    if (presets[count]) return { slots: presets[count], columns: count > 2 ? 2 : count, rows: 1 };
    var columns = Math.ceil(Math.sqrt(count * 1.3));
    var rows = Math.ceil(count / columns);
    var slots = [];
    for (var i = 0; i < count; i += 1) {
      var row = Math.floor(i / columns);
      var column = i % columns;
      var inRow = Math.min(columns, count - row * columns);
      slots.push([0.5 + (column - (inRow - 1) / 2) * 0.62 / columns,
        0.35 + (row + 0.5) * 0.43 / rows]);
    }
    return { slots: slots, columns: columns, rows: rows };
  }

  function placeNodes(room, width, height) {
    var icons = Array.from(room.querySelectorAll(".mr-node"));
    var layout = nodeSlots(icons.length);
    var size = Math.min(40, width * 0.14,
      width * 0.62 / Math.max(layout.columns, 1) * 0.85,
      height * 0.43 / layout.rows * 0.85);
    icons.forEach(function (icon, index) {
      icon.style.left = (layout.slots[index][0] * 100).toFixed(2) + "%";
      icon.style.top = (layout.slots[index][1] * 100).toFixed(2) + "%";
      icon.style.setProperty("--mr-node-size", size.toFixed(1) + "px");
    });
  }

  function highlight() {
    svg.querySelectorAll("path").forEach(function (path) {
      var connected = activeNode && (path.dataset.source === activeNode || path.dataset.target === activeNode);
      path.classList.toggle("is-emphasized", Boolean(connected));
      path.classList.toggle("is-dimmed", Boolean(activeNode) && !connected);
    });
  }

  function drawLinks() {
    var box = graph.getBoundingClientRect();
    svg.setAttribute("viewBox", "0 0 " + box.width + " " + box.height);
    svg.replaceChildren();
    var nodeBounds = Array.from(nodes, function (item) {
      var rect = item[1].getBoundingClientRect();
      var room = item[1].closest(".mr-room");
      return { id: item[0], x: rect.left + rect.width / 2 - box.left,
        y: rect.top + rect.height / 2 - box.top, radius: Math.min(rect.width, rect.height) / 2,
        room: rooms.indexOf(room) };
    });
    var occurrences = new Map();
    var linkData = links.map(function (link) {
      var identity = JSON.stringify([link.dataset.source, link.dataset.target,
        link.dataset.network]);
      var occurrence = occurrences.get(identity) || 0;
      occurrences.set(identity, occurrence + 1);
      return { id: identity + ":" + occurrence,
        source: link.dataset.source, target: link.dataset.target,
        width: linkWidth(link) };
    });
    geometry.routeGraph(nodeBounds, linkData, { width: box.width, height: box.height })
      .forEach(function (route) {
      if (!route) return;
      var link = links[route.index];
      var path = document.createElementNS(ns, "path");
      path.setAttribute("d", route.d);
      path.dataset.source = link.dataset.source;
      path.dataset.target = link.dataset.target;
      path.dataset.lqi = link.dataset.lqi || "unknown";
      var network = (link.dataset.network || "").toLowerCase().replace(/[^a-z0-9_-]/g, "");
      path.dataset.network = network;
      path.style.stroke = network
        ? "var(--mr-net-" + network + ", var(--mr-primary))"
        : "var(--mr-primary)";
      svg.appendChild(path);
      });
    highlight();
  }

  function layout() {
    if (links.length > 40) graph.style.minWidth = "1295px";
    var available = graph.clientWidth;
    if (!available || !rooms.length) return;
    var viewport = graph.parentElement;
    var viewportWidth = viewport.clientWidth;
    var shouldCenter = !hasCenteredViewport || available !== lastWidth ||
      viewportWidth !== lastViewportWidth;
    var gap = 8;
    var outerPadding = 20;
    var columns = available >= 1050 ? 4 : available >= 760 ? 3 : available >= 490 ? 2 : 1;
    columns = Math.min(columns, rooms.length);
    var rows = Math.ceil(rooms.length / columns);
    var span = columns + (rows > 1 ? 0.5 : 0);
    var gapCount = columns - 1 + (rows > 1 ? 0.5 : 0);
    var width = Math.max(1, Math.min(280,
      (available - 2 * outerPadding - gapCount * gap) / span));
    function positionsFor(roomWidth) {
      var roomHeight = roomWidth * 6 / 7;
      return rooms.map(function (_, index) {
        var row = Math.floor(index / columns);
        var column = index % columns;
        return point((column + (row % 2) * 0.5) * (roomWidth + gap),
          row * (roomHeight * 0.75 + gap * 0.75));
      });
    }
    var positions = positionsFor(width);
    var center = point(
      positions.reduce(function (sum, position) { return sum + position.x; }, 0) / positions.length,
      positions.reduce(function (sum, position) { return sum + position.y; }, 0) / positions.length
    );
    var centerSlot = positions.map(function (_, index) { return index; }).sort(function (a, b) {
      return distance(positions[a], center) - distance(positions[b], center) || a - b;
    })[0];
    var height = width * 6 / 7;
    var slots = positions.map(function (_, index) { return index; }).sort(function (a, b) {
      if (a === centerSlot) return -1;
      if (b === centerSlot) return 1;
      return distance(positions[a], positions[centerSlot]) -
        distance(positions[b], positions[centerSlot]) || a - b;
    });
    var minX = Math.min.apply(null, positions.map(function (position) { return position.x; }));
    var maxX = Math.max.apply(null, positions.map(function (position) {
      return position.x + width;
    }));
    var minY = Math.min.apply(null, positions.map(function (position) { return position.y; }));
    var maxY = Math.max.apply(null, positions.map(function (position) {
      return position.y + height;
    }));
    var graphHeight = Math.ceil(maxY - minY + 2 * outerPadding);
    var left = (available - maxX + minX) / 2 - minX;
    var top = (graphHeight - maxY + minY) / 2 - minY;
    graph.style.height = graphHeight + "px";
    graph.classList.add("is-laid-out");
    rooms.forEach(function (room, rank) {
      var position = positions[slots[rank]];
      room.style.left = (left + position.x).toFixed(1) + "px";
      room.style.top = (top + position.y).toFixed(1) + "px";
      room.style.width = width.toFixed(1) + "px";
      room.style.height = height.toFixed(1) + "px";
      placeNodes(room, width, height);
    });
    drawLinks();
    if (viewportWidth < available && shouldCenter) {
      viewport.scrollLeft = (available - viewportWidth) / 2;
    } else if (viewportWidth >= available) {
      viewport.scrollLeft = 0;
    }
    hasCenteredViewport = viewportWidth < available;
    lastWidth = available;
    lastViewportWidth = viewportWidth;
  }

  graph.addEventListener("pointermove", function (event) {
    var node = event.target.closest(".mr-node");
    if (node && node.dataset.nodeId !== activeNode) {
      activeNode = node.dataset.nodeId;
      highlight();
    }
  });
  graph.addEventListener("pointerout", function (event) {
    if (!event.relatedTarget || !event.relatedTarget.closest(".mr-node")) {
      activeNode = null;
      highlight();
    }
  });
  graph.addEventListener("focusin", function (event) {
    var node = event.target.closest(".mr-node");
    if (node) { activeNode = node.dataset.nodeId; highlight(); }
  });
  graph.addEventListener("focusout", function () { activeNode = null; highlight(); });
  window.addEventListener("pageshow", function () { activeNode = null; highlight(); });

  layout();
  if (window.ResizeObserver) {
    new ResizeObserver(function () {
      var width = graph.clientWidth;
      if (width !== lastWidth || graph.parentElement.clientWidth !== lastViewportWidth) layout();
    }).observe(graph.parentElement);
  }
  window.addEventListener("resize", layout);
})();
