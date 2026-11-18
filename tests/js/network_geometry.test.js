"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const geometry = require("../../app/web/static/network_geometry.js");

function distance(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }
function delta(a, b) { return { x: b.x - a.x, y: b.y - a.y }; }
function cross(a, b) { return a.x * b.y - a.y * b.x; }
function near(actual, expected, tolerance = 0.02) {
  assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} is not near ${expected}`);
}

test("visible same-room neighbours use straight center-to-center links", () => {
  const nodes = [
    { id: "A", x: 100, y: 100, radius: 18, room: 0 },
    { id: "B", x: 240, y: 100, radius: 18, room: 0 },
    { id: "C", x: 170, y: 220, radius: 18, room: 0 }
  ];
  const links = [
    { id: "A-B", source: "A", target: "B", width: 2 },
    { id: "A-C", source: "A", target: "C", width: 2 },
    { id: "B-C", source: "B", target: "C", width: 2 }
  ];
  const bounds = { width: 340, height: 300 };
  const routes = geometry.routeGraph(nodes, links, bounds);
  assert.ok(routes.every(route => route && route.conflicts === 0));
  assert.deepEqual(routes.map(route => route.d), [
    "M 100.00 100.00 L 240.00 100.00",
    "M 100.00 100.00 L 170.00 220.00",
    "M 240.00 100.00 L 170.00 220.00"
  ]);
  assert.ok(routes.every(route => route.segments.length === 1 &&
    route.segments[0].part === "direct" && geometry.routeValid(route, nodes, bounds)));
  assert.equal(new Set(routes.filter(route => route.source === "A")
    .map(route => route.sourceAngle)).size, 2);
});

test("a blocked neighbour or a different room keeps elbow routing", () => {
  const nodes = [
    { id: "A", x: 100, y: 100, radius: 18, room: 0 },
    { id: "B", x: 300, y: 150, radius: 18, room: 0 },
    { id: "blocker", x: 200, y: 125, radius: 18, room: 0 }
  ];
  const links = [{ id: "A-B", source: "A", target: "B", width: 2 }];
  const bounds = { width: 500, height: 300 };
  const [blocked] = geometry.routeGraph(nodes, links, bounds);
  assert.ok(blocked && blocked.segments.some(segment => segment.part === "orthogonal"));
  nodes[2].x = 400;
  nodes[1].room = 1;
  const [interRoom] = geometry.routeGraph(nodes, links, bounds);
  assert.ok(interRoom && interRoom.segments.some(segment => segment.part === "orthogonal"));
});

test("one connector leaves both circles radially and starts each fillet 4 px beyond the rim", () => {
  const nodes = [
    { id: "A", x: 100, y: 100, radius: 20 },
    { id: "B", x: 320, y: 220, radius: 16 }
  ];
  const [route] = geometry.routeGraph(nodes, [{ id: "A-B", source: "A", target: "B", width: 3 }],
    { width: 450, height: 350 });
  assert.ok(route);
  near(distance(nodes[0], route.sourcePort), nodes[0].radius + 4);
  near(distance(nodes[1], route.targetPort), nodes[1].radius + 4);
  near(distance(nodes[0], route.sourceRim), nodes[0].radius);
  near(distance(nodes[1], route.targetRim), nodes[1].radius);
  assert.match(route.d, /^M 100\.00 100\.00 L /);
  assert.equal(route.segments[0].part, "sourceRadial");
  near(distance(route.segments[0].a, nodes[0]), nodes[0].radius);
  assert.ok(geometry.routeValid(route, nodes, { width: 450, height: 350 }));

  near(cross(delta(nodes[0], route.sourcePort),
    delta(route.start.port, route.start.control)), 0);
  near(cross(delta(route.start.control, route.start.axisPoint),
    delta(route.points[0], route.points[1])), 0);
  near(cross(delta(route.points.at(-2), route.points.at(-1)),
    delta(route.end.axisPoint, route.end.control)), 0);
  near(cross(delta(route.end.control, route.end.port),
    delta(route.targetPort, nodes[1])), 0);
  route.points.slice(1).forEach((point, index) => {
    const before = route.points[index];
    assert.ok(Math.abs(point.x - before.x) < 0.01 || Math.abs(point.y - before.y) < 0.01);
  });
});

test("links at one device get distinct, stable bearings even for collinear peers", () => {
  const nodes = [
    { id: "A", x: 100, y: 100, radius: 20 },
    { id: "B", x: 300, y: 100, radius: 20 },
    { id: "C", x: 400, y: 100, radius: 20 }
  ];
  const links = [
    { id: "A-B", source: "A", target: "B", width: 3 },
    { id: "A-C", source: "A", target: "C", width: 2 }
  ];
  const angles = geometry.allocateAngles(nodes, links);
  const first = angles.get("0:source");
  const second = angles.get("1:source");
  assert.notEqual(first, second);
  const chord = 2 * nodes[0].radius * Math.sin(Math.abs(first - second) / 2);
  assert.ok(chord >= (3 + 2) / 2 + 2);
  const reversed = geometry.allocateAngles(nodes, links.slice().reverse());
  near(first, reversed.get("1:source"));
  near(second, reversed.get("0:source"));
});

test("parallel links to one peer use distinct ports and separate routes", () => {
  const nodes = [
    { id: "A", x: 100, y: 100, radius: 20 },
    { id: "B", x: 350, y: 100, radius: 20 }
  ];
  const links = ["one", "two", "three"].map(id =>
    ({ id, source: "A", target: "B", width: 3 }));
  const routes = geometry.routeGraph(nodes, links, { width: 450, height: 220 });
  assert.ok(routes.every(Boolean));
  assert.ok(routes.every(route => route.conflicts === 0));
  assert.equal(new Set(routes.map(route => route.sourceAngle)).size, 3);
  assert.equal(new Set(routes.map(route => route.targetAngle)).size, 3);
});

test("high-degree ports remain distinct when their full stroke spacing cannot fit", () => {
  const hub = { id: "hub", x: 400, y: 400, radius: 16 };
  const nodes = [hub];
  const links = [];
  for (let index = 0; index < 40; index += 1) {
    const angle = index * Math.PI * 2 / 40;
    nodes.push({ id: String(index), x: 400 + 250 * Math.cos(angle),
      y: 400 + 250 * Math.sin(angle), radius: 16 });
    links.push({ id: index, source: "hub", target: String(index), width: 3 });
  }
  const angles = geometry.allocateAngles(nodes, links);
  assert.equal(new Set(links.map((_, index) => angles.get(index + ":source"))).size, 40);
  const routes = geometry.routeGraph(nodes, links, { width: 800, height: 800 });
  assert.ok(routes.every(Boolean));
  assert.equal(new Set(routes.map(route => route.sourceAngle)).size, 40);
});

test("near-edge and resized circles retain the radial clearance and a route", () => {
  for (const radius of [12, 18, 24]) {
    const nodes = [
      { id: "edge", x: radius + 5, y: radius + 5, radius },
      { id: "other", x: 340, y: 210, radius }
    ];
    const [route] = geometry.routeGraph(nodes,
      [{ id: "edge-other", source: "edge", target: "other", width: 2 }],
      { width: 400, height: 260 });
    assert.ok(route, `missing route for radius ${radius}`);
    near(distance(nodes[0], route.sourcePort), radius + 4);
    near(distance(nodes[1], route.targetPort), radius + 4);
  }
});

test("dense 40-device mesh keeps all links and separate routing lanes", () => {
  const nodes = Array.from({ length: 40 }, (_, index) => ({
    id: String(index), x: 80 + (index % 8) * 145,
    y: 80 + Math.floor(index / 8) * 145, radius: 18
  }));
  const links = [];
  for (let index = 0; index < 40; index += 1) {
    for (const step of [1, 8, 9]) {
      if (index + step < 40) links.push({ id: links.length,
        source: String(index), target: String(index + step), width: 3 });
    }
  }
  const routes = geometry.routeGraph(nodes, links, { width: 1300, height: 800 });
  assert.equal(routes.length, links.length);
  assert.ok(routes.every(Boolean));
  assert.ok(routes.every(route => route.conflicts === 0));
  assert.ok(routes.every(route => geometry.routeValid(route, nodes,
    { width: 1300, height: 800 })));
});

test("irregular 40-device mesh resolves crowded link angles without overlaps", () => {
  let state = 1;
  const random = () => ((state = (state * 1664525 + 1013904223) >>> 0) / 4294967296);
  const nodes = Array.from({ length: 40 }, (_, index) => ({
    id: String(index), x: 75 + (index % 8) * 145 + (random() - 0.5) * 40,
    y: 75 + Math.floor(index / 8) * 145 + (random() - 0.5) * 40, radius: 18
  }));
  const links = [];
  const seen = new Set();
  while (links.length < 60) {
    const source = Math.floor(random() * 40);
    const target = Math.floor(random() * 40);
    const key = [source, target].sort((a, b) => a - b).join("-");
    if (source === target || seen.has(key)) continue;
    seen.add(key);
    links.push({ id: key, source: String(source), target: String(target),
      width: 1 + Math.floor(random() * 3) });
  }
  const bounds = { width: 1300, height: 800 };
  const routes = geometry.routeGraph(nodes, links, bounds);
  assert.equal(routes.length, links.length);
  assert.ok(routes.every(Boolean));
  assert.ok(routes.every(route => route.conflicts === 0));
  assert.ok(routes.every(route => geometry.routeValid(route, nodes, bounds)));
  for (const node of nodes) {
    const angles = routes.flatMap(route => route.source === node.id ? [route.sourceAngle] :
      route.target === node.id ? [route.targetAngle] : []);
    assert.equal(new Set(angles).size, angles.length, `repeated angle at ${node.id}`);
  }
});
