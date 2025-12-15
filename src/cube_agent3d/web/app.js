import * as THREE from "https://unpkg.com/three@0.160.0/build/three.module.js";
import { OrbitControls } from "https://unpkg.com/three@0.160.0/examples/jsm/controls/OrbitControls.js";

const elCanvas = document.getElementById("canvas");
const elStart = document.getElementById("btnStart");
const elStop = document.getElementById("btnStop");
const elReset = document.getElementById("btnReset");
const elZoomReset = document.getElementById("btnZoomReset");
const elStatus = document.getElementById("status");
const elMeta = document.getElementById("meta");
const elLog = document.getElementById("log");

function logLine(s) {
  const t = new Date().toLocaleTimeString();
  elLog.innerText = `[${t}] ${s}\n` + elLog.innerText;
}

function hexToThreeColor(hex) {
  return new THREE.Color(hex);
}

let ws = null;
let lastStatus = null;
let lastTick = 0;
let lastScore = 0;

// --- Three.js scene
const renderer = new THREE.WebGLRenderer({ canvas: elCanvas, antialias: true });
renderer.setPixelRatio(window.devicePixelRatio || 1);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0f17);

const camera = new THREE.PerspectiveCamera(60, 1, 0.05, 500);
camera.position.set(8, 8, 10);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.minDistance = 3;
controls.maxDistance = 60;
controls.target.set(0, 4, 0);

elZoomReset.addEventListener("click", () => {
  camera.position.set(8, 8, 10);
  controls.target.set(0, 4, 0);
  controls.update();
});

const amb = new THREE.AmbientLight(0xffffff, 0.55);
scene.add(amb);

const dir = new THREE.DirectionalLight(0xffffff, 1.0);
dir.position.set(10, 20, 10);
scene.add(dir);

const grid = new THREE.GridHelper(40, 40, 0x223044, 0x121a27);
grid.position.y = 0;
scene.add(grid);

// Instanced cubes
const MAX_INST = 256;
const geom = new THREE.BoxGeometry(1, 1, 1);

const mat = new THREE.MeshStandardMaterial({
  vertexColors: true,
  roughness: 0.65,
  metalness: 0.1,
});

let inst = new THREE.InstancedMesh(geom, mat, MAX_INST);
inst.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
inst.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(MAX_INST * 3), 3);
scene.add(inst);

// Ghost targets (green transparent)
const MAX_GHOST = 128;
const ghostMat = new THREE.MeshBasicMaterial({
  color: 0x00ff66,
  transparent: true,
  opacity: 0.22,
  depthWrite: false,
});
let ghostInst = new THREE.InstancedMesh(geom, ghostMat, MAX_GHOST);
ghostInst.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
scene.add(ghostInst);

let currentCount = 0;

// Latest snapshot cache (for DUPLICATE offset -> absolute ghost position)
const cubeById = new Map(); // id -> { pos:[x,y,z], scale:[sx,sy,sz] }

// Ghost cache with TTL
let ghosts = []; // {pos:[x,y,z], scale:[sx,sy,sz], until:number(ms)}

const _m = new THREE.Matrix4();
const _p = new THREE.Vector3();
const _q = new THREE.Quaternion();
const _s = new THREE.Vector3();

function applySnapshot(payload) {
  const cubes = payload.cubes || [];
  lastTick = payload.tick || 0;

  cubeById.clear();
  for (const c of cubes) {
    if (c && c.id != null) cubeById.set(String(c.id), { pos: c.pos, scale: c.scale });
  }

  currentCount = Math.min(cubes.length, MAX_INST);

  for (let i = 0; i < currentCount; i++) {
    const c = cubes[i];

    _p.set(c.pos[0], c.pos[1], c.pos[2]);
    _q.set(c.rot[0], c.rot[1], c.rot[2], c.rot[3]);
    _s.set(c.scale[0], c.scale[1], c.scale[2]);

    _m.compose(_p, _q, _s);
    inst.setMatrixAt(i, _m);

    const col = hexToThreeColor(c.color || "#7dd3fc");
    inst.setColorAt(i, col);
  }

  for (let i = currentCount; i < MAX_INST; i++) {
    _m.identity();
    _m.makeScale(0, 0, 0);
    inst.setMatrixAt(i, _m);
  }

  inst.instanceMatrix.needsUpdate = true;
  if (inst.instanceColor) inst.instanceColor.needsUpdate = true;
}

function pushGhost(pos, scale, ttlMs) {
  const now = performance.now();
  ghosts.push({
    pos: [pos[0], pos[1], pos[2]],
    scale: [scale[0], scale[1], scale[2]],
    until: now + (ttlMs || 900),
  });
  if (ghosts.length > 256) ghosts = ghosts.slice(ghosts.length - 256);
}

function applyGhostActions(actions) {
  for (const a of actions || []) {
    if (!a || !a.type) continue;

    // Prefer explicit hint from server policy
    if (a.ghost && a.ghost.pos && a.ghost.scale) {
      pushGhost(a.ghost.pos, a.ghost.scale, a.ghost.ttl_ms || 900);
      continue;
    }

    // Derive from action payload
    if (a.type === "MOVE" && Array.isArray(a.pos) && a.id != null) {
      const id = String(a.id);
      const cur = cubeById.get(id);
      const sc = cur?.scale || [1, 1, 1];
      pushGhost(a.pos, sc, 900);
      continue;
    }

    if (a.type === "DUPLICATE" && a.source_id != null && Array.isArray(a.offset)) {
      const src = cubeById.get(String(a.source_id));
      if (!src) continue;
      const p = [
        src.pos[0] + a.offset[0],
        src.pos[1] + a.offset[1],
        src.pos[2] + a.offset[2],
      ];
      pushGhost(p, src.scale || [1, 1, 1], 900);
      continue;
    }
  }
}

function updateGhostInstances() {
  const now = performance.now();
  ghosts = ghosts.filter(g => g.until > now);

  const n = Math.min(ghosts.length, MAX_GHOST);
  for (let i = 0; i < n; i++) {
    const g = ghosts[i];
    _p.set(g.pos[0], g.pos[1] + 0.01, g.pos[2]); // z-fighting 방지 미세 상승
    _q.set(0, 0, 0, 1);
    _s.set(g.scale[0], g.scale[1], g.scale[2]);
    _m.compose(_p, _q, _s);
    ghostInst.setMatrixAt(i, _m);
  }
  for (let i = n; i < MAX_GHOST; i++) {
    _m.identity();
    _m.makeScale(0, 0, 0);
    ghostInst.setMatrixAt(i, _m);
  }
  ghostInst.instanceMatrix.needsUpdate = true;
}

function connectWS() {
  const proto = (location.protocol === "https:") ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws`;
  ws = new WebSocket(url);

  ws.onopen = () => {
    elStatus.innerText = "연결됨";
    ws.send(JSON.stringify({ type: "HELLO", payload: { client: "web" } }));
    logLine("WebSocket 연결됨");
  };

  ws.onclose = () => {
    elStatus.innerText = "연결 끊김";
    logLine("WebSocket 연결 끊김");
    setTimeout(connectWS, 800);
  };

  ws.onerror = () => {
    elStatus.innerText = "오류";
  };

  ws.onmessage = (ev) => {
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch { return; }

    const type = msg.type;
    const payload = msg.payload || {};

    if (type === "SERVER_STATUS") {
      lastStatus = payload;
      const run = payload.running ? "RUN" : "STOP";
      elStatus.innerText = `${run} | tick=${payload.tick} | cubes=${payload.cube_count}`;
      elMeta.innerText = `session=${payload.session_id} | tick_hz=${payload.tick_hz} | max_cubes=${payload.max_cubes}`;
    }

    if (type === "STATE_SNAPSHOT") {
      applySnapshot(payload);
    }

    if (type === "ACTION_BATCH") {
      lastScore = payload.score || 0;
      const actions = payload.actions || [];

      // 초록 투명 "탐색/목표" 위치 표시
      applyGhostActions(actions);

      if (actions.length > 0) {
        const head = actions[0];
        logLine(`tick=${payload.tick} score=${lastScore.toFixed(2)} actions=${actions.length} head=${head.type}`);
      } else {
        logLine(`tick=${payload.tick} score=${lastScore.toFixed(2)} actions=0`);
      }
    }
  };
}

elStart.addEventListener("click", () => {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "UI_START", payload: {} }));
});

elStop.addEventListener("click", () => {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "UI_STOP", payload: {} }));
});

elReset.addEventListener("click", () => {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "RESET", payload: {} }));
  logLine("RESET 요청");
});

function resize() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);
resize();

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  updateGhostInstances();
  renderer.render(scene, camera);
}
animate();

connectWS();
