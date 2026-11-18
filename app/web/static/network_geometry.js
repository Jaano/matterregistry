(function (root, factory) {
  "use strict";
  var geometry = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = geometry;
  if (root) root.MRNetworkGeometry = geometry;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  var TAU = Math.PI * 2;
  var CLEARANCE = 4;
  var FILLET = 8;
  var LANE_STEP = 5.25;
  var CARDINAL = [
    { x: 1, y: 0, angle: 0, axis: "horizontal" },
    { x: 0, y: 1, angle: Math.PI / 2, axis: "vertical" },
    { x: -1, y: 0, angle: Math.PI, axis: "horizontal" },
    { x: 0, y: -1, angle: Math.PI * 1.5, axis: "vertical" }
  ];

  function point(x, y) { return { x: x, y: y }; }
  function add(a, b) { return point(a.x + b.x, a.y + b.y); }
  function subtract(a, b) { return point(a.x - b.x, a.y - b.y); }
  function scale(a, value) { return point(a.x * value, a.y * value); }
  function length(a) { return Math.hypot(a.x, a.y); }
  function distance(a, b) { return length(subtract(a, b)); }
  function unit(angle) { return point(Math.cos(angle), Math.sin(angle)); }
  function coord(p) { return p.x.toFixed(2) + " " + p.y.toFixed(2); }
  function normalize(angle) { return (angle % TAU + TAU) % TAU; }
  function angleDistance(a, b) {
    var difference = Math.abs(normalize(a - b));
    return Math.min(difference, TAU - difference);
  }

  function allocatePorts(nodes, links) {
    var byNode = new Map(nodes.map(function (node) { return [node.id, []]; }));
    var nodeById = new Map(nodes.map(function (node) { return [node.id, node]; }));
    links.forEach(function (link, index) {
      var source = nodeById.get(link.source);
      var target = nodeById.get(link.target);
      if (!source || !target || source.id === target.id) return;
      var bearing = Math.atan2(target.y - source.y, target.x - source.x);
      var key = String(link.source) + "|" + String(link.target) + "|" + String(link.id ?? index);
      byNode.get(source.id).push({ index: index, end: "source", desired: normalize(bearing),
        key: key, width: link.width || 1 });
      byNode.get(target.id).push({ index: index, end: "target", desired: normalize(bearing + Math.PI),
        key: key, width: link.width || 1 });
    });
    var assigned = new Map();
    var slotCounts = new Map();
    var incidentKeys = new Map();
    nodes.forEach(function (node) {
      var incident = byNode.get(node.id);
      if (!incident.length) return;
      incidentKeys.set(node.id, incident.map(function (item) {
        return item.index + ":" + item.end;
      }));
      incident.sort(function (a, b) { return a.desired - b.desired || a.key.localeCompare(b.key); });
      if (incident.length === 1) {
        assigned.set(incident[0].index + ":" + incident[0].end, incident[0].desired);
        return;
      }
      var widest = Math.max.apply(null, incident.map(function (item) { return item.width; }));
      var gap = widest + 2;
      var radius = Math.max(node.radius, 1);
      var separation = 2 * Math.asin(Math.min(1, gap / (2 * radius)));
      var slots = Math.max(incident.length, Math.floor(TAU / separation));
      slotCounts.set(node.id, slots);
      var step = TAU / slots;
      var occupied = new Set();
      incident.forEach(function (item) {
        var best = -1;
        var bestDistance = Infinity;
        for (var slot = 0; slot < slots; slot += 1) {
          if (occupied.has(slot)) continue;
          var difference = angleDistance(item.desired, slot * step);
          if (difference < bestDistance - 1e-9) { best = slot; bestDistance = difference; }
        }
        occupied.add(best);
        assigned.set(item.index + ":" + item.end, best * step);
      });
    });
    return { angles: assigned, slots: slotCounts, incidentKeys: incidentKeys,
      incidents: byNode };
  }

  function allocateAngles(nodes, links) {
    return allocatePorts(nodes, links).angles;
  }

  function headingsFor(angle) {
    return CARDINAL.filter(function (heading) {
      return angleDistance(angle, heading.angle) <= Math.PI / 2 + 1e-8;
    }).sort(function (a, b) {
      return angleDistance(angle, a.angle) - angleDistance(angle, b.angle) || a.angle - b.angle;
    });
  }

  function endpoint(node, angle, heading, incoming, fillet) {
    var outward = unit(angle);
    var radial = incoming ? scale(outward, -1) : outward;
    var rim = add(node, scale(outward, node.radius));
    var port = add(node, scale(outward, node.radius + CLEARANCE));
    if (angleDistance(Math.atan2(radial.y, radial.x), heading.angle) < 1e-7) fillet = 0;
    if (incoming) {
      var controlIn = subtract(port, scale(radial, fillet));
      return { rim: rim, port: port, control: controlIn,
        axisPoint: subtract(controlIn, scale(heading, fillet)), fillet: fillet };
    }
    var controlOut = add(port, scale(radial, fillet));
    return { rim: rim, port: port, control: controlOut,
      axisPoint: add(controlOut, scale(heading, fillet)), fillet: fillet };
  }

  function roundedOrthogonal(points) {
    var result = { text: "", samples: [points[0]] };
    for (var i = 1; i < points.length - 1; i += 1) {
      var before = points[i - 1];
      var corner = points[i];
      var after = points[i + 1];
      var first = distance(before, corner);
      var second = distance(corner, after);
      var radius = Math.min(10, first / 2, second / 2);
      if (radius < 0.5) {
        result.text += " L " + coord(corner);
        result.samples.push(corner);
        continue;
      }
      var entry = add(corner, scale(subtract(before, corner), radius / first));
      var exit = add(corner, scale(subtract(after, corner), radius / second));
      result.text += " L " + coord(entry) + " Q " + coord(corner) + " " + coord(exit);
      result.samples.push(entry);
      for (var step = 1; step <= 6; step += 1) {
        result.samples.push(quadratic(entry, corner, exit, step / 6));
      }
    }
    result.text += " L " + coord(points[points.length - 1]);
    result.samples.push(points[points.length - 1]);
    return result;
  }

  function quadratic(a, control, b, t) {
    var back = 1 - t;
    return point(back * back * a.x + 2 * back * t * control.x + t * t * b.x,
      back * back * a.y + 2 * back * t * control.y + t * t * b.y);
  }

  function samplesForCurve(a, control, b) {
    var samples = [a];
    for (var i = 1; i <= 8; i += 1) samples.push(quadratic(a, control, b, i / 8));
    return samples;
  }

  function samplesLength(samples) {
    var total = 0;
    for (var i = 1; i < samples.length; i += 1) total += distance(samples[i - 1], samples[i]);
    return total;
  }

  function addSegments(result, samples, width, part) {
    for (var i = 1; i < samples.length; i += 1) {
      if (distance(samples[i - 1], samples[i]) > 0.01) {
        result.push({ a: samples[i - 1], b: samples[i], width: width, part: part });
      }
    }
  }

  function buildRoute(entry, candidate) {
    var start = candidate.start;
    var end = candidate.end;
    var orthogonal = roundedOrthogonal(candidate.points);
    var text = "M " + coord(entry.source) + " L " + coord(start.port);
    if (start.fillet) text += " Q " + coord(start.control) + " " + coord(start.axisPoint);
    text += orthogonal.text;
    if (end.fillet) text += " Q " + coord(end.control) + " " + coord(end.port);
    else text += " L " + coord(end.port);
    text += " L " + coord(entry.target);

    var segments = [];
    addSegments(segments, [start.rim, start.port], entry.width, "sourceRadial");
    if (start.fillet) addSegments(segments,
      samplesForCurve(start.port, start.control, start.axisPoint), entry.width, "sourceFillet");
    addSegments(segments, orthogonal.samples, entry.width, "orthogonal");
    if (end.fillet) addSegments(segments,
      samplesForCurve(end.axisPoint, end.control, end.port), entry.width, "targetFillet");
    addSegments(segments, [end.port, end.rim], entry.width, "targetRadial");
    return { index: entry.index, source: entry.source.id, target: entry.target.id,
      sourceAngle: entry.sourceAngle, targetAngle: entry.targetAngle,
      sourcePort: start.port, targetPort: end.port, sourceRim: start.rim, targetRim: end.rim,
      sourceFilletStart: start.port, targetFilletEnd: end.port,
      d: text, segments: segments, points: candidate.points, start: start, end: end,
      cost: candidate.cost,
      width: entry.width };
  }

  function headingMatches(a, b, heading) {
    var delta = subtract(b, a);
    return length(delta) > 0.5 &&
      (delta.x * heading.x + delta.y * heading.y) > 0.5;
  }

  function candidate(entry, sourceHeading, targetHeading, points, fillet) {
    if (points.length < 2 || !headingMatches(points[0], points[1], sourceHeading) ||
        !headingMatches(points[points.length - 2], points[points.length - 1], targetHeading)) return null;
    var travel = 0;
    for (var i = 1; i < points.length; i += 1) travel += distance(points[i - 1], points[i]);
    var turnCost = angleDistance(entry.sourceAngle, sourceHeading.angle) +
      angleDistance(normalize(entry.targetAngle + Math.PI), targetHeading.angle);
    var start = endpoint(entry.source, entry.sourceAngle, sourceHeading, false, fillet);
    var end = endpoint(entry.target, entry.targetAngle, targetHeading, true, fillet);
    travel += entry.source.radius + entry.target.radius + 2 * CLEARANCE;
    if (start.fillet) travel += samplesLength(samplesForCurve(start.port,
      start.control, start.axisPoint));
    if (end.fillet) travel += samplesLength(samplesForCurve(end.axisPoint,
      end.control, end.port));
    return { start: start, end: end, points: points,
      cost: travel + (points.length - 2) * 8 + turnCost * 12 };
  }

  function middleValues(base, nodes, axis, limit) {
    var values = [base];
    for (var step = 1; step <= 40; step += 1) {
      values.push(base + step * LANE_STEP, base - step * LANE_STEP);
    }
    nodes.forEach(function (node) {
      var location = axis === "x" ? node.x : node.y;
      var offset = node.radius + 7;
      values.push(location - offset, location + offset);
    });
    values.push(7, limit - 7);
    return Array.from(new Set(values.map(function (value) { return Math.round(value * 100) / 100; })))
      .filter(function (value) { return value >= 0 && value <= limit; })
      .sort(function (a, b) { return Math.abs(a - base) - Math.abs(b - base) || a - b; });
  }

  function routeCandidates(entry, nodes, bounds) {
    var sourceHeadings = headingsFor(entry.sourceAngle);
    var targetHeadings = headingsFor(normalize(entry.targetAngle + Math.PI));
    var fillet = Math.min(FILLET, Math.max(2, distance(entry.source, entry.target) / 12));
    var routes = [];
    sourceHeadings.forEach(function (sourceHeading) {
      targetHeadings.forEach(function (targetHeading) {
        var start = endpoint(entry.source, entry.sourceAngle, sourceHeading, false, fillet);
        var end = endpoint(entry.target, entry.targetAngle, targetHeading, true, fillet);
        var a = start.axisPoint;
        var b = end.axisPoint;
        var options = [];
        if (sourceHeading.axis === "horizontal" && targetHeading.axis === "horizontal") {
          if (Math.abs(a.y - b.y) < 0.1) options.push([a, b]);
          middleValues((a.x + b.x) / 2, nodes, "x", bounds.width).forEach(function (middle) {
            options.push([a, point(middle, a.y), point(middle, b.y), b]);
          });
        } else if (sourceHeading.axis === "vertical" && targetHeading.axis === "vertical") {
          if (Math.abs(a.x - b.x) < 0.1) options.push([a, b]);
          middleValues((a.y + b.y) / 2, nodes, "y", bounds.height).forEach(function (middle) {
            options.push([a, point(a.x, middle), point(b.x, middle), b]);
          });
        } else if (sourceHeading.axis === "horizontal") {
          options.push([a, point(b.x, a.y), b]);
          [20, 40, 70].forEach(function (offset) {
            var x = a.x + sourceHeading.x * offset;
            var y = b.y - targetHeading.y * offset;
            options.push([a, point(x, a.y), point(x, y), point(b.x, y), b]);
          });
        } else {
          options.push([a, point(a.x, b.y), b]);
          [20, 40, 70].forEach(function (offset) {
            var y = a.y + sourceHeading.y * offset;
            var x = b.x - targetHeading.x * offset;
            options.push([a, point(a.x, y), point(x, y), point(x, b.y), b]);
          });
        }
        options.forEach(function (points) {
          var proposed = candidate(entry, sourceHeading, targetHeading, points, fillet);
          if (proposed) routes.push(proposed);
        });
      });
    });
    return routes.sort(function (a, b) { return a.cost - b.cost; });
  }

  function distanceToSegment(p, a, b) {
    var delta = subtract(b, a);
    var squared = delta.x * delta.x + delta.y * delta.y;
    if (!squared) return distance(p, a);
    var t = Math.max(0, Math.min(1,
      ((p.x - a.x) * delta.x + (p.y - a.y) * delta.y) / squared));
    return distance(p, add(a, scale(delta, t)));
  }

  function properCrossing(first, second) {
    function cross(a, b, c) {
      return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
    }
    var firstA = cross(first.a, first.b, second.a);
    var firstB = cross(first.a, first.b, second.b);
    var secondA = cross(second.a, second.b, first.a);
    var secondB = cross(second.a, second.b, first.b);
    return firstA * firstB < -1e-6 && secondA * secondB < -1e-6;
  }

  function segmentOverlap(first, second) {
    var separation = (first.width + second.width) / 2 + 2;
    var deltaA = subtract(first.b, first.a);
    var deltaB = subtract(second.b, second.a);
    var lengthA = length(deltaA);
    var lengthB = length(deltaB);
    if (Math.max(first.a.x, first.b.x) + separation < Math.min(second.a.x, second.b.x) ||
        Math.max(second.a.x, second.b.x) + separation < Math.min(first.a.x, first.b.x) ||
        Math.max(first.a.y, first.b.y) + separation < Math.min(second.a.y, second.b.y) ||
        Math.max(second.a.y, second.b.y) + separation < Math.min(first.a.y, first.b.y)) return false;
    if (properCrossing(first, second)) return false;
    var cosine = Math.abs((deltaA.x * deltaB.x + deltaA.y * deltaB.y) / (lengthA * lengthB));
    if (cosine < Math.cos(Math.PI / 12)) return false;
    var axis = scale(deltaA, 1 / lengthA);
    var projectionA = [first.a.x * axis.x + first.a.y * axis.y,
      first.b.x * axis.x + first.b.y * axis.y].sort(function (a, b) { return a - b; });
    var projectionB = [second.a.x * axis.x + second.a.y * axis.y,
      second.b.x * axis.x + second.b.y * axis.y].sort(function (a, b) { return a - b; });
    if (Math.min(projectionA[1], projectionB[1]) - Math.max(projectionA[0], projectionB[0]) < 0.5) return false;
    var gap = Math.min(distanceToSegment(first.a, second.a, second.b),
      distanceToSegment(first.b, second.a, second.b),
      distanceToSegment(second.a, first.a, first.b),
      distanceToSegment(second.b, first.a, first.b));
    return gap < separation - 1e-6;
  }

  function routeValid(route, nodes, bounds) {
    for (var i = 0; i < route.segments.length; i += 1) {
      var segment = route.segments[i];
      var margin = segment.width / 2;
      if ([segment.a, segment.b].some(function (p) {
        return p.x < margin || p.y < margin || p.x > bounds.width - margin ||
          p.y > bounds.height - margin;
      })) return false;
      for (var j = 0; j < nodes.length; j += 1) {
        var node = nodes[j];
        if (node.id === route.source &&
            (segment.part === "sourceRadial" || segment.part === "direct")) continue;
        if (node.id === route.target &&
            (segment.part === "targetRadial" || segment.part === "direct")) continue;
        var obstacle = node.radius + margin + 0.25;
        if (node.x + obstacle < Math.min(segment.a.x, segment.b.x) ||
            node.x - obstacle > Math.max(segment.a.x, segment.b.x) ||
            node.y + obstacle < Math.min(segment.a.y, segment.b.y) ||
            node.y - obstacle > Math.max(segment.a.y, segment.b.y)) continue;
        if (distanceToSegment(node, segment.a, segment.b) < obstacle) return false;
      }
    }
    return true;
  }

  function occupiedIndex() {
    var cells = new Map();
    var cellSize = 32;
    function keys(segment, padding) {
      var result = [];
      var left = Math.floor((Math.min(segment.a.x, segment.b.x) - padding) / cellSize);
      var right = Math.floor((Math.max(segment.a.x, segment.b.x) + padding) / cellSize);
      var top = Math.floor((Math.min(segment.a.y, segment.b.y) - padding) / cellSize);
      var bottom = Math.floor((Math.max(segment.a.y, segment.b.y) + padding) / cellSize);
      for (var x = left; x <= right; x += 1) {
        for (var y = top; y <= bottom; y += 1) result.push(x + ":" + y);
      }
      return result;
    }
    return {
      add: function (segments) {
        segments.forEach(function (segment) {
          keys(segment, 0).forEach(function (key) {
            if (!cells.has(key)) cells.set(key, []);
            cells.get(key).push(segment);
          });
        });
      },
      near: function (segment) {
        var nearby = new Set();
        keys(segment, 5).forEach(function (key) {
          (cells.get(key) || []).forEach(function (other) { nearby.add(other); });
        });
        return nearby;
      }
    };
  }

  function findRoute(entry, occupied, nodes, bounds) {
    var candidates = routeCandidates(entry, nodes, bounds);
    var fallback = null;
    for (var i = 0; i < candidates.length; i += 1) {
      var route = buildRoute(entry, candidates[i]);
      if (!routeValid(route, nodes, bounds)) continue;
      var conflicts = 0;
      for (var j = 0; j < route.segments.length; j += 1) {
        for (var other of occupied.near(route.segments[j])) {
          if (segmentOverlap(route.segments[j], other)) conflicts += 1;
          if (fallback && conflicts >= fallback.conflicts) break;
        }
        if (fallback && conflicts >= fallback.conflicts) break;
      }
      if (!conflicts) { route.conflicts = 0; return route; }
      if (!fallback || conflicts < fallback.conflicts) {
        route.conflicts = conflicts;
        fallback = route;
      }
    }
    return fallback;
  }

  function directRoute(entry, assigned, ports, occupied, nodes, bounds) {
    if (entry.source.room === undefined || entry.source.room < 0 ||
        entry.source.room !== entry.target.room) return null;
    var bearing = Math.atan2(entry.target.y - entry.source.y,
      entry.target.x - entry.source.x);
    var sourceAngle = normalize(bearing);
    var targetAngle = normalize(bearing + Math.PI);
    function hasRoomAt(node, angle) {
      return (ports.incidents.get(node.id) || []).every(function (incident) {
        if (incident.index === entry.index) return true;
        var otherAngle = assigned.get(incident.index + ":" + incident.end);
        var chord = 2 * node.radius * Math.sin(angleDistance(angle, otherAngle) / 2);
        return chord >= (entry.width + incident.width) / 2 + 2;
      });
    }
    if (!hasRoomAt(entry.source, sourceAngle) ||
        !hasRoomAt(entry.target, targetAngle)) return null;
    var direction = unit(bearing);
    var sourceRim = add(entry.source, scale(direction, entry.source.radius));
    var targetRim = subtract(entry.target, scale(direction, entry.target.radius));
    var sourcePort = add(sourceRim, scale(direction, CLEARANCE));
    var targetPort = subtract(targetRim, scale(direction, CLEARANCE));
    if (distance(sourcePort, targetPort) < 1) return null;
    var segment = { a: sourceRim, b: targetRim, width: entry.width, part: "direct" };
    var route = { index: entry.index, source: entry.source.id, target: entry.target.id,
      sourceAngle: sourceAngle, targetAngle: targetAngle,
      sourcePort: sourcePort, targetPort: targetPort,
      sourceRim: sourceRim, targetRim: targetRim,
      d: "M " + coord(entry.source) + " L " + coord(entry.target),
      segments: [segment], points: [sourcePort, targetPort],
      cost: distance(entry.source, entry.target), width: entry.width, conflicts: 0 };
    if (!routeValid(route, nodes, bounds)) return null;
    for (var other of occupied.near(segment)) {
      if (segmentOverlap(segment, other)) return null;
    }
    return route;
  }

  function nearbyAngles(nodeId, key, assigned, slotCounts, incidentKeys) {
    var current = assigned.get(key);
    var slots = slotCounts.get(nodeId);
    if (!slots) return [current];
    var step = TAU / slots;
    var currentSlot = Math.round(current / step) % slots;
    var used = new Set();
    (incidentKeys.get(nodeId) || []).forEach(function (otherKey) {
      if (otherKey === key) return;
      used.add(Math.round(assigned.get(otherKey) / step) % slots);
    });
    var choices = [current];
    for (var offset = 1; offset <= Math.min(3, slots - 1); offset += 1) {
      [offset, -offset].forEach(function (signed) {
        var slot = (currentSlot + signed + slots) % slots;
        if (!used.has(slot) && !choices.some(function (angle) {
          return angleDistance(angle, slot * step) < 1e-7;
        })) choices.push(slot * step);
      });
    }
    return choices;
  }

  function findRouteWithPorts(entry, assigned, ports, occupied, nodes, bounds) {
    var sourceKey = entry.index + ":source";
    var targetKey = entry.index + ":target";
    var straight = directRoute(entry, assigned, ports, occupied, nodes, bounds);
    if (straight) {
      assigned.set(sourceKey, straight.sourceAngle);
      assigned.set(targetKey, straight.targetAngle);
      return straight;
    }
    var sourceChoices = nearbyAngles(entry.source.id, sourceKey, assigned,
      ports.slots, ports.incidentKeys);
    var targetChoices = nearbyAngles(entry.target.id, targetKey, assigned,
      ports.slots, ports.incidentKeys);
    var pairs = [[sourceChoices[0], targetChoices[0]]];
    sourceChoices.slice(1).forEach(function (angle) { pairs.push([angle, targetChoices[0]]); });
    targetChoices.slice(1).forEach(function (angle) { pairs.push([sourceChoices[0], angle]); });
    sourceChoices.slice(1).forEach(function (source) {
      targetChoices.slice(1).forEach(function (target) { pairs.push([source, target]); });
    });
    var best = null;
    pairs.forEach(function (pair) {
      if (best && best.conflicts === 0) return;
      var candidateEntry = Object.assign({}, entry,
        { sourceAngle: pair[0], targetAngle: pair[1] });
      var route = findRoute(candidateEntry, occupied, nodes, bounds);
      if (route && (!best || route.conflicts < best.conflicts ||
          (route.conflicts === best.conflicts && route.cost < best.cost))) best = route;
    });
    if (best) {
      assigned.set(sourceKey, best.sourceAngle);
      assigned.set(targetKey, best.targetAngle);
    }
    return best;
  }

  function routeGraph(nodes, links, bounds) {
    var nodeById = new Map(nodes.map(function (node) { return [node.id, node]; }));
    var ports = allocatePorts(nodes, links);
    var angles = ports.angles;
    var entries = links.map(function (link, index) {
      var source = nodeById.get(link.source);
      var target = nodeById.get(link.target);
      if (!source || !target || source.id === target.id) return null;
      return { index: index, source: source, target: target, width: link.width || 1,
        sourceAngle: angles.get(index + ":source"), targetAngle: angles.get(index + ":target"),
        bearing: Math.atan2(target.y - source.y, target.x - source.x),
        sameRoom: source.room !== undefined && source.room >= 0 &&
          source.room === target.room };
    }).filter(Boolean);
    var orders = [
      entries.slice().sort(function (a, b) {
        return Number(b.sameRoom) - Number(a.sameRoom) ||
          (a.sameRoom ? distance(a.source, a.target) - distance(b.source, b.target) : 0) ||
          a.bearing - b.bearing || a.index - b.index;
      }),
      entries,
      entries.slice().reverse()
    ];
    var best = null;
    for (var attempt = 0; attempt < orders.length; attempt += 1) {
      var occupied = occupiedIndex();
      var chosenAngles = new Map(angles);
      var routes = new Map();
      var conflicts = 0;
      orders[attempt].forEach(function (entry) {
        var route = findRouteWithPorts(entry, chosenAngles, ports,
          occupied, nodes, bounds);
        if (!route) return;
        routes.set(entry.index, route);
        conflicts += route.conflicts;
        occupied.add(route.segments);
      });
      if (!best || routes.size > best.routes.size ||
          (routes.size === best.routes.size && conflicts < best.conflicts)) {
        best = { routes: routes, conflicts: conflicts };
      }
      if (routes.size === entries.length && conflicts === 0) break;
    }
    return entries.map(function (entry) { return best.routes.get(entry.index) || null; });
  }

  return { allocateAngles: allocateAngles, routeGraph: routeGraph, routeCandidates: routeCandidates,
    routeValid: routeValid, segmentOverlap: segmentOverlap, CLEARANCE: CLEARANCE };
});
